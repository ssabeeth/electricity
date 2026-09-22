# Progress

_No blockers._ The remote `origin` is `https://github.com/ssabeeth/electricity.git`.

## Phase status

| Phase | Status | Tag |
|---|---|---|
| 1. Scaffold | done | `v0.1-scaffold` |
| 2. Ingestion | done | `v0.2-ingestion` |
| 3. dbt | next | |
| 4. Modelling | | |
| 5. Battery simulation | | |
| 6. Orchestration | | |
| 7. Serving | | |
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
