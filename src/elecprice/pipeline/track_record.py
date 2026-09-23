"""A public, append-only record of live forecasts, scored once prices are known.

The record is a plain directory, published as the ``track-record`` branch:

    forecasts/YYYY-MM-DD.csv   P10/P50/P90 per half-hour, written once, before the day starts
    schedules/YYYY-MM-DD.csv   battery schedules set from those forecasts, written with them
    scores/forecast_daily.csv  accuracy per day, once actual prices exist
    scores/battery_daily.csv   P&L per day at actual prices, with the perfect-foresight bound
    models.json                every model used, with its training window and file hashes
    README.md                  running totals, regenerated each run

Forecast and schedule files are never rewritten. A backtest can always be tuned
after the fact; a file committed before its delivery day cannot, and the branch's
git history is the timestamp. Scores are derived data and are recomputed each run.

CSV rather than Parquet so anyone can read the record on GitHub without tooling.
Only pandas is imported here, because the hosted dashboard uses this module too.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from elecprice.timeutils import local_midnight_utc

FORECASTS, SCHEDULES, SCORES = "forecasts", "schedules", "scores"
FORECAST_SCORES = f"{SCORES}/forecast_daily.csv"
BATTERY_SCORES = f"{SCORES}/battery_daily.csv"
# Static backtest and simulation outputs the dashboard shows beside the live record.
SNAPSHOT = "snapshot"

TIME_COLS = ("start_time_utc", "cutoff_utc", "created_at")
LGBM, NAIVE = "lgbm_quantile", "seasonal_naive"


class RecordError(RuntimeError):
    """The record would stop being trustworthy if this went ahead."""


def _day_file(root: Path, kind: str, day: date) -> Path:
    return root / kind / f"{day.isoformat()}.csv"


def has_day(root: Path, day: date) -> bool:
    return _day_file(root, FORECASTS, day).exists()


def delivery_start_utc(day: date) -> pd.Timestamp:
    """00:00 UK time on ``day``, in UTC (naive), for comparison with ``created_at``."""
    return local_midnight_utc(day).tz_localize(None)


def _to_csv(df: pd.DataFrame, path: Path) -> None:
    out = df.copy()
    out["settlement_date"] = pd.to_datetime(out["settlement_date"]).dt.strftime("%Y-%m-%d")
    for col in TIME_COLS:
        if col in out:
            out[col] = pd.to_datetime(out[col]).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False, float_format="%.4f")


def _read_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["settlement_date"] = pd.to_datetime(df["settlement_date"])
    for col in TIME_COLS:
        if col in df:
            df[col] = pd.to_datetime(df[col], utc=True).dt.tz_localize(None)
    for col in ("model_version",):
        if col in df:
            df[col] = df[col].astype(str)
    return df


def record_day(root: Path, day: date, forecasts: pd.DataFrame, schedules: pd.DataFrame) -> bool:
    """Write ``day``'s forecasts and schedules once. Returns False if already recorded.

    Refuses to record a day whose delivery has already started, or a forecast
    made after that point: the record's only claim is that it could not have
    seen the prices it is scored against.
    """
    if has_day(root, day):
        return False
    fc = forecasts[pd.to_datetime(forecasts["settlement_date"]) == pd.Timestamp(day)]
    sc = schedules[pd.to_datetime(schedules["settlement_date"]) == pd.Timestamp(day)]
    if fc.empty or sc.empty:
        raise RecordError(f"nothing to record for {day}: forecasts or schedules are missing")
    start = delivery_start_utc(day)
    latest = max(pd.to_datetime(fc["created_at"]).max(), pd.to_datetime(sc["created_at"]).max())
    if latest >= start:
        raise RecordError(
            f"the forecast for {day} was made at {latest} UTC, after delivery began at "
            f"{start} UTC; it cannot go in the record"
        )
    _to_csv(fc.sort_values(["model", "settlement_period"]), _day_file(root, FORECASTS, day))
    _to_csv(sc.sort_values(["strategy", "settlement_period"]), _day_file(root, SCHEDULES, day))
    return True


def _load_days(root: Path, kind: str) -> pd.DataFrame:
    files = sorted((root / kind).glob("*.csv"))
    if not files:
        return pd.DataFrame()
    return pd.concat([_read_csv(f) for f in files], ignore_index=True)


def load(root: Path) -> dict[str, pd.DataFrame]:
    """Every part of the record as DataFrames (empty where nothing exists yet)."""
    out = {"forecasts": _load_days(root, FORECASTS), "schedules": _load_days(root, SCHEDULES)}
    for key, rel in (("forecast_daily", FORECAST_SCORES), ("battery_daily", BATTERY_SCORES)):
        path = root / rel
        out[key] = _read_csv(path) if path.exists() else pd.DataFrame()
    return out


def materialise(root: Path, outputs_dir: Path) -> dict[str, int]:
    """Write the record into the Parquet layout the pipeline and API read.

    The record is the source of truth: these files are replaced, not merged, so
    the live outputs can never hold a forecast the record does not.
    """
    parts = load(root)
    live = outputs_dir / "live"
    live.mkdir(parents=True, exist_ok=True)
    names = {
        "forecasts": "forecasts.parquet",
        "schedules": "battery_schedules.parquet",
        "forecast_daily": "forecast_daily_metrics.parquet",
        "battery_daily": "battery_daily.parquet",
    }
    for key, name in names.items():
        path = live / name
        if parts[key].empty:
            path.unlink(missing_ok=True)
        else:
            parts[key].to_parquet(path, index=False)
    snapshot = root / SNAPSHOT
    if snapshot.exists():
        for sub in snapshot.iterdir():
            if sub.is_dir():
                shutil.copytree(sub, outputs_dir / sub.name, dirs_exist_ok=True)
    return {key: len(df) for key, df in parts.items()}


def write_scores(root: Path, outputs_dir: Path) -> None:
    """Copy the monitor's per-day scores from the Parquet outputs into the record."""
    live = outputs_dir / "live"
    for rel, name in (
        (FORECAST_SCORES, "forecast_daily_metrics.parquet"),
        (BATTERY_SCORES, "battery_daily.parquet"),
    ):
        path = live / name
        if path.exists():
            df = pd.read_parquet(path)
            keys = ["settlement_date", "model" if "model" in df else "strategy"]
            _to_csv(df.sort_values(keys), root / rel)


def summarise(root: Path) -> dict:
    """Running totals over every settled day.

    Coverage and pinball loss are pooled over half-hours (each day weighted by the
    number of priced periods it had), not averaged over days, so a day with gaps
    counts for less.
    """
    parts = load(root)
    fc, fd, bd = parts["forecasts"], parts["forecast_daily"], parts["battery_daily"]
    out: dict = {
        "days_forecast": int(fc["settlement_date"].nunique()) if not fc.empty else 0,
        "first_day": str(fc["settlement_date"].min().date()) if not fc.empty else None,
        "last_day": str(fc["settlement_date"].max().date()) if not fc.empty else None,
        "days_settled": 0,
    }
    if not fd.empty:
        settled = fd.groupby("model")
        out["days_settled"] = int(fd.loc[fd["model"] == LGBM, "settlement_date"].nunique())
        pooled = {}
        for model, g in settled:
            w = g["n"]
            pooled[model] = {
                "coverage": float((g["coverage"] * w).sum() / w.sum()),
                "pinball_mean": float((g["pinball_mean"] * w).sum() / w.sum()),
                "mae_p50": float((g["mae_p50"] * w).sum() / w.sum()),
                "half_hours": int(w.sum()),
            }
        out["models"] = pooled
        if LGBM in pooled and NAIVE in pooled:
            out["pinball_skill_vs_naive"] = 1 - (
                pooled[LGBM]["pinball_mean"] / pooled[NAIVE]["pinball_mean"]
            )
    if not bd.empty:
        totals = bd.groupby("strategy")["net_gbp"].sum()
        out["net_gbp"] = {k: float(v) for k, v in totals.items()}
        if "perfect_foresight" in totals and totals["perfect_foresight"] > 0:
            out["capture_vs_perfect"] = {
                k: float(v / totals["perfect_foresight"])
                for k, v in totals.items()
                if k != "perfect_foresight"
            }
    return out


def write_readme(root: Path, repo_url: str, models: list[dict] | None = None) -> str:
    """The branch's front page: what the record is, how to check it, and the totals."""
    s = summarise(root)
    if models is None:
        path = root / "models.json"
        models = json.loads(path.read_text()) if path.exists() else []
    lines = [
        "# Live forecast track record",
        "",
        "Day-ahead forecasts of the GB Market Index Price, committed here **before each "
        "delivery day starts** and scored once the actual prices are published. Nothing in "
        "`forecasts/` or `schedules/` is ever edited; the workflow refuses to change a "
        "published file, and each commit links to the GitHub Actions run that made it.",
        "",
        f"Code, method and backtest: [{repo_url}]({repo_url}).",
        "",
    ]
    if models:
        lines += [
            "## Models",
            "",
            "The model is refitted at the first forecast of each month on every delivery day "
            "up to two days before, the same protocol as the walk-forward backtest. Each is "
            "published as a release of the repository, and `forecasts/*.csv` name the one "
            "that made them.",
            "",
            "| Release | Serves from | Trained on delivery days |",
            "|---|---|---|",
        ]
        for m in models:
            window = f"{m.get('trained_from', '…')} to {m.get('trained_through')}"
            lines.append(
                f"| [{m['release']}]({repo_url}/releases/tag/{m['release']}) "
                f"| {m.get('month', '')} | {window} |"
            )
        lines.append("")
    lines += [
        "## Running totals",
        "",
        f"- Days forecast: {s['days_forecast']}"
        + (f" ({s['first_day']} to {s['last_day']})" if s["first_day"] else ""),
        f"- Days settled against actual prices: {s['days_settled']}",
    ]
    models = s.get("models", {})
    if LGBM in models:
        m = models[LGBM]
        lines.append(
            f"- P10–P90 coverage: {m['coverage']:.1%} of {m['half_hours']:,} half-hours "
            "(nominal 80%)"
        )
        lines.append(f"- MAE of the P50: £{m['mae_p50']:.2f}/MWh")
    if "pinball_skill_vs_naive" in s:
        lines.append(
            f"- Pinball-loss skill against the seasonal-naive baseline: "
            f"{s['pinball_skill_vs_naive']:.1%}"
        )
    if "net_gbp" in s:
        lines.append("")
        lines.append("| Battery strategy (1 MW / 2 MWh) | Net £ | Share of perfect foresight |")
        lines.append("|---|---|---|")
        labels = {
            "forecast_lgbm": "Scheduled on the LightGBM forecast",
            "forecast_naive": "Scheduled on the seasonal-naive forecast",
            "perfect_foresight": "Perfect foresight (upper bound)",
        }
        capture = s.get("capture_vs_perfect", {})
        for key, label in labels.items():
            if key in s["net_gbp"]:
                share = f"{capture[key]:.1%}" if key in capture else "—"
                lines.append(f"| {label} | £{s['net_gbp'][key]:,.2f} | {share} |")
    if (root / SNAPSHOT).exists():
        lines += [
            "",
            "`snapshot/` holds the walk-forward backtest and battery simulation from release "
            "v1.0, which the dashboard shows beside this record. It is not part of the live "
            "record and is not scored here.",
        ]
    lines += [
        "",
        "## How to check it",
        "",
        "Open any file in `forecasts/` and look at its history: the commit that added it "
        "predates the delivery day in its name. `created_at` inside the file is when it was "
        "computed, and `cutoff_utc` is the 09:00 UK information cutoff the features respect.",
        "",
        f"_Updated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC._",
        "",
    ]
    text = "\n".join(lines)
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(text)
    return text


__all__ = [
    "RecordError",
    "delivery_start_utc",
    "has_day",
    "load",
    "materialise",
    "record_day",
    "summarise",
    "write_readme",
    "write_scores",
]
