"""Pre-registered experiments on the selection folds: ``elec experiments``.

Every experiment is one change to the current model, motivated by a pattern in
``reports/data_patterns.md``. Each is scored on the selection folds only
(2025-03 to 2026-02 with the current config); the frame is cut at the first
hold-out day before anything is computed, so the hold-out cannot influence an
adoption. The rule (``adopt``) was fixed in DECISIONS.md before the first run.

The same fold loop, scoring and paired bootstrap serve the model comparison in
``compare.py``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from elecprice.config import REPO_ROOT
from elecprice.logging_utils import get_logger
from elecprice.modelling.backtest import Fold, assert_no_leakage, make_folds
from elecprice.modelling.data import FEATURES, TARGET, ModelConfig, load_frame
from elecprice.modelling.metrics import evaluate, pinball_loss, qcol
from elecprice.modelling.models import QuantileLGBM
from elecprice.modelling.patterns import hour_group, selection_end

log = get_logger(__name__)

# The adoption rule, fixed before any experiment ran (DECISIONS.md, 2026-09-26).
BOOTSTRAP = 2000
BLOCK_DAYS = 7
MIN_FOLDS_BETTER = 9
COVERAGE_RANGE = (0.77, 0.83)

# ---------------------------------------------------------------------------
# Candidate features. Each uses only settled days up to D-2 or forecasts that
# are already in the mart (known at the cutoff), so they are point-in-time by
# construction; tests/test_experiments.py checks it by perturbing later prices.
# ---------------------------------------------------------------------------

MERIT_DAYS = 14
PROFILE_DAYS = 7
REGIME_MARKERS = ["nuclear_share_7d", "demand_outturn_7d_mean_mw", "net_imports_7d_mean_mw"]


def _slot(df: pd.DataFrame) -> pd.Series:
    """UK local half-hour of the day (0-47), so a slot is the same clock time across DST."""
    local = (
        pd.to_datetime(df["start_time_utc"]).dt.tz_localize("UTC").dt.tz_convert("Europe/London")
    )
    return local.dt.hour * 2 + local.dt.minute // 30


def _by_day(df: pd.DataFrame, values: pd.DataFrame) -> pd.DataFrame:
    """Daily aggregates reindexed to a gap-free calendar, so shift(n) means n days."""
    days = pd.date_range(df["settlement_date"].min(), df["settlement_date"].max(), freq="D")
    return values.reindex(days)


def same_period_profile(df: pd.DataFrame, days: int = PROFILE_DAYS) -> pd.Series:
    """Mean price at the same UK clock time over days D-2-(days-1) to D-2."""
    slot = _slot(df)
    wide = df.assign(slot=slot).pivot_table(
        index="settlement_date", columns="slot", values=TARGET, aggfunc="mean"
    )
    wide = _by_day(df, wide)
    rolled = wide.shift(2).rolling(days, min_periods=max(2, days // 2)).mean()
    long = rolled.stack(future_stack=True)  # noqa: PD013  (date x slot -> one series)
    idx = pd.MultiIndex.from_arrays([df["settlement_date"], slot])
    return pd.Series(long.reindex(idx).to_numpy(), index=df.index)


def merit_order(df: pd.DataFrame, days: int = MERIT_DAYS) -> pd.DataFrame:
    """OLS of price on forecast residual demand (GW) over days D-2-(days-1) to D-2.

    Returns the slope and the price that curve implies for each row's own
    residual demand forecast.
    """
    x = df["residual_demand_mw"] / 1000
    y = df[TARGET]
    ok = x.notna() & y.notna()
    parts = pd.DataFrame(
        {
            "n": ok.astype(float),
            "sx": x.where(ok, 0.0),
            "sy": y.where(ok, 0.0),
            "sxx": (x * x).where(ok, 0.0),
            "sxy": (x * y).where(ok, 0.0),
        }
    )
    daily = _by_day(df, parts.groupby(df["settlement_date"]).sum()).fillna(0.0)
    s = daily.shift(2).rolling(days, min_periods=days // 2).sum()
    denom = s["n"] * s["sxx"] - s["sx"] ** 2
    slope = (s["n"] * s["sxy"] - s["sx"] * s["sy"]) / denom.where(denom > 0)
    intercept = (s["sy"] - slope * s["sx"]) / s["n"].where(s["n"] > 0)
    d = df["settlement_date"]
    b, a = slope.reindex(d).to_numpy(), intercept.reindex(d).to_numpy()
    return pd.DataFrame({"merit_slope": b, "merit_implied": a + b * x.to_numpy()}, index=df.index)


def renewable_share(df: pd.DataFrame) -> pd.Series:
    """Forecast wind and embedded solar as a share of demand, all as of the cutoff."""
    supply = df["windfor_mw"] + df["emb_wind_mw"] + df["emb_solar_mw"]
    return supply / (df["ndf_demand_mw"] + df["emb_wind_mw"] + df["emb_solar_mw"])


def add_candidates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["price_profile_7d"] = same_period_profile(df)
    out["price_profile_7d_rel"] = out["price_profile_7d"] - out["price_7d_mean"]
    mo = merit_order(df)
    out["merit_slope"] = mo["merit_slope"]
    out["merit_implied_rel"] = mo["merit_implied"] - out["price_7d_mean"]
    out["renewable_share"] = renewable_share(df)
    return out


# ---------------------------------------------------------------------------
# Model variants. Each is the current QuantileLGBM with one thing changed.
# ---------------------------------------------------------------------------


class WeightedLGBM(QuantileLGBM):
    """Training rows weighted by recency: a row ``half_life_days`` old counts half."""

    def __init__(self, *args, half_life_days: float = 180.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.half_life_days = half_life_days

    def _fit_boosters(self, train: pd.DataFrame):
        import lightgbm as lgb

        age = (train["settlement_date"].max() - train["settlement_date"]).dt.days.to_numpy()
        w = 0.5 ** (age / self.half_life_days)
        X = train[self.features]
        y = train[TARGET].to_numpy(dtype=float) - self._anchor(train)
        boosters = {}
        for q in self.quantiles:
            model = lgb.LGBMRegressor(objective="quantile", alpha=q, **self.params)
            model.fit(X, y, sample_weight=w)
            boosters[q] = model.booster_
        return boosters


class WindowLGBM(QuantileLGBM):
    """Trained on the last ``window_days`` only, instead of the expanding window."""

    def __init__(self, *args, window_days: int = 365, **kwargs):
        super().__init__(*args, **kwargs)
        self.window_days = window_days

    def fit(self, df: pd.DataFrame) -> WindowLGBM:
        last = df.loc[df[TARGET].notna(), "settlement_date"].max()
        return super().fit(df[df["settlement_date"] > last - pd.Timedelta(days=self.window_days)])


class GroupCalibratedLGBM(QuantileLGBM):
    """Conformal shifts estimated per time-of-day group instead of once."""

    def fit(self, df: pd.DataFrame) -> GroupCalibratedLGBM:
        train = df[df[TARGET].notna() & df["price_7d_mean"].notna()]
        last = train["settlement_date"].max()
        cal_start = last - pd.Timedelta(days=self.calibration_days - 1)
        proper = train[train["settlement_date"] <= cal_start - pd.Timedelta(days=self.gap_days)]
        cal = train[train["settlement_date"] >= cal_start]
        raw = self._raw_predict(self._fit_boosters(proper), cal)
        resid = cal[TARGET].to_numpy(dtype=float)[:, None] - raw
        groups = hour_group(cal["local_hour"]).to_numpy()
        self.group_shifts_ = {
            g: {
                q: float(np.quantile(resid[groups == g, i], q))
                for i, q in enumerate(self.quantiles)
            }
            for g in np.unique(groups)
        }
        self.shifts_ = {q: float(np.quantile(resid[:, i], q)) for i, q in enumerate(self.quantiles)}
        self.boosters_ = self._fit_boosters(train)
        return self

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        raw = self._raw_predict(self.boosters_, df)
        groups = hour_group(df["local_hour"]).to_numpy()
        shift = np.array(
            [[self.group_shifts_.get(g, self.shifts_)[q] for q in self.quantiles] for g in groups]
        )
        pred = np.sort(raw + shift, axis=1)
        return pd.DataFrame(pred, columns=[qcol(q) for q in self.quantiles], index=df.index)


def current_model(config: ModelConfig, cls=QuantileLGBM, features=None, params=None, **kw):
    """The production QuantileLGBM as configured, with optional overrides."""
    cal = dict(config.calibration)
    kw.setdefault("calibration_days", cal.get("days", 56))
    return cls(
        config.quantiles,
        {**config.lightgbm, **(params or {})},
        config.target_mode,
        features=features or FEATURES,
        calibrate=cal.get("mode", "none"),
        gap_days=config.backtest["gap_days"],
        **kw,
    )


def _monotone(features: list[str]) -> dict:
    return {"monotone_constraints": [1 if f == "residual_demand_mw" else 0 for f in features]}


@dataclass(frozen=True)
class Experiment:
    name: str
    pattern: str
    change: str
    build: Callable[[ModelConfig], Any]


def experiments(markers: list[str] | None = None) -> list[Experiment]:
    markers = markers or REGIME_MARKERS
    plus = lambda *cols: FEATURES + list(cols)  # noqa: E731
    return [
        Experiment(
            "merit_order",
            "2. The level drifts, and the merit order moves with it",
            f"add the slope of price on residual demand over the last {MERIT_DAYS} settled days, "
            "and the price that curve implies for each half-hour (relative to the 7-day mean)",
            lambda c: current_model(c, features=plus("merit_slope", "merit_implied_rel")),
        ),
        Experiment(
            "recency_weight",
            "2. The level drifts, and the merit order moves with it",
            "weight training rows by recency (half-life 180 days)",
            lambda c: current_model(c, cls=WeightedLGBM, half_life_days=180.0),
        ),
        Experiment(
            "rolling_365d",
            "2. The level drifts, and the merit order moves with it",
            "train on the last 365 days instead of the expanding window",
            lambda c: current_model(c, cls=WindowLGBM, window_days=365),
        ),
        Experiment(
            "monotone_residual_demand",
            "3. Residual demand is the strongest single signal",
            "constrain every tree to be non-decreasing in residual demand",
            lambda c: current_model(c, params=_monotone(FEATURES)),
        ),
        Experiment(
            "same_period_profile",
            "4. The daily shape repeats",
            f"add the same half-hour's mean price over the last {PROFILE_DAYS} settled days "
            "(level and relative to the 7-day mean)",
            lambda c: current_model(c, features=plus("price_profile_7d", "price_profile_7d_rel")),
        ),
        Experiment(
            "renewable_share",
            "6. Renewables set the bottom of the distribution",
            "add the forecast wind and solar share of demand",
            lambda c: current_model(c, features=plus("renewable_share")),
        ),
        Experiment(
            "calibration_28d",
            "7. Volatility persists for days, not weeks",
            "estimate the conformal shifts on the last 28 days instead of 56",
            lambda c: current_model(c, calibration_days=28),
        ),
        Experiment(
            "calibration_by_hour",
            "8. Where the current model misses",
            "estimate the conformal shifts per time-of-day group",
            lambda c: current_model(c, cls=GroupCalibratedLGBM),
        ),
        Experiment(
            "drop_regime_markers",
            "9. The inputs drift between years",
            "drop " + ", ".join(f"`{m}`" for m in markers),
            lambda c: current_model(c, features=[f for f in FEATURES if f not in markers]),
        ),
    ]


# ---------------------------------------------------------------------------
# Fold loop, scoring and the rule.
# ---------------------------------------------------------------------------


def selection_data(config: ModelConfig, df: pd.DataFrame | None = None) -> pd.DataFrame:
    """The modelling frame cut at the first hold-out day, with the candidate features."""
    end = selection_end(config)
    df = load_frame() if df is None else df
    df = df[df["settlement_date"] < end].reset_index(drop=True)
    assert df["settlement_date"].max() < end
    return add_candidates(df)


def folds_for(df: pd.DataFrame, config: ModelConfig, holdout: bool = False) -> list[Fold]:
    known = df.loc[df[TARGET].notna(), "settlement_date"].max().date()
    folds = make_folds(known, config.backtest["first_test_month"], config.backtest["gap_days"])
    n = int(config.backtest["selection_folds"])
    return folds[n:] if holdout else folds[:n]


def run_folds(build: Callable[[], Any], df: pd.DataFrame, folds: list[Fold], quantiles) -> dict:
    cols = [qcol(q) for q in quantiles]
    parts, seconds = [], 0.0
    for fold in folds:
        train = df[df["settlement_date"].dt.date <= fold.train_end]
        test = df[
            (df["settlement_date"].dt.date >= fold.test_start)
            & (df["settlement_date"].dt.date <= fold.test_end)
            & df[TARGET].notna()
        ]
        assert_no_leakage(train, test)
        model = build()
        t0 = time.perf_counter()
        model.fit(train)
        p = model.predict(test)
        seconds += time.perf_counter() - t0
        frame = test[["settlement_date", "settlement_period", "local_hour", TARGET]].copy()
        frame[cols] = p[cols].to_numpy()
        frame["fold"] = fold.index
        parts.append(frame)
    return {"predictions": pd.concat(parts, ignore_index=True), "fit_seconds": seconds}


def row_pinball(pred: pd.DataFrame, quantiles) -> pd.Series:
    y = pred[TARGET].to_numpy(dtype=float)
    losses = []
    for q in quantiles:
        diff = y - pred[qcol(q)].to_numpy(dtype=float)
        losses.append(np.maximum(q * diff, (q - 1) * diff))
    return pd.Series(np.mean(losses, axis=0), index=pred.index)


def block_bootstrap_mean(
    daily: np.ndarray, block: int = BLOCK_DAYS, n: int = BOOTSTRAP, seed: int = 0
):
    """95% moving-block bootstrap interval for the mean of a daily series."""
    rng = np.random.default_rng(seed)
    days = len(daily)
    k = int(np.ceil(days / block))
    starts = rng.integers(0, days - block + 1, size=(n, k))
    idx = (starts[:, :, None] + np.arange(block)).reshape(n, -1)[:, :days]
    means = daily[idx].mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def summarise(pred: pd.DataFrame, quantiles) -> dict:
    m = evaluate(pred[TARGET], pred, quantiles)
    return {
        k: m[k] for k in ("n", "pinball_mean", "mae_p50", "rmse_p50", "coverage", "interval_width")
    }


def paired(base: pd.DataFrame, other: pd.DataFrame, quantiles) -> dict:
    """``other`` against ``base`` on the same half-hours: positive means ``other`` is better."""
    key = ["settlement_date", "settlement_period"]
    merged = (
        base[[*key, "fold"]]
        .assign(b=row_pinball(base, quantiles))
        .merge(other[key].assign(o=row_pinball(other, quantiles)), on=key, validate="one_to_one")
    )
    assert len(merged) == len(base) == len(other), "the two runs scored different half-hours"
    daily = merged.groupby("settlement_date")[["b", "o"]].mean()
    gain = (daily["b"] - daily["o"]).to_numpy()
    lo, hi = block_bootstrap_mean(gain)
    by_fold = merged.groupby("fold")[["b", "o"]].mean()
    return {
        "days": len(daily),
        "mean_daily_gain": float(gain.mean()),
        "relative_gain": float(gain.mean() / daily["b"].mean()),
        "gain_interval": [lo, hi],
        "folds_better": int((by_fold["o"] < by_fold["b"]).sum()),
        "folds": len(by_fold),
        "by_fold": [
            {"fold": int(f), "base": float(r["b"]), "other": float(r["o"])}
            for f, r in by_fold.iterrows()
        ],
    }


def adopt(cmp: dict, coverage: float) -> bool:
    lo = cmp["gain_interval"][0]
    return (
        lo > 0
        and cmp["folds_better"] >= MIN_FOLDS_BETTER
        and COVERAGE_RANGE[0] <= coverage <= COVERAGE_RANGE[1]
    )


def _run_one(name: str, build, df, folds, quantiles) -> tuple[str, dict]:
    """One variant over all folds; a variant that cannot run is recorded, not fatal."""
    t0 = time.perf_counter()
    try:
        out = run_folds(build, df, folds, quantiles)
    except Exception as exc:  # a library refusing a setting is itself a result
        log.warning("%s: could not run: %s", name, exc)
        return name, {"error": f"{type(exc).__name__}: {exc}"}
    log.info("%s: %d folds in %.0f s", name, len(folds), time.perf_counter() - t0)
    return name, out


def run_many(builds: dict[str, Callable[[], Any]], df, folds, quantiles, workers: int = 3) -> dict:
    from joblib import Parallel, delayed

    done = Parallel(n_jobs=workers, backend="loky")(
        delayed(_run_one)(name, build, df, folds, quantiles) for name, build in builds.items()
    )
    return dict(done)


def _combination(config: ModelConfig, adopted: list[str], markers: list[str]) -> Callable:
    """All adopted changes at once (features added or dropped, calibration, weighting)."""
    feats = list(FEATURES)
    extra = {
        "merit_order": ["merit_slope", "merit_implied_rel"],
        "same_period_profile": ["price_profile_7d", "price_profile_7d_rel"],
        "renewable_share": ["renewable_share"],
    }
    for name in adopted:
        feats += extra.get(name, [])
    if "drop_regime_markers" in adopted:
        feats = [f for f in feats if f not in markers]
    kw: dict[str, Any] = {"features": feats}
    if "monotone_residual_demand" in adopted:
        kw["params"] = _monotone(feats)
    if "calibration_28d" in adopted:
        kw["calibration_days"] = 28
    classes = [
        c
        for n, c in (
            ("recency_weight", WeightedLGBM),
            ("rolling_365d", WindowLGBM),
            ("calibration_by_hour", GroupCalibratedLGBM),
        )
        if n in adopted
    ]
    if len(classes) > 1:
        raise ValueError(f"cannot combine {classes}; run the combination by hand")
    if classes:
        kw["cls"] = classes[0]
        if classes[0] is WeightedLGBM:
            kw["half_life_days"] = 180.0
        if classes[0] is WindowLGBM:
            kw["window_days"] = 365
    return partial(current_model, config, **kw)


def run(out_dir: Path | None = None, workers: int = 3, only: list[str] | None = None) -> dict:
    out_dir = out_dir or REPO_ROOT / "reports"
    config = ModelConfig.load()
    markers = REGIME_MARKERS
    pj = out_dir / "data_patterns.json"
    if pj.exists():
        markers = json.loads(pj.read_text())["adversarial"].get("markers") or markers
    df = selection_data(config)
    folds = folds_for(df, config)
    q = config.quantiles
    exps = [e for e in experiments(markers) if not only or e.name in only]
    builds = {"baseline": partial(current_model, config)}
    builds |= {e.name: partial(e.build, config) for e in exps}
    runs = run_many(builds, df, folds, q, workers)
    base = runs["baseline"]["predictions"]
    results = [
        {
            "name": "baseline",
            "pattern": "",
            "change": "the current model",
            **summarise(base, q),
            "fit_seconds": runs["baseline"]["fit_seconds"],
        }
    ]
    if "error" in runs["baseline"]:
        raise RuntimeError(f"the baseline did not run: {runs['baseline']['error']}")
    for e in exps:
        if "error" in runs[e.name]:
            results.append(
                {
                    "name": e.name,
                    "pattern": e.pattern,
                    "change": e.change,
                    "error": runs[e.name]["error"],
                    "adopted": False,
                }
            )
            continue
        pred = runs[e.name]["predictions"]
        s = summarise(pred, q)
        cmp = paired(base, pred, q)
        results.append(
            {
                "name": e.name,
                "pattern": e.pattern,
                "change": e.change,
                **s,
                "fit_seconds": runs[e.name]["fit_seconds"],
                **cmp,
                "adopted": adopt(cmp, s["coverage"]),
            }
        )
    adopted = [r["name"] for r in results if r.get("adopted")]
    combo = None
    if len(adopted) > 1:
        _, out = _run_one("combination", _combination(config, adopted, markers), df, folds, q)
        s = summarise(out["predictions"], q)
        cmp = paired(base, out["predictions"], q)
        combo = {"members": adopted, **s, **cmp, "adopted": adopt(cmp, s["coverage"])}
    res = {
        "folds": [f"{f.label}" for f in folds],
        "last_day": f"{df['settlement_date'].max():%Y-%m-%d}",
        "holdout_from": f"{selection_end(config):%Y-%m-%d}",
        "rule": {
            "bootstrap": BOOTSTRAP,
            "block_days": BLOCK_DAYS,
            "min_folds_better": MIN_FOLDS_BETTER,
            "coverage_range": COVERAGE_RANGE,
        },
        "markers": markers,
        "experiments": results,
        "adopted": adopted,
        "combination": combo,
    }
    (out_dir / "experiments.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
    from elecprice.modelling.experiments_report import write

    write(res, out_dir)
    return res


def pinball_check(pred: pd.DataFrame, quantiles) -> float:
    """Mean pinball over quantiles, as metrics.evaluate computes it (used in tests)."""
    y = pred[TARGET].to_numpy(dtype=float)
    return float(np.mean([pinball_loss(y, pred[qcol(q)].to_numpy(), q) for q in quantiles]))


def holdout(out_dir: Path | None = None, members: list[str] | None = None) -> dict:
    """Read the hold-out once: the adopted configuration against the current model.

    ``members`` defaults to what ``reports/experiments.json`` adopted (the passing
    combination, or the single best change). The result is appended to that file
    and to ``reports/experiments.md``, with the time it was read.
    """
    from datetime import UTC, datetime

    from elecprice.modelling.experiments_report import write

    out_dir = out_dir or REPO_ROOT / "reports"
    res = json.loads((out_dir / "experiments.json").read_text())
    if res.get("holdout"):
        raise RuntimeError(f"the hold-out was already read at {res['holdout']['read_at']}")
    members = members or final_members(res)
    if not members:
        raise RuntimeError("nothing was adopted, so there is nothing to test on the hold-out")
    config = ModelConfig.load()
    df = add_candidates(load_frame())
    folds = folds_for(df, config, holdout=True)
    q = config.quantiles
    runs = run_many(
        {
            "current": partial(current_model, config),
            "adopted": _combination(config, members, res["markers"]),
        },
        df,
        folds,
        q,
        workers=2,
    )
    base, new = runs["current"]["predictions"], runs["adopted"]["predictions"]
    cmp = paired(base, new, q)
    res["holdout"] = {
        "read_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "members": members,
        "folds": [f.label for f in folds],
        "n": len(base),
        "base": summarise(base, q),
        "new": summarise(new, q),
        **{k: cmp[k] for k in ("relative_gain", "gain_interval", "folds_better", "by_fold")},
        "folds_n": cmp["folds"],
    }
    (out_dir / "experiments.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
    write(res, out_dir)
    return res["holdout"]


def final_members(res: dict) -> list[str]:
    """What the rule adopts: the combination if it passes, else the single best change."""
    combo = res.get("combination")
    if combo and combo["adopted"]:
        return list(combo["members"])
    passing = [r for r in res["experiments"] if r.get("adopted")]
    if not passing:
        return []
    return [max(passing, key=lambda r: r["mean_daily_gain"])["name"]]
