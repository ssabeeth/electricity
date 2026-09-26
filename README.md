# Live forecast track record

Day-ahead forecasts of the GB Market Index Price, committed here **before each delivery day starts** and scored once the actual prices are published. Nothing in `forecasts/` or `schedules/` is ever edited; the workflow refuses to change a published file, and each commit links to the GitHub Actions run that made it.

Code, method and backtest: [https://github.com/ssabeeth/electricity](https://github.com/ssabeeth/electricity).

## Models

The model is refitted at the first forecast of each month on every delivery day up to two days before, the same protocol as the walk-forward backtest. Each is published as a release of the repository, and `forecasts/*.csv` name the one that made them.

| Release | Serves from | Trained on delivery days |
|---|---|---|
| [model-v1](https://github.com/ssabeeth/electricity/releases/tag/model-v1) | 2026-09 | 2024-03-01 to 2026-09-21 |

## Running totals

- Days forecast: 3 (2026-09-25 to 2026-09-27)
- Days settled against actual prices: 1
- P10–P90 coverage: 95.8% of 48 half-hours (nominal 80%)
- MAE of the P50: £14.16/MWh
- Pinball-loss skill against the seasonal-naive baseline: 81.5%

| Battery strategy (1 MW / 2 MWh) | Net £ | Share of perfect foresight |
|---|---|---|
| Scheduled on the LightGBM forecast | £57.93 | 60.4% |
| Scheduled on the seasonal-naive forecast | £73.01 | 76.2% |
| Perfect foresight (upper bound) | £95.83 | — |

`snapshot/` holds the walk-forward backtest and battery simulation from release v1.0, which the dashboard shows beside this record. It is not part of the live record and is not scored here.

## How to check it

Open any file in `forecasts/` and look at its history: the commit that added it predates the delivery day in its name. `created_at` inside the file is when it was computed, and `cutoff_utc` is the 09:00 UK information cutoff the features respect.

_Updated 2026-09-26 13:47 UTC._
