"""Backtest figures and ``reports/backtest.md``, built from saved backtest outputs."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

LGBM, NAIVE = "lgbm_quantile", "seasonal_naive"
LABELS = {LGBM: "LightGBM quantile", NAIVE: "Seasonal naive (baseline)"}
COLORS = {LGBM: "#2a6fdb", NAIVE: "#9a9a9a"}

plt.rcParams.update(
    {
        "figure.dpi": 110,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.alpha": 0.25,
        "font.size": 9,
    }
)


def load_outputs(result_dir: Path) -> dict:
    return {
        "predictions": pd.read_parquet(result_dir / "predictions.parquet"),
        "folds": pd.read_csv(result_dir / "fold_metrics.csv"),
        "summary": pd.read_csv(result_dir / "summary.csv"),
        "importance": pd.read_csv(result_dir / "importance.csv", index_col=0).iloc[:, 0],
        "meta": json.loads((result_dir / "meta.json").read_text()),
    }


def _by_fold(ax, folds: pd.DataFrame, metric: str, ylabel: str) -> None:
    for model in (NAIVE, LGBM):
        g = folds[folds["model"] == model]
        ax.plot(g["month"], g[metric], marker="o", ms=3, color=COLORS[model], label=LABELS[model])
    ax.set_ylabel(ylabel)
    ax.tick_params(axis="x", rotation=60)
    ax.legend(frameon=False)


def fig_pinball(folds: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 3.2))
    _by_fold(ax, folds, "pinball_mean", "Mean pinball loss (£/MWh)")
    ax.set_title("Walk-forward pinball loss by test month (lower is better)")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_coverage(folds: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 3.2))
    _by_fold(ax, folds, "coverage", "P10-P90 coverage")
    ax.axhline(0.8, color="black", lw=1, ls="--", label="Nominal 80%")
    ax.set_ylim(0, 1)
    ax.legend(frameon=False, loc="lower left")
    ax.set_title("Interval coverage by test month")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _fan(ax, week: pd.DataFrame, title: str) -> None:
    lg = week[week["model"] == LGBM].sort_values("start_time_utc")
    nv = week[week["model"] == NAIVE].sort_values("start_time_utc")
    t = lg["start_time_utc"]
    ax.fill_between(t, lg["p10"], lg["p90"], color=COLORS[LGBM], alpha=0.2, label="P10-P90")
    ax.plot(t, lg["p50"], color=COLORS[LGBM], lw=1.2, label="P50 (LightGBM)")
    ax.plot(nv["start_time_utc"], nv["p50"], color=COLORS[NAIVE], lw=1, ls="--", label="Baseline")
    ax.plot(t, lg["price_gbp_mwh"], color="black", lw=1, label="Actual MID")
    ax.set_title(title)
    ax.set_ylabel("£/MWh")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %d %b"))


def fig_fan(pred: pd.DataFrame, path: Path) -> tuple[str, str]:
    p = pred.copy()
    p["week"] = p["start_time_utc"].dt.to_period("W-SUN")
    lg = p[p["model"] == LGBM]
    counts = lg.groupby("week").size()
    full_weeks = counts[counts >= 7 * 46].index
    recent = full_weeks.max()
    others = [w for w in full_weeks if w != recent]
    volatile = lg[lg["week"].isin(others)].groupby("week")["price_gbp_mwh"].std().idxmax()
    fig, axes = plt.subplots(2, 1, figsize=(9, 6))
    _fan(axes[0], p[p["week"] == recent], f"Most recent full week ({recent})")
    _fan(axes[1], p[p["week"] == volatile], f"Most volatile earlier week ({volatile})")
    axes[0].legend(frameon=False, ncol=4, loc="upper left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return str(recent), str(volatile)


def fig_importance(importance: pd.Series, path: Path, top: int = 20) -> None:
    imp = importance.head(top)[::-1]
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.barh(imp.index, imp.to_numpy(), color=COLORS[LGBM])
    ax.set_xlabel("Share of total gain (P50 model, final fold)")
    ax.set_title("Feature importance")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_calibration(summary: pd.DataFrame, path: Path) -> None:
    s = summary[summary["scope"] == "all"].set_index("model")
    nominal = [0.1, 0.5, 0.9]
    cols = ["frac_below_p10", "frac_below_p50", "frac_below_p90"]
    fig, ax = plt.subplots(figsize=(4, 4))
    ax.plot([0, 1], [0, 1], color="black", lw=1, ls="--", label="Perfect")
    for model in (NAIVE, LGBM):
        ax.plot(nominal, s.loc[model, cols], marker="o", color=COLORS[model], label=LABELS[model])
    ax.set_xlabel("Nominal quantile")
    ax.set_ylabel("Observed fraction below")
    ax.set_title("Quantile calibration (all folds)")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def write_figures(result_dir: Path, fig_dir: Path) -> dict:
    out = load_outputs(result_dir)
    fig_dir.mkdir(parents=True, exist_ok=True)
    fig_pinball(out["folds"], fig_dir / "pinball_by_fold.png")
    fig_coverage(out["folds"], fig_dir / "coverage_by_fold.png")
    recent, volatile = fig_fan(out["predictions"], fig_dir / "fan_chart.png")
    fig_importance(out["importance"], fig_dir / "feature_importance.png")
    fig_calibration(out["summary"], fig_dir / "calibration.png")
    return {"recent_week": recent, "volatile_week": volatile}


def _fmt_table(df: pd.DataFrame, cols: dict[str, str], fmt: dict[str, str]) -> str:
    head = "| " + " | ".join(cols.values()) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    rows = []
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            cells.append(fmt.get(c, "{}").format(v) if pd.notna(v) else "")
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *rows])


def write_markdown(result_dir: Path, report_path: Path, fig_rel: str = "figures") -> None:
    out = load_outputs(result_dir)
    meta = out["meta"]
    s = out["summary"].copy()
    s["model"] = s["model"].map(LABELS)
    scope_names = {"all": "All folds", "selection": "Selection folds", "holdout": "Hold-out folds"}
    s["scope"] = s["scope"].map(scope_names)
    cols = {
        "scope": "Scope",
        "model": "Model",
        "n": "Half-hours",
        "pinball_mean": "Pinball",
        "pinball_skill_vs_baseline": "Skill vs baseline",
        "mae_p50": "MAE P50",
        "rmse_p50": "RMSE P50",
        "coverage": "P10-P90 coverage",
        "interval_width": "Interval width",
    }
    fmt = {
        "n": "{:,.0f}",
        "pinball_mean": "{:.2f}",
        "pinball_skill_vs_baseline": "{:.1%}",
        "mae_p50": "{:.2f}",
        "rmse_p50": "{:.2f}",
        "coverage": "{:.1%}",
        "interval_width": "{:.1f}",
    }
    order = {name: i for i, name in enumerate(scope_names.values())}
    s = s.sort_values(["scope", "model"], key=lambda c: c.map(order) if c.name == "scope" else c)
    headline = _fmt_table(s, cols, fmt)

    f = out["folds"]
    wide = f.pivot_table(
        index="month", columns="model", values=["pinball_mean", "coverage", "mae_p50"]
    )
    wide.columns = [f"{m}_{k}" for m, k in wide.columns]
    wide = wide.reset_index()
    fold_cols = {
        "month": "Test month",
        f"pinball_mean_{NAIVE}": "Pinball (baseline)",
        f"pinball_mean_{LGBM}": "Pinball (LightGBM)",
        f"mae_p50_{NAIVE}": "MAE (baseline)",
        f"mae_p50_{LGBM}": "MAE (LightGBM)",
        f"coverage_{NAIVE}": "Coverage (baseline)",
        f"coverage_{LGBM}": "Coverage (LightGBM)",
    }
    fold_fmt = {k: ("{:.1%}" if "coverage" in k else "{:.2f}") for k in fold_cols if k != "month"}
    fold_table = _fmt_table(wide, fold_cols, fold_fmt)

    all_rows = out["summary"][out["summary"]["scope"] == "all"].set_index("model")
    lg, nv = all_rows.loc[LGBM], all_rows.loc[NAIVE]
    top = ", ".join(f"`{k}`" for k in out["importance"].head(5).index)

    run_id = meta.get("mlflow_run_id", "n/a")
    text = f"""# Backtest report

_Generated by `elec backtest` on {meta["generated_at"]}. MLflow run `{run_id}`._

## Setup

- **Target:** Elexon Market Index Price (APXMIDP), £/MWh, half-hourly. It is
  not the day-ahead auction price, which is not freely available (see DECISIONS.md).
- **Decision cutoff:** 09:00 UK time on the day before delivery. Every feature
  comes from `mart_features`, whose point-in-time dbt test guarantees nothing
  was issued after the cutoff.
- **Walk-forward:** {meta["n_folds"]} expanding-window folds, one per calendar
  month from {meta["first_test_month"]} to {meta["last_test_month"]}. Models
  are refitted each fold on every delivery day up to two days before the test
  month, with no shuffling. Training data starts 2024-03-01.
- **Model selection:** every modelling choice was made on folds
  1-{meta["selection_folds"]} (the target transform and calibration variant on
  folds 1-6 first; the model comparison and the pre-registered experiments on
  all of them, see [experiments.md](experiments.md)). Later folds are reported
  separately as out-of-sample: no choice was made on them.
- **Models:**
  - *Seasonal naive baseline:* P50 is the price at the same UK clock time one
    week earlier. P10/P90 add empirical residual quantiles per local hour.
  - *LightGBM quantile:* one model per quantile predicting price minus its
    trailing 7-day mean, then calibrated with a conformal shift estimated on
    the last 56 days of each training window.

## Headline results

{headline}

Across all folds the LightGBM model cuts mean pinball loss by
**{lg["pinball_skill_vs_baseline"]:.0%}** versus the baseline. MAE of the P50
falls from £{nv["mae_p50"]:.2f} to **£{lg["mae_p50"]:.2f}/MWh**. The P10-P90
interval covers **{lg["coverage"]:.1%}** of outcomes (nominal 80%) while being
{1 - lg["interval_width"] / nv["interval_width"]:.0%} narrower than the
baseline's.

![Pinball loss by fold]({fig_rel}/pinball_by_fold.png)

![Coverage by fold]({fig_rel}/coverage_by_fold.png)

## Forecasts versus outcomes

![Fan charts]({fig_rel}/fan_chart.png)

## Calibration

The uncalibrated quantile models covered only about 55% of outcomes with their
P10-P90 interval. Quantile gradient-boosted models are overconfident out of
sample. The conformal shift brings coverage close to nominal and also lowers
pinball loss (see DECISIONS.md for the comparison).

![Calibration]({fig_rel}/calibration.png)

## What drives the forecast

Top features by gain: {top}.

![Feature importance]({fig_rel}/feature_importance.png)

## Per-fold metrics

{fold_table}

## Caveats

- There is no gas price input. Gas sets the marginal price most of the time, and
  no free API offers a point-in-time series. The model tracks the price level
  through recent-price features instead, which lags sharp moves in gas.
  Months where the level shifts (for example 2026-09) score worst.
- MID is a short-term traded index, so its volatility is not identical to the
  day-ahead auction's.
- Monthly refitting approximates the weekly retrain in production.
"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text)
