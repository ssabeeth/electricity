# Free hosting: Streamlit Community Cloud and a GitHub Actions track record

The full stack (Airflow, MLflow, API, dashboard) needs a 4-8 GB server; see
[deploy_vps.md](deploy_vps.md). This is the free alternative. It keeps the part a
reviewer can check, a live forecast every day scored against real prices, and
leaves Airflow and MLflow to `make up` on a laptop.

```
GitHub Actions, daily 09:20 UTC                     Streamlit Community Cloud
  ingest last 200 days  ─┐                            deploy/streamlit/streamlit_app.py
  dbt build (PIT test)   │                              downloads the track-record branch
  refit (a new month)    ├─► track-record branch ──►    (at most hourly), loads it into the
  forecast D+1, schedule │   forecasts/, schedules/,     output layout, and serves the normal
  settle past days      ─┘   scores/, models.json        dashboard with the API in-process
```

Nothing costs money. No secrets are needed: every data source is keyless, and the
workflow uses the repository's own `GITHUB_TOKEN`.

## What is already set up

- **`.github/workflows/track-record.yml`**: the daily job. It also runs on demand
  from the Actions tab (Actions → track-record → Run workflow). A run before the
  09:00 UK cutoff settles past days and does not forecast.
- **The `track-record` branch**: the record itself. Each forecast is committed by
  `github-actions[bot]`, with a link to the run that made it, before its delivery
  day begins. The commit step fails rather than change or delete a published
  forecast.
- **Model releases**: `model-v1` is the champion exported with
  `elec export-model`, and serves September 2026. At the first forecast of each
  later month the workflow refits on every delivery day up to two days before
  (the backtest's protocol), publishes the result as `model-YYYY-MM`, and only
  then commits forecasts that cite it. `export.json` in each `model.tar.gz`
  records the training window and a SHA-256 of every file, and the workflow
  checks those hashes before forecasting. `models.json` on the branch lists
  every model used.

## Deploy the dashboard (done; kept for redeploying)

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
2. **Create app** → **Deploy a public app from GitHub**, then fill in:
   - Repository: `ssabeeth/electricity`
   - Branch: `main`
   - Main file path: `deploy/streamlit/streamlit_app.py`
   - App URL: pick a subdomain, e.g. `gb-power-forecast`
3. **Advanced settings** → Python version **3.12**. No secrets are needed.
4. **Deploy**. The first build takes a few minutes.

Community Cloud installs `deploy/streamlit/requirements.txt`, not the project's
`pyproject.toml`, because a requirements file beside the entry point takes
precedence. That file carries only what the dashboard imports: no MLflow, dbt or
LightGBM.

The deployed app is [ukelectricity.streamlit.app](https://ukelectricity.streamlit.app).

## Optional hardening

The record's claim rests on published forecasts never changing. The workflow
already refuses to modify them; a branch rule makes the same promise at the
repository level:

Settings → Rules → Rulesets → New branch ruleset → target `track-record` →
enable **Restrict deletions** and **Block force pushes**.

## Running it locally

```bash
git worktree add ../record track-record         # a checkout of the record
uv run elec ingest --days 200 && uv run elec dbt build
ELEC_MODEL_DIR=/path/to/model uv run elec track-record daily --record ../record

# The hosted dashboard, against a local checkout instead of GitHub:
ELEC_RECORD_DIR=../record uv run streamlit run deploy/streamlit/streamlit_app.py
```

## Changing the model

Refits happen by themselves. To change the method itself (features or
`configs/model.yaml`), change the code: the next monthly refit uses it, and
`models.json` shows the boundary. Do not replace a published release; publish
a new one, because forecasts already cite the old one by name.

## Things to know

- **Scheduled runs can be late.** GitHub starts cron jobs when it has capacity,
  sometimes an hour or more after the scheduled time. 09:20 UTC leaves until
  midnight UK. A day missed is a gap in the record, and is never back-filled.
- **GitHub disables scheduled workflows** in public repositories after 60 days
  without repository activity. If that happens, re-enable it from the Actions tab.
- **The API cache is a courtesy, not a dependency.** A cold run ingests 200 days
  from the four APIs in about 80 seconds, so an evicted cache only costs time.
  A refit day ingests the full history and takes several minutes longer.
