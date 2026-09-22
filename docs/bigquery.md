# Running the warehouse on BigQuery

The dbt project has two targets. `duckdb` is the default, used locally, in CI
and in Docker. `bigquery` is configured and ready, but **has not been run**,
because it needs a GCP project and service account that only the owner can
create. Everything below is for the owner to execute.

## What has been verified without credentials

- `dbt/profiles.yml` has a `bigquery` output (service-account auth, `europe-west2`).
- Every non-portable SQL expression goes through an adapter-dispatched macro
  (`dbt/macros/cross_db.sql`): timezone conversion, date and integer series,
  timestamp arithmetic, day of week and "now".
- `tests/test_dbt_bigquery.py` (runs in CI) compiles the whole project for the
  `bigquery` target offline, using a throwaway key. It then parses all 94
  compiled models, tests and unit tests with sqlglot's BigQuery dialect. They
  all parse.
- `elec load-bigquery --dry-run` plans the raw-table loads without credentials.

What remains unverified until it runs for real: column types at runtime,
permissions, and cost. The first `dbt build` on BigQuery is the real test.

## Cost

The raw data is about 4.5 M rows (under 1 GB), and a full `dbt build` scans a
few GB. That is well inside BigQuery's free tier (10 GB storage and 1 TB of
queries per month). BigQuery still needs a billing account on the project,
which is the owner's decision.

## 1. Create the GCP resources

```bash
PROJECT=my-elecprice-project
gcloud projects create $PROJECT        # or use an existing one
gcloud config set project $PROJECT
gcloud services enable bigquery.googleapis.com

gcloud iam service-accounts create elecprice-dbt --display-name "elecprice dbt"
SA=elecprice-dbt@$PROJECT.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$SA --role roles/bigquery.dataEditor
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$SA --role roles/bigquery.jobUser
gcloud iam service-accounts keys create ~/.config/elecprice-sa.json --iam-account $SA
chmod 600 ~/.config/elecprice-sa.json
```

`dataEditor` plus `jobUser` is enough to create datasets and tables and run
queries. Scope `dataEditor` to the datasets below if you prefer, after
creating them yourself.

## 2. Configure

In `.env`:

```bash
DBT_TARGET=bigquery
ELEC_WAREHOUSE=bigquery            # Python modelling reads marts from BigQuery
GCP_PROJECT=my-elecprice-project
BQ_DATASET=elecprice               # dbt writes elecprice_staging, _intermediate, _marts, ...
BQ_RAW_DATASET=elecprice_raw       # raw tables loaded from the Parquet lake
BQ_LOCATION=europe-west2
GOOGLE_APPLICATION_CREDENTIALS=/home/you/.config/elecprice-sa.json
```

Dataset layout: the `generate_schema_name` macro prefixes each layer with the
target dataset, which gives `elecprice_staging`, `elecprice_intermediate`,
`elecprice_marts`, `elecprice_seeds` and `elecprice_snapshots`.

## 3. Load the raw data

Ingestion is unchanged: `elec ingest` still writes the local Parquet lake and
raw cache. The lake is then loaded into BigQuery:

```bash
uv sync --all-extras                 # includes dbt-bigquery and google-cloud-bigquery
uv run elec load-bigquery --dry-run  # what will be loaded
uv run elec load-bigquery            # full reload (WRITE_TRUNCATE), one table per source
```

Tables are named `<source>_<name>` (`elexon_mid`, `neso_embedded_forecast`,
...) to match `dbt/models/staging/_sources.yml`. Timestamps in the lake are
naive UTC. The loader makes them timezone-aware so BigQuery types them
`TIMESTAMP`, which is what the models compare against. Mixing `DATETIME` and
`TIMESTAMP` is a type error in BigQuery.

For daily operation, append recent chunks after each ingest. The staging models
already deduplicate on natural keys, keeping the latest `_ingested_at`:

```bash
uv run elec load-bigquery --recent-days 7
```

In Airflow, add that command as a task after `ingest_recent` in
`elec_ingest.py` and after `ingest_latest` in the daily DAG.

## 4. Build

```bash
uv run elec dbt debug      # checks credentials and connectivity
uv run elec dbt seed
uv run elec dbt build      # models, snapshots, point-in-time tests, unit tests
uv run elec dbt source freshness
```

The point-in-time test, the unit tests and the schema tests run unchanged. A
failure here is a real portability bug. Fix it in `macros/cross_db.sql`, not
in the models.

## 5. Model and serve

With `ELEC_WAREHOUSE=bigquery`, `load_frame()` queries
`<project>.<BQ_DATASET>_marts.mart_features` and `fct_price_actuals` through the
BigQuery client, and converts timestamps back to naive UTC. `elec backtest`,
`elec train`, `elec forecast`, `elec retrain` and `elec monitor` then work
unchanged. The API and dashboard read the pipeline's Parquet outputs, so they
don't care which warehouse built them.

In Docker, mount the key and set the variables for the Airflow services, for
example in a `docker-compose.override.yml`:

```yaml
services:
  airflow-scheduler:
    environment:
      DBT_TARGET: bigquery
      ELEC_WAREHOUSE: bigquery
      GCP_PROJECT: ${GCP_PROJECT}
      GOOGLE_APPLICATION_CREDENTIALS: /run/secrets/gcp.json
    volumes:
      - ${GOOGLE_APPLICATION_CREDENTIALS}:/run/secrets/gcp.json:ro
```

The Airflow image installs the `ml` and `dbt` extras. Add `--extra bigquery`
to the `uv sync` lines in `docker/airflow.Dockerfile` when switching.

## Rollback

Set `DBT_TARGET=duckdb` and `ELEC_WAREHOUSE=duckdb` again. The local lake and
DuckDB warehouse are untouched by the BigQuery path.
