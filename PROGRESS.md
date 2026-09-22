# Progress

_No blockers._ The remote `origin` is `https://github.com/ssabeeth/electricity.git`.

## Phase status

| Phase | Status | Tag |
|---|---|---|
| 1. Scaffold | done | `v0.1-scaffold` |
| 2. Ingestion | done | `v0.2-ingestion` |
| 3. dbt | done | `v0.3-dbt` |
| 4. Modelling | done | `v0.4-modelling` |
| 5. Battery simulation | done | `v0.5-battery` |
| 6. Orchestration | done | `v0.6-orchestration` |
| 7. Serving | next | |
| 8. Containerise | | |
| 9. Deployment prep | | |
| 10. README | | |

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
