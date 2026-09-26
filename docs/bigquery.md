# Running the warehouse on BigQuery

The dbt project has two targets. `duckdb` is the default, used locally, in CI
and in Docker. `bigquery` builds the same warehouse in a GCP project. It was
run for real on 2026-09-25, in the owner's project on the free BigQuery
sandbox (no billing account).

## Result of the first real run (2026-09-25)

- `elec load-bigquery` loaded all eight raw tables (4.5 M rows) in about 30
  seconds.
- The first `dbt build` found a portability bug that the offline compile could
  not: three `accepted_values` tests on integer columns quoted their values,
  and BigQuery will not compare `INT64` with a string (DuckDB casts silently).
  They now set `quote: false`.
- After that fix, `dbt build` passes on BigQuery: 95 of 96 nodes pass,
  including the point-in-time test and both unit tests. The one warning is
  the `warn`-severity `not_null` test on `ndf_demand_mw`: 220 rows, of which 38
  fall in the days both builds cover (DuckDB warns on the same 38) and 182 are
  the future days the newer build's calendar adds, which have no forecast yet.
- `scripts/compare_warehouses.py` read `mart_features` and `fct_price_actuals`
  from both warehouses and compared all 2,246,300 values (44,926 delivery
  periods, 50 columns): **no differences**. The BigQuery build was two days
  newer, so its settlement calendar ran four empty days further; those rows
  have no price and are left out of the comparison.
- Snapshots: their first run (`CREATE TABLE`) worked on BigQuery. Updating a
  snapshot is a `MERGE`, and the sandbox forbids DML ("DML queries are not
  allowed in the free tier"), so on the sandbox they are skipped with
  `BQ_SANDBOX=true`. With billing enabled, leave it unset and they run as on
  DuckDB.

The sandbox has two other limits: tables expire after 60 days, and there is
no streaming insert (the loader uses load jobs, which are allowed).

## What is verified offline, in CI

- Every non-portable SQL expression goes through an adapter-dispatched macro
  (`dbt/macros/cross_db.sql`): timezone conversion, date and integer series,
  timestamp arithmetic, day of week and "now".
- `tests/test_dbt_bigquery.py` compiles the whole project for the `bigquery`
  target offline, using a throwaway service-account key. It then parses all 94
  compiled models, tests and unit tests with sqlglot's BigQuery dialect.
- `elec load-bigquery --dry-run` plans the raw-table loads without credentials.

## Cost

The raw data is about 4.5 M rows (under 1 GB), and a full `dbt build` scans a
few GB. That is well inside BigQuery's free tier (10 GB storage and 1 TB of
queries per month). The sandbox needs no billing account at all, which is how
it was run; a billing account removes the sandbox limits (and is needed for
the snapshots), and is the owner's decision.

## 1. Create the GCP resources

```bash
PROJECT=my-elecprice-project
gcloud projects create $PROJECT        # or use an existing one
gcloud config set project $PROJECT
gcloud services enable bigquery.googleapis.com
```

**Auth, option A: your own login (what the first run used).** dbt and the
Python client use Application Default Credentials, so nothing is stored in the
repository or `.env`:

```bash
gcloud auth application-default login
```

**Auth, option B: a service account** (for a server, such as the VPS):

```bash
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
BQ_SANDBOX=true                    # only on the free sandbox: skips the snapshots (MERGE)
# option B only:
# BQ_AUTH_METHOD=service-account
# GOOGLE_APPLICATION_CREDENTIALS=/home/you/.config/elecprice-sa.json
```

`BQ_AUTH_METHOD` defaults to `oauth` (option A).

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

Then check the BigQuery marts against the local DuckDB build:

```bash
uv run python scripts/compare_warehouses.py
```

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
      BQ_AUTH_METHOD: service-account
      GOOGLE_APPLICATION_CREDENTIALS: /run/secrets/gcp.json
    volumes:
      - ${GOOGLE_APPLICATION_CREDENTIALS}:/run/secrets/gcp.json:ro
```

The Airflow image installs the `ml` and `dbt` extras. Add `--extra bigquery`
to the `uv sync` lines in `docker/airflow.Dockerfile` when switching.

## Rollback

Set `DBT_TARGET=duckdb` and `ELEC_WAREHOUSE=duckdb` again. The local lake and
DuckDB warehouse are untouched by the BigQuery path.
