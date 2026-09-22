"""Weekly retrain with a champion/challenger gate.

Each week:

1. **Judge.** Score the current ``champion`` and last week's ``challenger`` on
   the latest fold. The fold is the settled days *after* both models' training
   data ended (plus the cutoff gap), so it is out-of-sample for both. The
   challenger has one more week of data; this is where the value of that data
   gets tested.
2. **Promote** the challenger to champion only if its mean pinball loss on
   that fold is strictly lower. Otherwise production is unchanged.
3. **Train** a new challenger on all data to date and register it, to be judged
   next week.

Bootstrap: with no champion, the new model becomes champion directly; with no
challenger, step 1 is skipped. Every decision is logged to MLflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import mlflow
import pandas as pd
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

from elecprice.logging_utils import get_logger
from elecprice.modelling import tracking
from elecprice.modelling.data import TARGET, ModelConfig, complete_days, load_frame
from elecprice.modelling.metrics import evaluate
from elecprice.modelling.models import SeasonalNaive
from elecprice.modelling.runner import TRAINING_EXPERIMENT, fit_final, register_model

log = get_logger(__name__)

CHALLENGER = "challenger"
RETRAIN_EXPERIMENT = "elecprice-retrain"


@dataclass(frozen=True)
class Decision:
    promote: bool
    reason: str


def decide(
    champion_pinball: float | None, challenger_pinball: float | None, n: int, min_n: int
) -> Decision:
    """Pure promotion rule: strictly better pinball on enough out-of-sample periods."""
    if challenger_pinball is None:
        return Decision(False, "no challenger to judge")
    if champion_pinball is None:
        return Decision(True, "no champion: challenger promoted")
    if n < min_n:
        return Decision(False, f"only {n} out-of-sample periods (< {min_n}); keep champion")
    if challenger_pinball < champion_pinball:
        return Decision(
            True, f"challenger {challenger_pinball:.3f} < champion {champion_pinball:.3f}"
        )
    return Decision(
        False, f"challenger {challenger_pinball:.3f} >= champion {champion_pinball:.3f}"
    )


def _load_alias(alias: str):
    try:
        model, version = tracking.load_registered(alias)
    except MlflowException:
        return None, None, None
    mv = MlflowClient().get_model_version(tracking.REGISTERED_MODEL, version)
    trained_through = mv.tags.get("trained_through")
    return model, version, (date.fromisoformat(trained_through) if trained_through else None)


def judgement_fold(df: pd.DataFrame, trained_through: list[date], gap_days: int) -> pd.DataFrame:
    """Settled rows whose cutoff comes after every model's training data ended."""
    start = pd.Timestamp(max(trained_through)) + pd.Timedelta(days=gap_days)
    return df[(df["settlement_date"] >= start) & df[TARGET].notna()]


def retrain(config: ModelConfig | None = None, min_eval_days: int = 5) -> dict:
    config = config or ModelConfig.load()
    gap = config.backtest["gap_days"]
    tracking.configure(TRAINING_EXPERIMENT)
    champion, champ_v, champ_tt = _load_alias(tracking.CHAMPION)
    challenger, chall_v, chall_tt = _load_alias(CHALLENGER)
    df = load_frame()
    result: dict = {"champion_version": champ_v, "challenger_version": chall_v}

    # 1-2. Judge last week's challenger against the champion on unseen days.
    decision = Decision(False, "no challenger to judge")
    if challenger is not None:
        fold = judgement_fold(df, [t for t in (champ_tt, chall_tt) if t], gap)
        n = len(fold)
        chall_m = evaluate(fold[TARGET], challenger.predict(fold)) if n else {}
        champ_m = evaluate(fold[TARGET], champion.predict(fold)) if (n and champion) else {}
        history = df[df["settlement_date"] < fold["settlement_date"].min()] if n else df
        base_m = (
            evaluate(
                fold[TARGET],
                SeasonalNaive(config.quantiles, **config.baseline).fit(history).predict(fold),
            )
            if n
            else {}
        )
        decision = decide(
            champ_m.get("pinball_mean") if champion else None,
            chall_m.get("pinball_mean"),
            n,
            min_eval_days * 46,
        )
        result.update(
            fold_start=str(fold["settlement_date"].min().date()) if n else None,
            fold_end=str(fold["settlement_date"].max().date()) if n else None,
            fold_periods=n,
            champion_pinball=champ_m.get("pinball_mean"),
            challenger_pinball=chall_m.get("pinball_mean"),
            baseline_pinball=base_m.get("pinball_mean"),
        )
        if decision.promote:
            MlflowClient().set_registered_model_alias(
                tracking.REGISTERED_MODEL, tracking.CHAMPION, chall_v
            )
            log.info("promoted challenger v%s to champion: %s", chall_v, decision.reason)
        else:
            log.info("kept champion v%s: %s", champ_v, decision.reason)
    result.update(promoted=decision.promote, reason=decision.reason)

    # 3. Train next week's challenger on every fully settled day.
    train = complete_days(df)
    new = fit_final(config, train)
    trained_through = train["settlement_date"].max().date()
    tags = {"trained_through": str(trained_through), "role_at_registration": CHALLENGER}
    alias = CHALLENGER
    if champion is None and not decision.promote:
        alias = tracking.CHAMPION  # bootstrap
        tags["role_at_registration"] = tracking.CHAMPION
    sample = train.drop(columns=[TARGET]).tail(48)
    new_v = register_model(new, config, {}, tags, alias=alias, sample=sample)
    result.update(new_version=new_v, new_alias=alias, trained_through=str(trained_through))

    tracking.configure(RETRAIN_EXPERIMENT)
    with mlflow.start_run(run_name=f"retrain-{datetime.now(UTC):%Y%m%d-%H%M}"):
        mlflow.log_params({k: v for k, v in result.items() if not isinstance(v, float)})
        mlflow.log_metrics({k: v for k, v in result.items() if isinstance(v, float)})
    log.info("retrain result: %s", result)
    return result
