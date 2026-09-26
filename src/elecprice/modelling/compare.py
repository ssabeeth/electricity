"""Model families compared on the selection folds: ``elec compare-models``.

Every model sees the same features and folds as the current LightGBM, predicts
the same de-levelled target and gets the same conformal calibration, so the
comparison is of the learner and nothing else. Settings are each library's
usual choices at a similar budget (700 trees, learning rate 0.03 to 0.05,
about 31 leaves), not tuned, because tuning each on these folds would make
the comparison a contest in tuning effort. The hold-out is not read.

Also compares two naive forecasts: the brief's baseline (same half-hour last
week) and the same half-hour two days earlier, which the data patterns show is
the stronger of the two.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd

from elecprice.config import REPO_ROOT
from elecprice.modelling.data import ModelConfig
from elecprice.modelling.experiments import (
    current_model,
    folds_for,
    paired,
    run_many,
    selection_data,
    summarise,
)
from elecprice.modelling.models import QuantileLGBM, SeasonalNaive
from elecprice.modelling.patterns import selection_end

LINEAR_EVERY = 3  # linear quantile regression is fitted on every third training row


class NaiveD2(SeasonalNaive):
    """Same half-hour two days earlier (known at a 09:00 cutoff on D-1)."""

    name = "naive_d2"

    @staticmethod
    def _point(df: pd.DataFrame) -> pd.Series:
        return (
            df["price_d2_same_period"]
            .fillna(df["price_d7_same_period"])
            .fillna(df["price_7d_mean"])
        )


class PointLGBM(QuantileLGBM):
    """One squared-error LightGBM; P10 and P90 are its empirical residual quantiles.

    With calibration mode ``all`` the conformal shifts are exactly the residual
    quantiles on the last 56 days, so this is the classic point model with
    empirical intervals.
    """

    def _fit_boosters(self, train):
        import lightgbm as lgb

        y = train["price_gbp_mwh"].to_numpy(dtype=float) - self._anchor(train)
        model = lgb.LGBMRegressor(objective="regression", **self.params)
        model.fit(train[self.features], y)
        return {"point": model.booster_}

    def _raw_predict(self, boosters, df):
        p = boosters["point"].predict(df[self.features]) + self._anchor(df)
        return np.repeat(p[:, None], len(self.quantiles), axis=1)


class XGBQuantile(QuantileLGBM):
    def _fit_boosters(self, train):
        import xgboost as xgb

        model = xgb.XGBRegressor(
            objective="reg:quantileerror",
            quantile_alpha=np.array(self.quantiles),
            n_estimators=self.params["n_estimators"],
            learning_rate=self.params["learning_rate"],
            max_leaves=self.params["num_leaves"],
            max_depth=0,
            grow_policy="lossguide",
            min_child_weight=1.0,
            subsample=self.params["subsample"],
            colsample_bytree=self.params["colsample_bytree"],
            reg_lambda=self.params["reg_lambda"],
            tree_method="hist",
            n_jobs=self.params["n_jobs"],
            random_state=self.params["random_state"],
        )
        y = train["price_gbp_mwh"].to_numpy(dtype=float) - self._anchor(train)
        model.fit(train[self.features], y)
        return {"model": model}

    def _raw_predict(self, boosters, df):
        return boosters["model"].predict(df[self.features]) + self._anchor(df)[:, None]


class CatQuantile(QuantileLGBM):
    def _fit_boosters(self, train):
        from catboost import CatBoostRegressor

        alphas = ",".join(str(q) for q in self.quantiles)
        model = CatBoostRegressor(
            loss_function=f"MultiQuantile:alpha={alphas}",
            iterations=self.params["n_estimators"],
            learning_rate=0.05,
            depth=6,
            thread_count=self.params["n_jobs"],
            random_seed=self.params["random_state"],
            verbose=False,
            allow_writing_files=False,
        )
        y = train["price_gbp_mwh"].to_numpy(dtype=float) - self._anchor(train)
        model.fit(train[self.features], y)
        return {"model": model}

    def _raw_predict(self, boosters, df):
        return boosters["model"].predict(df[self.features]) + self._anchor(df)[:, None]


class LinearQuantile(QuantileLGBM):
    """Linear quantile regression on standardised, median-imputed inputs."""

    def _fit_boosters(self, train):
        from sklearn.linear_model import QuantileRegressor

        X = train[self.features]
        med = X.median().fillna(0.0)  # a column with no values in this window
        # missing D-1 lags are the rule, not the exception: give the model the indicator
        miss = [c for c in self.features if X[c].isna().mean() > 0.01]
        Z = self._design(X, med, miss)
        mu, sd = Z.mean(), Z.std().fillna(1.0).replace(0, 1.0)
        Z = ((Z - mu) / sd).to_numpy()
        y = train["price_gbp_mwh"].to_numpy(dtype=float) - self._anchor(train)
        rows = slice(None, None, LINEAR_EVERY)
        models = {
            q: QuantileRegressor(quantile=q, alpha=0.0, solver="highs").fit(Z[rows], y[rows])
            for q in self.quantiles
        }
        return {"models": models, "med": med, "miss": miss, "mu": mu, "sd": sd}

    @staticmethod
    def _design(X, med, miss):
        Z = X.fillna(med)
        for c in miss:
            Z[f"{c}_missing"] = X[c].isna().astype(float)
        return Z

    def _raw_predict(self, boosters, df):
        Z = self._design(df[self.features], boosters["med"], boosters["miss"])
        Z = ((Z - boosters["mu"]) / boosters["sd"]).to_numpy()
        anchor = self._anchor(df)
        return np.column_stack([boosters["models"][q].predict(Z) + anchor for q in self.quantiles])


LABELS = {
    "seasonal_naive": "Seasonal naive, same half-hour last week (the brief's baseline)",
    "naive_d2": "Naive, same half-hour two days earlier",
    "linear_quantile": "Linear quantile regression",
    "lgbm_point": "LightGBM point forecast + empirical residual intervals",
    "xgboost_quantile": "XGBoost quantile",
    "catboost_quantile": "CatBoost multi-quantile",
    "lgbm_quantile": "LightGBM quantile (current model)",
}


def builds(config: ModelConfig) -> dict:
    naive = dict(config.baseline)
    return {
        "seasonal_naive": partial(SeasonalNaive, config.quantiles, **naive),
        "naive_d2": partial(NaiveD2, config.quantiles, **naive),
        "linear_quantile": partial(current_model, config, cls=LinearQuantile),
        "lgbm_point": partial(current_model, config, cls=PointLGBM),
        "xgboost_quantile": partial(current_model, config, cls=XGBQuantile),
        "catboost_quantile": partial(current_model, config, cls=CatQuantile),
        "lgbm_quantile": partial(current_model, config),
    }


def run(out_dir: Path | None = None, workers: int = 3) -> dict:
    out_dir = out_dir or REPO_ROOT / "reports"
    config = ModelConfig.load()
    df = selection_data(config)
    folds = folds_for(df, config)
    q = config.quantiles
    runs = run_many(builds(config), df, folds, q, workers)
    ref = runs["lgbm_quantile"]["predictions"]
    naive = summarise(runs["seasonal_naive"]["predictions"], q)["pinball_mean"]
    d2 = summarise(runs["naive_d2"]["predictions"], q)["pinball_mean"]
    rows = []
    for name in LABELS:
        pred = runs[name]["predictions"]
        s = summarise(pred, q)
        row = {
            "name": name,
            "label": LABELS[name],
            **s,
            "fit_seconds": runs[name]["fit_seconds"],
            "skill_vs_seasonal_naive": 1 - s["pinball_mean"] / naive,
            "skill_vs_naive_d2": 1 - s["pinball_mean"] / d2,
            "by_fold": pred.groupby("fold")
            .apply(lambda g: summarise(g, q)["pinball_mean"], include_groups=False)
            .to_dict(),
        }
        if name != "lgbm_quantile":
            # positive: the current LightGBM is better than this model
            row["lgbm_advantage"] = paired(pred, ref, q)
        rows.append(row)
    res = {
        "folds": [f.label for f in folds],
        "holdout_from": f"{selection_end(config):%Y-%m-%d}",
        "models": rows,
    }
    (out_dir / "model_comparison.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
    (out_dir / "model_comparison.md").write_text(markdown(res))
    return res


def markdown(res: dict) -> str:
    by = {r["name"]: r for r in res["models"]}
    lg = by["lgbm_quantile"]
    ranked = sorted(res["models"], key=lambda r: r["pinball_mean"])
    lines = [
        "# Model comparison",
        "",
        f"_Generated by `elec compare-models`. Selection folds {res['folds'][0]} to "
        f"{res['folds'][-1]} ({len(res['folds'])} monthly walk-forward folds, {lg['n']:,} "
        f"half-hours). The hold-out (from {res['holdout_from']}) is not read._",
        "",
        "Every learned model gets the same features, the same de-levelled target (price minus "
        "its trailing 7-day mean) and the same conformal calibration on the last 56 days of "
        "each training window. Pinball loss (£/MWh, mean over P10, P50 and P90) is the "
        "decision metric; lower is better.",
        "",
        "| Model | Pinball | Skill vs last week | Skill vs two days earlier | MAE P50 "
        "| RMSE P50 | P10-P90 coverage | Interval width | Fit time, all folds |",
        "|---|---|---|---|---|---|---|---|---|",
        *[
            f"| {r['label']} | {r['pinball_mean']:.3f} | {r['skill_vs_seasonal_naive']:.1%} "
            f"| {r['skill_vs_naive_d2']:.1%} | £{r['mae_p50']:.2f} | £{r['rmse_p50']:.2f} "
            f"| {r['coverage']:.1%} | £{r['interval_width']:.1f} | {r['fit_seconds']:.0f} s |"
            for r in ranked
        ],
        "",
        "## Is the current model's lead real?",
        "",
        "For each alternative: the current LightGBM's mean daily pinball advantage, with a "
        "95% moving-block bootstrap interval (7-day blocks), and the number of folds it wins.",
        "",
        "| Against | LightGBM's advantage | 95% interval | Folds LightGBM wins |",
        "|---|---|---|---|",
    ]
    for r in ranked:
        if r["name"] == "lgbm_quantile":
            continue
        a = r["lgbm_advantage"]
        lo, hi = a["gain_interval"]
        lines.append(
            f"| {r['label']} | {a['relative_gain']:+.1%} | £{lo:.3f} to £{hi:.3f} "
            f"| {a['folds_better']} of {a['folds']} |"
        )
    lines += ["", "## Pinball by fold", ""]
    names = [r["name"] for r in ranked]
    lines += [
        "| Fold | " + " | ".join(by[n]["label"].split(",")[0].split(" (")[0] for n in names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for i, label in enumerate(res["folds"], start=1):
        cells = [f"{by[n]['by_fold'][i]:.2f}" for n in names]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)
