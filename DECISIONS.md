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
