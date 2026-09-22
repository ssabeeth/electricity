# Decisions log

Each entry: date, the decision, options considered, and why. Newest last.

---

## 2026-09-22 — Target series: Elexon Market Index Price (APXMIDP)

**Options:** (a) EPEX / N2EX GB day-ahead auction prices; (b) Elexon Market Index
Data from the `N2EXMIDP` provider; (c) Elexon Market Index Data from `APXMIDP`;
(d) imbalance system price.

**Decision:** (c). The day-ahead auction results are commercial data sets and are
not available from a free API. `N2EXMIDP` reports zero volume for every period
checked in 2023 and 2026, so it carries no information. `APXMIDP` is free,
half-hourly, available since before 2023, and is the reference price Elexon uses
for the imbalance reverse price. The imbalance price was rejected because it is
set after delivery by system actions, which is a different forecasting problem.

**Consequence:** the project forecasts MID, not the day-ahead auction clearing
price. The README says so plainly. MID reflects short-term (mostly within-day
and day-ahead) trading, so it is a reasonable but imperfect proxy for what a
day-ahead trader would lock in. Periods with zero traded volume are treated as
missing rather than as a price of £0.

## 2026-09-22 — Decision cutoff: 09:00 Europe/London on D-1

**Options:** 09:00, 11:00 or 12:00 on D-1.

**Decision:** 09:00 UK local time on D-1, as specified in the brief. It is a
realistic morning cutoff for a day-ahead position ahead of GB auction gate
closures. The cutoff is a setting (`ELEC_CUTOFF_LOCAL`) and is converted to UTC
per day, so it is correct across daylight-saving changes (08:00 UTC in summer,
09:00 UTC in winter).

## 2026-09-22 — Weather: Open-Meteo Previous Runs API, ICON, availability-stamped vintages

**Options:** (a) Open-Meteo historical weather archive (observed / reanalysis);
(b) Historical Forecast API; (c) Previous Runs API.

**Decision:** (c). (a) is observed weather and would leak the outcome. (b)
stitches together the first hours of successive runs, which is close to observed
weather and is not what was known a day ahead. (c) exposes each variable as it
was forecast N days before its valid time (`_previous_dayN`).

Open-Meteo defines `_previous_day1` as "predicted 24 hours before valid time". For
a valid time at 18:00 on D, that run was made at about 18:00 on D-1, which is
*after* the 09:00 D-1 cutoff. Using `_previous_day1` naively would leak for most
of the day. We therefore store both `_previous_day1` and `_previous_day2` as
separate vintages, each stamped with a conservative *available-at* time of
`valid_time - N*24h + 6h` (worst-case run initialisation plus a 6-hour
publication delay). The feature build takes, per valid hour, the latest vintage
available at the cutoff, and a dbt test fails the build if any row breaks this.
In practice `_previous_day2` supplies almost every hour of D.

Model: `icon_seamless` (DWD ICON-EU nested in ICON global). It has
temperature, 100 m wind speed, shortwave radiation and cloud cover archived from
2024-02-17 onwards. ECMWF IFS coverage of 100 m wind starts later (2024-03-07),
and UKMO has no archived previous runs in the period.

## 2026-09-22 — History window: ingest from 2023-09-01, model from 2024-03-01

**Options:** (a) three years for everything, filling missing weather with NaN;
(b) three years of market data, modelling window starting when as-issued weather
exists; (c) drop weather.

**Decision:** (b). Prices, demand, generation and carbon intensity are ingested
from 2023-09-01 (three years), which feeds lag features and the seasonal
baseline. Archived weather forecasts only exist from 2024-02-17, so the feature
mart and the backtest start at 2024-03-01. Training on four months where every
weather feature is missing would teach the model a regime that never occurs in
production.

## 2026-09-22 — Demand and wind forecasts: Elexon NDF and WINDFOR, NESO embedded forecasts

**Options:** NESO data portal only, or Elexon BMRS for the NESO forecasts it
republishes, plus the NESO portal for what BMRS lacks.

**Decision:** The day-ahead national demand forecast (`NDF`) and the
transmission-connected wind forecast (`WINDFOR`) are NESO forecasts. Elexon
republishes them with a `publishTime` per vintage and lets us query by publish
time, which is exactly what point-in-time features need. The NESO portal is used
for the embedded (distribution-connected) wind and solar forecast archive, which
BMRS does not carry and which also has a per-vintage `Forecast_Datetime`.

To keep volumes sensible, forecast vintages are only ingested when issued between
04:00 and 11:00 UTC. That range covers the latest vintage before the cutoff in
both GMT and BST. It also deliberately includes vintages issued *after* the
cutoff, so the point-in-time test has something to catch.

## 2026-09-22 — Carbon intensity: lagged actuals only

The Carbon Intensity API returns a `forecast` for each half-hour but does not say
when that forecast was made, so it cannot be used point-in-time. Only `actual`
intensity for periods that ended before the cutoff is used, as a proxy for how
gas-heavy the recent generation mix was.

## 2026-09-22 — Git remote

The repository had no git history and no remote. The owner supplied
`https://github.com/ssabeeth/electricity.git` during the session; it is used as
`origin`.

## 2026-09-22 — NESO `Forecast_Datetime` timezone

**Finding:** the NESO embedded forecast archive labels its columns `DATE_GMT` and
`TIME_GMT`, but `Forecast_Datetime` is UK local time. There is no 01:12 vintage
on the spring clock-change day (2026-03-29), and in summer the first period of
each vintage is the one containing the issue time *minus one hour* in UTC.
Treating it as UTC would make summer vintages look an hour older than they were.

**Decision:** localise `Forecast_Datetime` to Europe/London. Resolve ambiguous
autumn times to the later instant.

**Format change:** from 2026-06-13 NESO switched `TIME_GMT` to `HH:MM` and moved
vintages to irregular minutes (for example 06:53:03). In the new format the first
forecast period is the *next* half-hour in UTC, which no longer pins down the
timezone. For those rows we use the UTC reading. It is never earlier than the
local reading (it is one hour later in BST and identical in GMT), so it is the
conservative choice. Each row records its basis in `forecast_time_basis`
(`uk_local` or `utc_assumed`).

## 2026-09-22 — Ingestion design: aligned chunks, raw cache, Parquet lake

**Options:** write straight into DuckDB tables; land raw JSON only; or land a raw
cache plus normalised Parquet.

**Decision:** each dataset is fetched in fixed chunks aligned to a fixed epoch,
so re-runs with a different start date reuse the same files. The raw response is
kept gzipped, which means we never need to re-hit an API. A normalised Parquet
file sits next to it, and dbt reads those files directly. Chunks older than
`refresh_days` (default 3) are never re-fetched. Recent chunks are, because
sources back-fill them. This keeps ingestion idempotent and the warehouse
rebuildable from files alone, with no database state to migrate. The same
Parquet files can be loaded into BigQuery.

## 2026-09-22 — Point-in-time feature design in dbt

- **As-of joins with window functions, not `ASOF JOIN`.** Each forecast source is
  joined to the settlement calendar on the target period, with
  `issued_at <= cutoff_utc`, keeping the newest match via
  `row_number() ... = 1`. DuckDB's `ASOF JOIN` would be terser, but this form runs
  unchanged on BigQuery.
- **Every feature group carries its availability timestamp.** These are
  `ndf_published_at`, `windfor_published_at`, `emb_issued_at`,
  `wx_available_at`, `price_available_at` and `system_available_at`. The generic
  `point_in_time` test fails if any is later than `cutoff_utc`. It also fails if
  a feature is non-null while its timestamp is null, so a forgotten timestamp
  cannot silently disable the guard.
- **The guard is proven, not assumed.** `pit_canary_leaky` reproduces a
  realistic bug: the cutoff is off by three hours. CI asserts that the test
  rejects it. dbt unit tests pin the as-of behaviour on hand-written vintages.
- **Target kept out of the feature mart.** Prices live in `fct_price_actuals`.
  The mart contains only what the model may see, so the PIT test covers all of
  it.
- **MID availability lag: 60 minutes after the period ends.** MID rows have no
  publish time. Elexon publishes within minutes; 60 is conservative. The same
  lag applies to carbon intensity actuals. Both are dbt vars.
- **Lags matched on UK clock time.** "Same half-hour last week" joins on
  local start time, so the naive baseline and the price lags stay aligned across
  clock changes. On the autumn change the repeated hour matches twice; the later
  match is kept.
- **Hourly sources.** WINDFOR and weather are hourly; both half-hours of an hour
  use that hour's value. For shortwave radiation, which Open-Meteo reports as
  the mean over the preceding hour, this is a 30-minute simplification.
- **Mart rows appear only after their cutoff has passed**, so the mart never
  contains a half-finished forecast input set.
- **Settlement calendar generated in SQL**, with DST-aware day boundaries via
  adapter-dispatched timezone macros (DuckDB `timezone()` and BigQuery
  `TIMESTAMP(DATETIME, tz)`).

## 2026-09-22 — Modelling choices

**Walk-forward design.** There are 19 monthly folds, 2025-03 to 2026-09 (the
last one partial). Training uses an expanding window from 2024-03-01, refitted
each fold on delivery days up to `test_start - 2 days`. At 09:00 on D-1 the
newest fully settled day is D-2. `assert_no_leakage` enforces this on every fold
by checking that the last training period ended before the first test cutoff.
Monthly refits approximate the weekly production retrain and keep the backtest
to about 3 minutes.

**Model selection without peeking at the hold-out.** Folds 1-6 (2025-03 to
2025-08) are the selection set; folds 7-19 are reported separately. Two choices
were made on the selection folds:

| Choice | Options (selection-fold pinball) | Picked |
|---|---|---|
| Target | `level` 4.69, `delta_7d_mean` 4.66 | `delta_7d_mean` |
| Calibration | `none` 4.66 (coverage 55%), `outer` 4.46, `all` 4.45 (coverage 81%) | `all` |

The hold-out folds agree with both choices: `level` scores 6.55 against
`delta_7d_mean`'s 5.76, and `all` scores 5.37 against `none`'s 5.76. The
LightGBM hyperparameters are sensible defaults and were not tuned, to avoid
overfitting the backtest.

**Target: price minus trailing 7-day mean.** Prices in 2026-Q3 averaged about
£120/MWh, above anything in the first year of training. Trees cannot
extrapolate levels, and without a gas price input the level has to come from
recent prices. Learning the deviation from the 7-day mean, which is known at the
cutoff, handles level shifts better.

**Conformal calibration.** Raw LightGBM quantiles covered only about 55% of
outcomes with the P10-P90 interval. A split-conformal step fixes this. The
model is fitted on the training window minus its last 56 days (with the same
two-day gap). On those 56 days we measure, for each quantile, the shift that
makes exactly that fraction of outcomes fall below it. The model is then refitted
on the full window, and the shifts are applied at prediction time. The
calibrated interval achieved 78.5% coverage and pinball loss fell from 5.40 to
5.07. Refitting after calibrating is the usual practical compromise; strict
split-conformal guarantees would need the calibrated model itself.

**Seasonal naive baseline, probabilistic.** P50 is the same UK clock time last
week, as the brief requires. P10/P90 add empirical residual quantiles by local
hour over the last 180 training days. That lets the baseline be scored with the
same pinball and coverage metrics. Its coverage is also about 78%, so the
comparison is like for like: the models reach similar coverage, and LightGBM's
interval is half as wide.

**Model registry.** Models are logged as MLflow pyfunc models, with the
LightGBM boosters and calibration shifts as artifacts. They are registered as
`elecprice-lgbm-quantile`, and production is the `champion` alias (MLflow
stages are deprecated).

## 2026-09-22 — Battery simulation

**Asset parameters** (`configs/battery.yaml`, all adjustable):

- **Size:** 1 MW / 2 MWh, the brief's asset and a typical GB 2-hour battery.
- **Round-trip efficiency:** 90%, split as √0.9 on charge and √0.9 on discharge.
- **Cycle limit:** 1.5 full-equivalent cycles per day, measured on energy out of
  storage. That is within typical warranty terms (about 1-2 cycles per day).
- **Degradation cost:** £10 per MWh discharged. This is linear in throughput.
  Published estimates for Li-ion are roughly £5-20/MWh; the value sets the
  minimum spread worth trading.

**State of charge resets daily, starting and ending empty.** Each delivery day is
scheduled independently, matching one day-ahead decision per day. I first set
the daily start at 50%. That limits a price-blind rule to 1 MWh a day, which made
the lower bound a strawman. Starting empty is the common convention in daily
arbitrage studies and lets every strategy run a full cycle.

**Optimiser:** an LP in PuLP, solved with HiGHS in-process via `highspy`.
PuLP 3 deprecates its bundled CBC, and HiGHS takes about 1.7 ms per solve
against CBC's 50 ms. If the LP solution charges and discharges in the same
period, which a real battery cannot do, the day is re-solved as a MILP with one
binary mode variable per period.

**Leakage structure:** `decision_inputs()` gives each strategy its decision
prices, and `settle()` alone sees actual prices. A unit test checks that
changing actual prices does not change a forecast-driven schedule. The forecasts
are the walk-forward backtest predictions, so each day's schedule used a model
trained only on data available at that point.

**Bounds:**

- **Upper bound:** perfect foresight, the same LP run on actual prices.
- **Lower bound:** a fixed rule that charges from 01:00 and discharges from
  16:30 UK time at full power. It is written as explicit logic, not as an LP.
  An LP version had many equally optimal schedules inside each window, and its
  result changed from £3.2k to £15.4k when the solver changed. That is a
  tie-breaking artefact, not a property of the strategy.
- **Middle reference:** the seasonal naive forecast fed through the same LP. It
  isolates the value of the better forecast from the value of optimising at all.

## 2026-09-22 — Orchestration

**Airflow tasks shell out to the `elec` CLI in a separate virtualenv.** Airflow
pins hundreds of dependencies through its constraints file. Installing
LightGBM, dbt and MLflow alongside it invites resolver conflicts. The Airflow
image therefore gets the project installed in its own venv (`ELEC_BIN`), and
DAGs use `BashOperator` to call the same commands a developer runs (`make daily`
is the daily DAG). Rejected: installing everything into Airflow's environment
(fragile) and `DockerOperator` (needs the Docker socket inside Airflow, which is
heavier and a security concern on a small VPS). DAG integrity tests run in CI
against real Airflow 3.3 in its own venv.

**Three DAGs, scheduled in UK local time:**

| DAG | Schedule | Steps |
|---|---|---|
| `elec_ingest` | every 3 h | rolling 7-day ingest, then source freshness |
| `elec_daily_forecast` | 09:05 daily | ingest latest, dbt build (point-in-time tests), forecast D+1, battery schedule, settle past days |
| `elec_weekly_retrain` | Sunday 06:00 | ingest, dbt build, champion/challenger retrain, backtest refresh, battery simulation refresh |

Cron is evaluated in Europe/London, so the daily run tracks 09:05 local time
across clock changes. If a point-in-time test fails, `dbt build` fails and no
forecast is issued.

**Champion/challenger with a one-week lag.** The brief says a retrain is
promoted only if it beats production on the latest fold. Two naive
interpretations fail:

- Scoring the current champion on a fold it was trained on favours it
  (in-sample).
- Refitting both recipes on the same window can never show the benefit of new
  data, because the same recipe on the same data gives the same model.

Instead, each week's model (fitted on all complete days) is registered as
`challenger`. The following week, challenger and champion are scored on the
settled days after both models' training data ended. That fold is
out-of-sample for both, and the challenger has one more week of data. The
challenger is promoted only if its mean pinball loss is strictly lower, over at
least 5 days of periods. Ties keep production. Every decision is logged to the
MLflow experiment `elecprice-retrain`. The pure rule (`decide`) is unit tested,
and the full flow is tested end to end on the fixture warehouse.

**Models train on complete days only.** The partially settled current day is
excluded, so a model's `trained_through` tag is an honest boundary for its
out-of-sample fold.

**Serving outputs are Parquet files, not DuckDB tables.** DuckDB allows one
writer process, and Airflow writes while the API reads. Pipeline outputs
(forecasts, schedules, live metrics, backtest and simulation results) are small
Parquet files written atomically with world-readable permissions (containers run
as different users). The API reads only these, never the warehouse.

**No back-filled "live" history.** Scoring past days with today's champion
would be in-sample and flattering. Live monitoring starts at deployment.
Historical performance comes only from the walk-forward backtest.

## 2026-09-22 — Serving

- **The API reads pipeline outputs only**: Parquet and CSV under `data/outputs`,
  cached by file modification time. It never opens the DuckDB warehouse, so it
  cannot block or be blocked by the single-writer pipeline. It is read-only and
  stateless, so it scales by adding replicas.
- **The forecast endpoints fall back to backtest forecasts** when no live
  forecast exists for a date. Every response carries a `source` field
  (`live` or `backtest`) so the two are never confused. Before the first live
  run, the dashboard still shows real out-of-sample forecasts.
- **Coverage over time is computed from the walk-forward predictions**, as daily
  coverage plus a rolling mean with a configurable window. Live coverage will
  join it as live days settle.
- **The dashboard talks only to the API**, as a separate service would. It also
  has an in-process mode (`ELEC_API_URL=inprocess`) for local use and for the
  headless Streamlit `AppTest`, which renders every tab in CI.
- **No authentication on the API.** It exposes public-data forecasts
  read-only. The deploy guide puts it, the dashboard and Airflow behind a
  reverse proxy, with basic auth on Airflow and the option to add it to
  everything.

## 2026-09-22 — Containerisation

- **Two images.**
  - `elecprice-app` (python:3.12-slim, uv, the project with the ml, dbt and
    serve extras) runs the API, the dashboard and the MLflow server. MLflow uses
    the same image so the server and client versions always match.
  - `elecprice-airflow` (apache/airflow:3.3.2) carries the project in a
    separate venv (`ELEC_BIN`), for the dependency-isolation reasons in the
    orchestration decision.
- **Airflow 3 topology:** LocalExecutor on Postgres 16, with the api-server,
  scheduler and dag-processor as separate services, plus a one-shot
  `airflow-init` that migrates the DB, creates the admin user through the FAB
  auth manager and creates the `warehouse` pool. There is no Celery or Redis;
  one machine is the target.
- **`make up` from a clean clone.** It writes `.env` with random secrets and the
  host UID, builds the images and starts everything. The one-off `elec_bootstrap`
  DAG then ingests history, runs `dbt build`, backtests, registers a champion,
  simulates the battery and issues the first forecast. Every step is idempotent.
  Verified locally with Colima: all services healthy, bootstrap succeeded, the
  daily DAG succeeded in the containers, and the API served the forecast they
  produced.
- **Data is a bind mount (`./data`), not a named volume.** It holds the raw cache,
  the lake, the warehouse, the MLflow server store and the outputs. Containers
  run as the host UID with group 0, so files stay readable and deletable on the
  host, and the same cache serves local runs and containers.
- **MLflow's server store is `data/mlflow-server`**, separate from the local-dev
  sqlite store in `data/mlflow`. Local runs record artifact paths as host paths,
  which do not exist inside containers.
- **MLflow is published on host port 5001.** On macOS, port 5000 belongs to the
  AirPlay Receiver (found during testing). Inside the Compose network it is
  still `mlflow:5000`.
- **First-start catch-up runs.** Airflow 3 creates one run for the most recent
  past interval of each newly unpaused DAG, even with `catchup=False`. This
  exposed a real bug: the daily forecast used the wall clock for "tomorrow", so
  a late or retried run would forecast the wrong day, or none. Tasks now pass
  `--as-of '{{ dag_run.run_after }}'` and the delivery day is derived from the
  run's own time. The `warehouse` pool kept the concurrent catch-up runs from
  contending for DuckDB.

## 2026-09-22 — Deployment prep (not executed)

**VPS.** A single VM with Docker Compose and Caddy as the only public service
(automatic HTTPS). Every other port is bound to `127.0.0.1`, because Docker's
iptables rules bypass ufw.

- **Airflow** is protected by its own FAB login. Proxy basic auth would collide
  with the Airflow 3 UI's `Authorization: Bearer` API calls, so an IP allowlist
  or VPN is documented as the second layer.
- **MLflow** has no login of its own and can delete models, so it sits behind
  Caddy basic auth.
- **The API and dashboard** are public read-only views of public-data forecasts.
  Basic auth is one line to add.

Memory limits come from measured `docker stats`: about 1.7 GB idle and 2.3 GB
at the weekly backtest peak, which fits a 4 GB VM. Measuring exposed that
MLflow 3's server starts six job consumers for GenAI features (2.1 GB in total).
`--workers 1` plus `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` brings it down to
335 MB. The configuration is validated (merged Compose config, `caddy validate`),
but no VPS was provisioned because that costs money.

**BigQuery.** The profile has existed since phase 3. This phase adds:

- `elec load-bigquery`, which loads the Parquet lake into `elecprice_raw.<source>_<name>`.
  It makes timestamps timezone-aware so BigQuery types them `TIMESTAMP`, which
  avoids `DATETIME`/`TIMESTAMP` comparison errors.
- `ELEC_WAREHOUSE=bigquery`, which makes the Python modelling code read the
  marts from BigQuery.
- An offline test that compiles the project for BigQuery with a throwaway key
  and parses all 94 compiled files with sqlglot's BigQuery dialect.
- `where true` before bare `QUALIFY` clauses, since BigQuery has required a
  `WHERE`, `GROUP BY` or `HAVING` alongside `QUALIFY`.

Runtime behaviour on BigQuery remains unverified until the owner creates
credentials (docs/bigquery.md).

## 2026-09-22 — Quantifying leakage instead of asserting it

`scripts/leakage_experiment.py` reruns the identical walk-forward backtest on
four feature sets that differ only in when their inputs were knowable. Results
(pinball; apparent improvement over honest):

| Feature set | Pinball | Apparent improvement |
|---|---|---|
| Honest | 5.052 | — |
| Weather forecast issued after the cutoff | 5.047 | +0.1% |
| Observed weather (reanalysis) | 4.987 | +1.3% |
| Actual demand and wind outturn instead of NESO forecasts | 4.555 | +9.9% |

The README reports this as it is. For this feature set, weather leakage is
small, because NESO's forecasts carry the weather signal. The dangerous leak is
realised system outturn, which sits in the same Elexon API as the forecasts. The
observed-weather archive was downloaded for this experiment only, cached under
`data/raw/experiments/`, and is not an input to any pipeline model.
