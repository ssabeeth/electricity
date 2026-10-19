# Live forecast track record

Day-ahead forecasts of the GB Market Index Price, committed here **before each delivery day starts** and scored once the actual prices are published. Nothing in `forecasts/` or `schedules/` is ever edited; the workflow refuses to change a published file, and each commit links to the GitHub Actions run that made it.

Code, method and backtest: [https://github.com/ssabeeth/electricity](https://github.com/ssabeeth/electricity).

## Models

The model is refitted at the first forecast of each month on every delivery day up to two days before, the same protocol as the walk-forward backtest. Each is published as a release of the repository, and `forecasts/*.csv` name the one that made them.

| Release | Serves from | Trained on delivery days |
|---|---|---|
| [model-v1](https://github.com/ssabeeth/electricity/releases/tag/model-v1) | 2026-09 | 2024-03-01 to 2026-09-21 |
| [model-2026-10](https://github.com/ssabeeth/electricity/releases/tag/model-2026-10) | 2026-10 | 2024-03-01 to 2026-09-29 |

## Running totals

- Days forecast: 25 (2026-09-25 to 2026-10-20)
- Days settled against actual prices: 23
- P10–P90 coverage: 83.6% of 1,104 half-hours (nominal 80%)
- MAE of the P50: £23.95/MWh
- Pinball-loss skill against the seasonal-naive baseline: 55.9%

| Battery strategy (1 MW / 2 MWh) | Net £ | Share of perfect foresight |
|---|---|---|
| Scheduled on the LightGBM forecast | £3,798.91 | 80.5% |
| Scheduled on the seasonal-naive forecast | £2,992.25 | 63.4% |
| Perfect foresight (upper bound) | £4,716.34 | — |

`snapshot/` holds the walk-forward backtest and battery simulation from release v1.0, which the dashboard shows beside this record. It is not part of the live record and is not scored here.

## How to check it

Open any file in `forecasts/` and look at its history: the commit that added it predates the delivery day in its name. `created_at` inside the file is when it was computed, and `cutoff_utc` is the 09:00 UK information cutoff the features respect.

_Updated 2026-10-19 16:32 UTC._
