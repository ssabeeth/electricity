# UK Electricity Price Forecasting — Build Instructions

You are building a portfolio project end to end, working autonomously. The owner is not available to answer questions. Make reasonable decisions, record them, and keep moving. Only stop for the items listed under "Stop and hand back".

## Goal

A production-style pipeline that:
1. Forecasts UK electricity prices (half-hourly, next-day horizon) with prediction intervals.
2. Uses **forecast** weather available at decision time, never observed weather.
3. Converts forecast quality into £ through a battery arbitrage simulation.
4. Runs on a schedule, is containerised, tested, and deployable.

The audience is hiring managers and technical reviewers. Correctness and clarity beat feature count.

## Stack

- Python 3.12, managed with `uv`
- Airflow for orchestration (DAGs for ingestion, daily forecast, weekly retrain)
- dbt with two targets:
  - `duckdb`: default, used for local runs and CI
  - `bigquery`: configured but not used until credentials exist
- MLflow for experiment tracking and the model registry
- LightGBM with quantile loss for the P10, P50 and P90 forecasts, plus a naive seasonal baseline
- FastAPI to serve the latest forecasts and simulation results
- Streamlit dashboard
- Docker Compose for the full stack
- GitHub Actions for CI: lint, tests, `dbt build` on sample data, image build
- `ruff`, `pytest`, `pre-commit`

## Data sources (all free, verify before building on them)

- **Elexon Insights / BMRS API**: market index price, demand, generation by fuel type. Confirm which price series is freely available. The Market Index Price is the likely target. If true day-ahead auction prices are not free, forecast MID and say so plainly in the README.
- **NESO data portal**: demand and wind/solar forecasts.
- **Open-Meteo**: use the historical forecast or previous-runs endpoints so you get forecasts as they were issued. Do not use the observed-weather archive as a model input. Use several representative GB locations, weighted towards wind and solar regions.
- **Carbon Intensity API**: carbon intensity data.

Cache raw responses to disk and never re-hit APIs unnecessarily. Start with 3 years of history.

## Non-negotiable correctness rules

1. **Point-in-time features.**
   - Every weather forecast row stores its issue time.
   - Features for a delivery day may only use data available before the decision cutoff. Assume the cutoff is 09:00 UK time on the day before delivery, and document that assumption.
   - Enforce this with a custom dbt test that fails the build if any feature row uses data issued after the cutoff.
2. **Walk-forward backtesting only.** Use expanding-window folds and no random splits.
3. **The baseline comes first.** Every model is reported against a naive seasonal baseline (same half-hour, previous week).
4. **Probabilistic evaluation.** Report pinball loss, interval coverage at P10–P90, and MAE on P50.
5. **No leakage in the battery simulation.**
   - The battery schedule is set from forecasts only.
   - It is settled against actual prices.
   - Run it with a perfect-foresight upper bound and a naive-strategy lower bound, so £ results have context.

## Build phases

Complete each phase fully, commit, then move on. Do not add orchestration until the modelling works.

1. **Scaffold.**
   - Repo structure, `uv` project, `pre-commit`, `ruff`, CI skeleton, `.env.example`, `README` stub.
2. **Ingestion.**
   - Plain Python scripts that pull each source into raw Parquet or DuckDB.
   - Add retries and logging.
   - Idempotent re-runs.
3. **dbt.**
   - Raw → staging → intermediate → feature marts.
   - Source freshness checks.
   - The point-in-time test.
   - Schema tests on keys and nulls.
   - Snapshots for revised data.
4. **Modelling.**
   - Baseline and LightGBM quantile models.
   - Walk-forward backtest.
   - Log everything to MLflow.
   - Commit a short `reports/backtest.md` with metrics and plots.
5. **Battery simulation.**
   - Assume a 1 MW / 2 MWh battery, 90% round-trip efficiency, a cycle limit and degradation cost. Treat all of these as parameters and document them.
   - Simple optimisation using LP via `pulp`, or a clear heuristic.
   - Report forecast-driven £ against the upper and lower bounds.
6. **Orchestration.**
   - Airflow DAGs wrapping phases 2–5.
   - The weekly retrain promotes a model in the MLflow registry only if it beats production on the latest fold.
7. **Serving.**
   - FastAPI endpoints for the latest forecast, backtest metrics and simulation results.
   - The Streamlit dashboard shows:
     - forecast fan charts
     - coverage over time
     - cumulative £
8. **Containerise.**
   - Docker Compose with Airflow, MLflow, API and dashboard.
   - `make up` brings the whole stack up from a clean clone.
9. **Deployment prep.**
   - A deploy guide for a 4–8GB VPS, with Airflow's UI behind auth.
   - A BigQuery profile and setup guide.
   - Do not execute either of these.
10. **README.**
    - Architecture diagram in Mermaid.
    - Quickstart.
    - Results.
    - A "Decisions" section explaining why each tool was chosen and the forecast-vs-observed weather issue.
    - Known limitations.

## Working rules

- **Decide, don't ask.** When something is ambiguous, pick the sensible option. Log it in `DECISIONS.md` with a date, the options considered and the reason.
- **Keep a log.** Maintain `PROGRESS.md` with what is done, what is next and any blockers. Update it at the end of every phase.
- **Commit often.** Use small commits with conventional messages. Never commit secrets, API keys or large data files. Use `.gitignore` and put sample data under `tests/fixtures`.
- **Test as you go.** Every phase needs passing tests before it is marked done. Write unit tests for the transforms, the point-in-time logic and the battery simulator.
- **Develop on a small slice.** Use a short date range while developing, then run the full history once the pipeline works.
- **Work around blockers.** If an API is down or rate-limits, use cached data or fixtures and note it in `PROGRESS.md`. Do not stall.
- **Keep scope tight.** No Kubernetes, Spark or agents in this project.

## Git and GitHub

- **Remote.** Assume `origin` is already configured and points to the owner's GitHub repo. At the start, run `git remote -v` to confirm it. If no remote exists, keep committing locally and note it at the top of `PROGRESS.md`. Do not create repos yourself.
- **Branches.** Work on a branch per phase, e.g. `phase-3-dbt`. Push the branch regularly so work is backed up.
- **Merging.** When a phase is complete and all tests pass:
  - merge the branch into `main`
  - push `main`
  - tag the merge commit, e.g. `v0.3-dbt`
- **Keep `main` green.** Never push a failing build to `main`. Never force-push. Never rewrite pushed history.
- **Commit hygiene.**
  - Before every push, check the diff for secrets and for data files over 5MB.
  - The `.gitignore` must cover `.env`, data directories, `mlruns/` and model artefacts from the first commit.

## Stop and hand back only for

- Credentials that must be created by the owner: a GCP service account or VPS access.
- A data source turning out not to be free or not to exist, where the fallback would change the project's headline claim.
- Anything that costs money.

When stopping, write the exact situation and the recommended next step at the top of `PROGRESS.md`.
