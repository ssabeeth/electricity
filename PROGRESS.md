# Progress

**Status: all ten phases complete (tag `v1.0`), plus phase 11, the free hosted
track record (tag `v1.1-track-record`); phase 12, the warehouse built on
BigQuery (tag `v1.2-bigquery`); and phase 13, data patterns, a model comparison
and pre-registered experiments (tag `v1.3-experiments`). No blockers.**
The remote `origin` is `https://github.com/ssabeeth/electricity.git`.

**What only the owner can do.** Each of these costs money or needs the owner's
credentials, and none has been done:

0. **Deploy the free dashboard.** Done 2026-09-24:
   [ukelectricity.streamlit.app](https://ukelectricity.streamlit.app).
1. **Deploy to a VPS.** Provision a 4-8 GB Ubuntu 24.04 machine, point DNS at
   it, and follow `docs/deploy_vps.md`. The configuration is validated in CI;
   start it with `make prod-up`.
2. ~~**BigQuery (optional).**~~ Done 2026-09-25 on the free sandbox of the
   owner's project `elecprice-portfolio`, with the owner's own gcloud login:
   `dbt build` passes and the feature mart matches DuckDB on every value
   (`docs/bigquery.md`, "Result of the first real run"). Sandbox tables expire
   after 60 days; rerun `elec load-bigquery` and `elec dbt build` to refresh.
3. **Rotate the local `.env`.** It was generated with random secrets during
   testing and is git-ignored. Delete it before sharing the machine;
   `make env` regenerates it.

**Local machine state.** Docker was installed via Homebrew (`colima`, `docker`,
`docker-compose`, `docker-buildx`), plus `libomp` for LightGBM and `uv`. Colima
is stopped. `data/` (about 70 MB) holds the full cached history, the warehouse,
the local MLflow store and the outputs.

## Phase status

| Phase | Status | Tag |
|---|---|---|
| 1. Scaffold | done | `v0.1-scaffold` |
| 2. Ingestion | done | `v0.2-ingestion` |
| 3. dbt | done | `v0.3-dbt` |
| 4. Modelling | done | `v0.4-modelling` |
| 5. Battery simulation | done | `v0.5-battery` |
| 6. Orchestration | done | `v0.6-orchestration` |
| 7. Serving | done | `v0.7-serving` |
| 8. Containerise | done | `v0.8-containerise` |
| 9. Deployment prep | done | `v0.9-deployment-prep` |
| 10. README | done | `v1.0` |
| 11. Live track record and free hosting | done; dashboard live at ukelectricity.streamlit.app | `v1.1-track-record` |
| 12. BigQuery, run for real | done; sandbox build passes, marts match DuckDB | `v1.2-bigquery` |
| 13. Data patterns, model comparison, pre-registered experiments | done; nothing adopted | `v1.3-experiments` |

## Log

### Phase 1 — Scaffold (2026-09-22)

Done:
- Verified every data source before building on it (see DECISIONS.md):
  - Elexon MID (APXMIDP), demand outturn, FUELHH, NDF and WINDFOR are all free.
    Their range limits are 7 days for MID, FUELHH and WINDFOR, 28 days for demand
    outturn, and 1 day of publish time for NDF.
  - The NESO embedded wind and solar forecast archive (2019–2026) includes the
    issue time and has a SQL endpoint.
  - The Open-Meteo Previous Runs API works for ICON and GFS from 2024-02-17.
    Nothing is archived before 2024, so the modelling window starts 2024-03-01.
  - The Carbon Intensity API works for 30-day windows.
- `uv` project (Python 3.12) with extras `ml`, `dbt`, `bigquery` and `serve`,
  plus a `dev` group. ruff, pytest and pre-commit are configured; the ruff hook
  uses the locked version.
- Time conventions module (`elecprice.timeutils`) handles the DST-aware
  settlement calendar and the decision cutoff, with tests.
- CI skeleton: lint and tests. dbt and the image build are added in later phases.
- Added `.gitignore` (secrets, data, mlruns, artefacts), `.env.example`,
  `DECISIONS.md` and a README stub.

Environment notes:
- Docker is not installed on the build machine. Compose files will be validated
  in CI; see Phase 8.
- LightGBM on macOS needs `brew install libomp`.

Next: Phase 2, ingestion.

### Phase 2 — Ingestion (2026-09-22)

Done:
- `elecprice.ingest` has eight datasets from four sources behind one chunked,
  cached, idempotent runner (`elec ingest [source|source/name] --start --end`).
- Retries use exponential backoff with jitter on connection errors, 429s and
  5xx responses. Chunk failures are logged and reported; they don't abort the
  run, and the CLI exits non-zero if any chunk failed.
- Full history ingested from 2023-09-01 to 2026-09-22, about 33MB of raw cache
  and 31MB of Parquet:

  | dataset | rows |
  |---|---|
  | elexon/mid | 54k |
  | elexon/demand_outturn | 54k |
  | elexon/fuelhh | 1.06M |
  | elexon/ndf (vintages) | 688k |
  | elexon/windfor (vintages) | 575k |
  | neso/embedded_forecast (vintages) | 1.38M |
  | openmeteo/weather_forecast (vintages, 15 locations) | 698k |
  | carbon/intensity | 54k |

- Found and handled two NESO timezone issues: `Forecast_Datetime` is UK local
  time, and the format changed on 2026-06-13 (see DECISIONS.md).
- Tests cover chunk alignment, skip / refresh / cache-rebuild / force behaviour,
  failure isolation, horizon clipping, every normaliser (with real trimmed
  payloads), the NESO timezone handling and Open-Meteo availability stamps.
  Live smoke tests are marked `network` and excluded from CI.

### Phase 3 — dbt (2026-09-22)

Done:
- dbt project with a `duckdb` target (default) and a `bigquery` target. Every
  non-portable SQL expression sits behind an adapter-dispatched macro
  (`macros/cross_db.sql`).
- Layers:
  - 8 staging views over the Parquet lake, deduplicated on natural keys.
  - A DST-aware settlement calendar with per-day cutoffs.
  - As-of models for NDF, WINDFOR, NESO embedded and weather (15 locations,
    weighted wind, solar and temperature aggregates, plus a turbine power-curve
    index).
  - Price-lag features and daily system-state features.
  - `mart_features` (44,926 half-hours, 2024-03-01 to today, 42 columns) and
    `fct_price_actuals`.
- Tests (99 nodes, about 3 seconds on the full history):
  - `point_in_time` on every as-of model and the mart;
  - a leaky canary model that must fail;
  - 2 dbt unit tests for the as-of logic;
  - uniqueness, not-null and range checks on keys and values;
  - calendar clock-change checks, an as-of optimality check, and a warning if
    no post-cutoff vintages exist (which would make the guard vacuous).
- Source freshness on all 8 sources. Snapshots (type-2 `check`) track
  revisions to MID, demand outturn and generation by fuel.
- The committed fixture lake is 560KB covering 2024-03-01 to 2024-04-07,
  including a clock change. It is reproducible with `make fixtures`. CI runs
  `dbt build` on it and asserts the canary fails.

Known data gaps (warnings, not errors): NDF has no vintages for 38 periods on
2025-07-15. Feature coverage is above 99.8% for every group except
`price_d1_same_period`, which by design is known only for early-morning periods.

### Phase 4 — Modelling (2026-09-22)

Done:
- `elecprice.modelling` contains:
  - the seasonal naive baseline (probabilistic, via residual quantiles);
  - LightGBM quantile models (P10/P50/P90) with a de-levelled target and
    conformal calibration;
  - metrics (pinball, coverage, MAE, RMSE, calibration);
  - a walk-forward backtest with an explicit leakage assertion;
  - MLflow tracking and registry;
  - a report generator.
- Results over 19 monthly folds (2025-03 to 2026-09, 27,339 half-hours), in
  full in `reports/backtest.md`:

  | model | pinball | MAE P50 | P10-P90 coverage |
  |---|---|---|---|
  | seasonal naive | 9.79 | £27.47 | 78.4% |
  | LightGBM quantile | 5.07 | £15.46 | 78.5% |

  LightGBM's pinball skill versus the baseline is 48% overall and 48.5% on the
  hold-out folds, which played no part in model selection.
- The champion model is registered in MLflow (`elecprice-lgbm-quantile@champion`,
  v1), trained on all data through 2026-09-21.
- CLI: `elec backtest`, `elec report`, `elec train --alias champion`.
- 17 new tests, all on synthetic data except one that uses the fixture
  warehouse:
  - pinball properties;
  - baseline behaviour;
  - LightGBM beats the naive baseline and keeps its quantiles ordered;
  - calibration moves coverage towards nominal;
  - save/load round trips;
  - fold construction;
  - the leakage assertion;
  - an end-to-end backtest.

Observations for the README:
- The top features are residual demand (NDF minus wind forecast), recent price
  deviations, and the recent gas share.
- The worst month is 2026-09. Prices jumped to a new level (weekday peaks above
  £200) with windy weekends near £0. Without a gas input the model adapts with a
  lag, and coverage on the latest 28-day fold is 58%.

### Phase 5 — Battery simulation (2026-09-22)

Done:
- `elecprice.battery`:
  - the LP optimiser (HiGHS, with a MILP fallback against simultaneous
    charge/discharge);
  - settlement at actual prices;
  - four strategies;
  - a parallel simulation over the backtest forecasts;
  - `reports/battery.md` with a cumulative £ chart and an example-day chart.
- Results for a 1 MW / 2 MWh battery over 568 settled out-of-sample days
  (2025-03-01 to 2026-09-21):

  | strategy | net £ | £/MW/yr | share of perfect foresight |
  |---|---|---|---|
  | perfect foresight (upper) | 57,940 | 37,232 | 100% |
  | LightGBM P50 forecast | 42,128 | 27,072 | 72.7% |
  | seasonal naive forecast | 27,036 | 17,373 | 46.7% |
  | fixed overnight/evening rule (lower) | 2,450 | 1,574 | 4.2% |

  The better forecast is worth about £9.7k per MW per year over the naive
  forecast on the same asset.
- CLI `elec simulate`, which logs to the MLflow experiment `elecprice-battery`.
- 14 tests:
  - physics (SoC dynamics, bounds, no simultaneous charge/discharge);
  - an analytic optimum on a two-price day;
  - flat prices mean no trading;
  - the cycle limit;
  - perfect foresight dominates on random days;
  - forecast schedules don't change when actual prices change (no leakage);
  - fixed-rule windows and determinism;
  - an end-to-end simulate/summarise run.

### Phase 6 — Orchestration (2026-09-22)

Done:
- `elecprice.pipeline` covers the live daily steps (`elec forecast`,
  `elec schedule`, `elec monitor`) and the champion/challenger retrain
  (`elec retrain`). It has atomic Parquet upserts for serving outputs.
- Airflow 3.3 DAGs: `elec_ingest` (every 3 h), `elec_daily_forecast` (09:05
  UK) and `elec_weekly_retrain` (Sunday 06:00 UK). All call the `elec` CLI via
  `BashOperator`.
- Verified locally:
  - forecasting tomorrow before its cutoff is refused;
  - today's forecast and schedule are written;
  - the retrain registers a challenger and gives "no challenger to judge" on
    its first run;
  - the MLflow store was rebuilt so every version uses models-from-code logging
    with a signature.
- Tests:
  - 3 pure tests (promotion rule, judgement fold, upsert semantics and
    permissions);
  - 1 end-to-end test on the fixture warehouse with a throwaway MLflow store
    (bootstrap → challenger → judged promotion → forecast → schedule → monitor);
  - 5 DAG integrity tests on real Airflow 3.3 (new `airflow` CI job, or
    `make test-airflow` locally).
- Fixed along the way:
  - output files were created with mode 0600, which would break cross-container
    reads;
  - the partially settled current day was being included in training.

### Phase 7 — Serving (2026-09-22)

Done:
- FastAPI (`elecprice.serving.api`), with typed Pydantic responses and OpenAPI
  docs at `/docs`:
  - `/health`
  - `/forecast/latest`, `/forecast/{date}` and `/forecast/range`
  - `/backtest/metrics` and `/backtest/coverage`
  - `/simulation/summary`, `/simulation/daily` and `/simulation/schedule/{date}`
  - `/live/metrics`
- Streamlit dashboard (`elecprice.serving.dashboard`) with four tabs:
  - latest forecast: fan chart plus the battery schedule;
  - backtest: KPIs, a week-picker fan chart, rolling coverage over time,
    pinball by fold and feature importance;
  - battery £: cumulative revenue by strategy and a summary table;
  - live monitoring.
- Verified against the real outputs: every endpoint returns 200, `uvicorn` and
  `streamlit` start and pass their health checks, and the dashboard's KPIs match
  the reports (48% skill, £15.5 MAE, 78.5% coverage, £27.1k/MW/yr, 73% of
  perfect foresight).
- 8 tests on outputs written by the real pipeline writers (a tiny backtest and
  simulation on synthetic data). They cover endpoint contracts, 404/422
  handling, live-over-backtest precedence, and a headless render of every
  dashboard tab.
- The in-app browser could not open localhost here, so the visual check was
  done via Streamlit `AppTest` plus HTTP health checks instead of screenshots.

### Phase 8 — Containerise (2026-09-22)

Done:
- `docker/app.Dockerfile` (API, dashboard, MLflow server, CLI) and
  `docker/airflow.Dockerfile` (Airflow 3.3.2 plus the project in an isolated
  venv).
- `docker-compose.yml`: Postgres, airflow-init, the Airflow api-server,
  scheduler and dag-processor, MLflow, the API and the dashboard, with health
  checks on every service.
- `make up` creates `.env` with random secrets and the host UID, then builds and
  starts the stack. A new `elec_bootstrap` DAG (runs once) builds everything
  from a clean clone. `make down`, `make ps` and `make logs` manage the stack.
- The `warehouse` Airflow pool (1 slot) serialises every task that opens DuckDB.
  A DAG test enforces it.
- Tested for real with Colima (Docker 29.5, Compose 5.5):
  - all 7 long-running services are healthy;
  - the pool and admin user were created;
  - `elec_bootstrap` succeeded, with the champion registered in the
    containerised MLflow;
  - the catch-up daily forecast run succeeded after the `--as-of` fix;
  - the API served the containers' forecast, and the dashboard and Airflow UI
    returned 200.
- CI gained an `images` job: compose validation, build of both images, and
  smoke tests.
- Found and fixed during testing:
  - macOS AirPlay occupies port 5000, so MLflow is now on host port 5001;
  - the daily DAG forecast the wall-clock "tomorrow" rather than the run's day;
  - the Airflow image's Python is at `/usr/python/bin`.

Environment note: Docker was installed via Homebrew (`colima`, `docker`,
`docker-compose`, `docker-buildx`). Colima is left stopped.

### Phase 9 — Deployment prep (2026-09-22). Not executed, as instructed.

Done:
- `docs/deploy_vps.md` covers sizing (measured), hardening, DNS, configuration,
  start-up, a verification checklist, operations (updates, backups, restore,
  alerts, logs, secret rotation), security notes and rollback.
- `deploy/docker-compose.prod.yml` adds Caddy on 80/443, binds everything else
  to localhost, sets memory limits and log rotation, and configures the Airflow
  base URL. It is validated by merging the Compose config.
- `deploy/Caddyfile` provides automatic HTTPS, security headers and basic auth
  on MLflow. It is validated with `caddy validate`.
- `docs/bigquery.md` covers GCP setup (service account, roles), configuration,
  raw loading, `dbt build`, reading the marts from Python, Docker wiring, cost
  and rollback.
- `elec load-bigquery` supports full reloads, `--recent-days` appends and a
  `--dry-run` that needs no credentials. `ELEC_WAREHOUSE=bigquery` switches the
  modelling read path.
- New tests:
  - an offline BigQuery compile with every compiled file parsed by sqlglot
    (94 files);
  - loader planning matching the dbt source names;
  - timestamp typing for BigQuery;
  - the marts-schema switch.
- Found by measuring: MLflow 3's job consumers used 2.1 GB. They are now
  disabled, and the stack idles at about 1.7 GB.

Owner actions (these cost money or need credentials):
1. VPS: provision a 4-8 GB Ubuntu 24.04 machine, point DNS at it and follow
   `docs/deploy_vps.md`.
2. BigQuery: create a GCP project and service account and follow
   `docs/bigquery.md`.

### Phase 10 — README (2026-09-22)

Done:
- The README covers:
  - the headline results (forecast and battery £) with figures;
  - "why the numbers can be trusted" (seven correctness guarantees);
  - a Mermaid architecture diagram;
  - quickstarts for Docker and local development;
  - the repository layout and the data-source table;
  - a decisions table with one line per tool;
  - the forecast-vs-observed weather discussion with measured leakage;
  - testing and CI, known limitations, and further reading.
- New leakage experiment (`scripts/leakage_experiment.py` ->
  `reports/leakage_experiment.md`). Weather issued after the cutoff flatters
  pinball by 0.1%, observed weather by 1.3%, and actual outturn by 9.9%.
- Regenerated `reports/backtest.md` and `reports/battery.md` from the final
  outputs. The weekly-retrain run during Docker testing had refreshed them, so
  the README, reports, API and dashboard now show the same numbers.

Final headline numbers:
- **Backtest** (19 folds, 27,344 half-hours): LightGBM pinball 5.05 against the
  baseline's 9.79 (48.4% skill; 48.7% on hold-out folds). P50 MAE £15.48
  against £27.47. P10-P90 coverage 78.7% (nominal 80%).
- **Battery** (568 days, 1 MW / 2 MWh): the forecast-driven schedule earns
  £42,162, 72.8% of perfect foresight's £57,940. The naive-forecast schedule
  earns £27,036 (46.7%), and the fixed rule £2,450. The better forecast is
  worth about £9.7k per MW per year.

### Phase 11 — Live track record and free hosting (2026-09-24)

Done:
- `elec export-model` exports the champion with a SHA-256 per file; published as
  release `model-v1` (trained through 2026-09-21), which serves September.
- Monthly refit, the backtest's protocol: the first forecast of each month is
  made by a model trained on every day up to D-2, published as `model-YYYY-MM`
  before the record cites it. `models.json` on the branch lists every model.
- `elecprice.pipeline.track_record`: the append-only record (one CSV per delivery
  day, written once, refused if made after delivery began), its load into the
  Parquet outputs, pooled scores and the branch README.
- `elec track-record plan` / `daily`: plan which model to fetch and whether to
  refit; refit if a month starts; forecast tomorrow once; settle past days;
  update scores. It skips the forecast if run before the cutoff.
- The live pipeline now also schedules the battery from the seasonal-naive
  forecast, and settles every strategy against perfect foresight.
- `.github/workflows/track-record.yml` runs daily at 09:20 UTC and commits to the
  `track-record` branch; the commit step refuses to modify a published forecast.
  CI ignores that branch.
- Dashboard: a Track record tab (coverage, pinball skill, cumulative £) and
  provenance links on live forecasts. `deploy/streamlit/` is the Community Cloud
  entry point, with its own minimal requirements.
- Verified: a cold 200-day ingest takes about 80 s; features and forecasts from
  that window match the full history exactly; the hosted dashboard runs from its
  minimal requirements with no exceptions.

The owner deployed the dashboard at https://ukelectricity.streamlit.app.

### Phase 12 — BigQuery, run for real (2026-09-25)

Done:
- `elec load-bigquery` loaded the eight raw tables (4.5 M rows) into the
  owner's sandbox project; `elec dbt build` on the `bigquery` target passes
  (95 pass, 1 warning that DuckDB shares on the common days).
- Fixed the one portability bug the real run found (`accepted_values` on
  integers needs `quote: false` on BigQuery).
- Auth defaults to the owner's gcloud login (`method: oauth`); a service-account
  key stays an option (`BQ_AUTH_METHOD=service-account`).
- `BQ_SANDBOX=true` skips the snapshots, whose updates are a `MERGE` the
  sandbox forbids.
- `scripts/compare_warehouses.py`: the BigQuery feature mart equals DuckDB's on
  all 2,246,300 values.

### Phase 13 — Data patterns, model comparison, experiments (2026-09-26)

Done:
- `elec patterns` writes `reports/data_patterns.md`: nine patterns on the
  delivery days before the hold-out, each with what it implies. Two first
  readings were wrong and corrected before use: day-level volatility needs a
  log scale (one spike dominates a day's standard deviation), and a
  between-years classifier scored ROC-AUC 1.00 only because single held-out
  days share their neighbours' 7-day inputs (0.74 with whole months held out).
- The rule and the experiment list were committed to DECISIONS.md before the
  first run. Selection folds are now 1-12 (a full year); 13-19 are the
  hold-out.
- `elec compare-models`: LightGBM 4.330, XGBoost 4.369, CatBoost 4.397, point
  model with residual intervals 4.867, linear quantile regression 5.362, D-2
  naive 8.134, baseline 8.484. Trees and direct quantiles are clearly better;
  the three boosting libraries are within the noise.
- `elec experiments`: none of eight passed; monotone constraints could not run
  (LightGBM refuses them with the quantile objective). The first full run
  crashed on that error, so the harness now records a variant that cannot run
  instead of failing the whole programme.
- `reports/backtest.md` re-split from saved predictions for the new hold-out.
- 12 new tests: the candidate features ignore prices after D-2, the merit-order
  regression recovers a known curve, the bootstrap and the rule, and every
  comparison model and variant fits and gives ordered quantiles.
