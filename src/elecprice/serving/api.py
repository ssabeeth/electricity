"""FastAPI service: latest forecasts, backtest metrics and battery simulation results.

Run locally:  uv run uvicorn elecprice.serving.api:app --reload
Docs:         http://localhost:8000/docs
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from elecprice import __version__
from elecprice.battery.simulate import STRATEGIES, STRATEGY_LABELS
from elecprice.serving.data import OutputStore

LGBM, NAIVE = "lgbm_quantile", "seasonal_naive"
MODELS = (LGBM, NAIVE)


# --- response models --------------------------------------------------------


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    outputs: dict[str, bool]
    latest_live_forecast: date | None = None
    champion_version: str | None = None


class ForecastPoint(BaseModel):
    settlement_period: int
    start_time_utc: datetime
    p10: float | None
    p50: float | None
    p90: float | None
    actual: float | None = None


class Forecast(BaseModel):
    delivery_date: date
    source: Literal["live", "backtest"] = Field(
        description="live: issued by the daily pipeline; backtest: walk-forward out-of-sample"
    )
    model: str
    model_version: str | None = None
    cutoff_utc: datetime | None = None
    created_at: datetime | None = None
    points: list[ForecastPoint]
    baseline: list[ForecastPoint] = []


class ForecastRangePoint(ForecastPoint):
    settlement_date: date
    model: str


class SummaryRow(BaseModel):
    model: str
    scope: str
    n: int
    pinball_mean: float
    pinball_skill_vs_baseline: float | None
    mae_p50: float
    rmse_p50: float
    coverage: float
    interval_width: float


class FoldRow(BaseModel):
    fold: int
    month: str
    model: str
    pinball_mean: float
    mae_p50: float
    coverage: float


class BacktestMetrics(BaseModel):
    meta: dict[str, Any]
    summary: list[SummaryRow]
    folds: list[FoldRow]
    feature_importance: dict[str, float]


class CoveragePoint(BaseModel):
    settlement_date: date
    model: str
    coverage_daily: float
    coverage_rolling: float | None


class SimulationRow(BaseModel):
    strategy: str
    label: str
    net_gbp: float
    net_gbp_per_mw_year: float
    capture_vs_perfect: float
    share_of_gap_closed: float
    cycles_per_day: float
    days: int


class SimulationSummary(BaseModel):
    params: dict[str, Any]
    rows: list[SimulationRow]


class SimulationDailyPoint(BaseModel):
    settlement_date: date
    strategy: str
    net_gbp: float
    cumulative_gbp: float


class SchedulePoint(BaseModel):
    settlement_period: int
    start_time_utc: datetime
    charge_mw: float
    discharge_mw: float
    soc_mwh: float
    decision_price: float | None
    actual_price: float | None = None


class Schedule(BaseModel):
    delivery_date: date
    source: Literal["live", "backtest"]
    strategy: str
    points: list[SchedulePoint]


class LiveMetrics(BaseModel):
    forecast_daily: list[dict[str, Any]]
    battery_daily: list[dict[str, Any]]


# --- helpers ----------------------------------------------------------------


def records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame -> JSON-safe dicts (NaN -> None)."""
    if df.empty:
        return []
    clean = df.astype(object).where(pd.notna(df), None)
    return clean.to_dict(orient="records")


def _dates(df: pd.DataFrame) -> pd.DataFrame:
    if not df.empty and "settlement_date" in df:
        df = df.assign(settlement_date=pd.to_datetime(df["settlement_date"]).dt.date)
    return df


def _points(df: pd.DataFrame, actual_col: str | None = None) -> list[ForecastPoint]:
    df = df.sort_values("settlement_period")
    out = []
    for r in records(df):
        out.append(
            ForecastPoint(
                settlement_period=r["settlement_period"],
                start_time_utc=r["start_time_utc"],
                p10=r["p10"],
                p50=r["p50"],
                p90=r["p90"],
                actual=r.get(actual_col) if actual_col else None,
            )
        )
    return out


def create_app(store: OutputStore | None = None) -> FastAPI:
    store = store or OutputStore()
    app = FastAPI(
        title="GB day-ahead electricity price forecasts",
        version=__version__,
        description=(
            "Probabilistic (P10/P50/P90) half-hourly forecasts of the GB Market Index "
            "Price made at a 09:00 UK D-1 cutoff, walk-forward backtest metrics, and "
            "battery arbitrage simulation results."
        ),
    )

    @app.get("/health", response_model=Health, tags=["meta"])
    def health() -> Health:
        live = _dates(store.live_forecasts())
        outputs = {
            "live_forecasts": not live.empty,
            "backtest": not store.backtest_summary().empty,
            "battery_simulation": not store.battery_summary().empty,
        }
        latest = champion = None
        if not live.empty:
            latest = max(live["settlement_date"])
            lg = live[(live["settlement_date"] == latest) & (live["model"] == LGBM)]
            champion = str(lg["model_version"].iloc[0]) if not lg.empty else None
        status = "ok" if outputs["backtest"] and outputs["battery_simulation"] else "degraded"
        return Health(
            status=status,
            version=__version__,
            outputs=outputs,
            latest_live_forecast=latest,
            champion_version=champion,
        )

    def _live_forecast(d: date) -> Forecast | None:
        live = _dates(store.live_forecasts())
        if live.empty:
            return None
        day = live[live["settlement_date"] == d]
        lg = day[day["model"] == LGBM]
        if lg.empty:
            return None
        first = records(lg.head(1))[0]
        return Forecast(
            delivery_date=d,
            source="live",
            model=LGBM,
            model_version=str(first["model_version"]),
            cutoff_utc=first["cutoff_utc"],
            created_at=first["created_at"],
            points=_points(lg),
            baseline=_points(day[day["model"] == NAIVE]),
        )

    def _backtest_forecast(d: date) -> Forecast | None:
        bt = _dates(store.backtest_predictions())
        if bt.empty:
            return None
        day = bt[bt["settlement_date"] == d]
        lg = day[day["model"] == LGBM]
        if lg.empty:
            return None
        return Forecast(
            delivery_date=d,
            source="backtest",
            model=LGBM,
            points=_points(lg, "price_gbp_mwh"),
            baseline=_points(day[day["model"] == NAIVE], "price_gbp_mwh"),
        )

    @app.get("/forecast/latest", response_model=Forecast, tags=["forecasts"])
    def latest_forecast() -> Forecast:
        """The most recent delivery day forecast by the live pipeline
        (falls back to the latest backtest day if the pipeline has not run yet)."""
        live = _dates(store.live_forecasts())
        if not live.empty:
            fc = _live_forecast(max(live["settlement_date"]))
            if fc:
                return fc
        bt = _dates(store.backtest_predictions())
        if bt.empty:
            raise HTTPException(404, "no forecasts available yet")
        return _backtest_forecast(max(bt["settlement_date"]))

    @app.get("/forecast/range", response_model=list[ForecastRangePoint], tags=["forecasts"])
    def forecast_range(
        start: date,
        end: date,
        model: Literal["lgbm_quantile", "seasonal_naive", "all"] = "all",
    ) -> list[ForecastRangePoint]:
        """Backtest (out-of-sample) forecasts with actuals over a date range (max 31 days)."""
        if end < start or (end - start).days > 31:
            raise HTTPException(422, "range must be 0-31 days")
        bt = _dates(store.backtest_predictions())
        if bt.empty:
            return []
        part = bt[(bt["settlement_date"] >= start) & (bt["settlement_date"] <= end)]
        if model != "all":
            part = part[part["model"] == model]
        part = part.sort_values(["model", "start_time_utc"])
        return [
            ForecastRangePoint(
                settlement_date=r["settlement_date"],
                model=r["model"],
                settlement_period=r["settlement_period"],
                start_time_utc=r["start_time_utc"],
                p10=r["p10"],
                p50=r["p50"],
                p90=r["p90"],
                actual=r["price_gbp_mwh"],
            )
            for r in records(part)
        ]

    @app.get("/forecast/{delivery_date}", response_model=Forecast, tags=["forecasts"])
    def forecast_for(delivery_date: date) -> Forecast:
        """Live forecast for a delivery day if issued, else its walk-forward backtest forecast."""
        fc = _live_forecast(delivery_date) or _backtest_forecast(delivery_date)
        if fc is None:
            raise HTTPException(404, f"no forecast for {delivery_date}")
        return fc

    @app.get("/backtest/metrics", response_model=BacktestMetrics, tags=["backtest"])
    def backtest_metrics() -> BacktestMetrics:
        summary = store.backtest_summary()
        if summary.empty:
            raise HTTPException(404, "backtest has not been run")
        imp = store.backtest_importance()
        importance = {}
        if not imp.empty:
            importance = {
                str(k): float(v) for k, v in imp.set_index(imp.columns[0]).iloc[:20, 0].items()
            }
        return BacktestMetrics(
            meta=store.backtest_meta(),
            summary=[SummaryRow(**r) for r in records(summary[list(SummaryRow.model_fields)])],
            folds=[
                FoldRow(**r) for r in records(store.backtest_folds()[list(FoldRow.model_fields)])
            ],
            feature_importance=importance,
        )

    @app.get("/backtest/coverage", response_model=list[CoveragePoint], tags=["backtest"])
    def coverage_over_time(
        window: int = Query(30, ge=1, le=365, description="rolling window in days"),
    ) -> list[CoveragePoint]:
        """Daily and rolling P10-P90 coverage (nominal 80%) of the out-of-sample forecasts."""
        bt = _dates(store.backtest_predictions())
        if bt.empty:
            return []
        bt = bt[bt["price_gbp_mwh"].notna()]
        bt["covered"] = (bt["price_gbp_mwh"] >= bt["p10"]) & (bt["price_gbp_mwh"] <= bt["p90"])
        daily = bt.groupby(["model", "settlement_date"])["covered"].mean().rename("coverage_daily")
        daily = daily.reset_index().sort_values(["model", "settlement_date"])
        daily["coverage_rolling"] = daily.groupby("model")["coverage_daily"].transform(
            lambda s: s.rolling(window, min_periods=max(1, window // 2)).mean()
        )
        return [CoveragePoint(**r) for r in records(daily)]

    @app.get("/simulation/summary", response_model=SimulationSummary, tags=["battery"])
    def simulation_summary() -> SimulationSummary:
        s = store.battery_summary()
        if s.empty:
            raise HTTPException(404, "battery simulation has not been run")
        s["label"] = s["strategy"].map(STRATEGY_LABELS)
        return SimulationSummary(
            params=store.battery_params(),
            rows=[SimulationRow(**r) for r in records(s[list(SimulationRow.model_fields)])],
        )

    @app.get("/simulation/daily", response_model=list[SimulationDailyPoint], tags=["battery"])
    def simulation_daily() -> list[SimulationDailyPoint]:
        """Daily and cumulative net £ per strategy (for the cumulative revenue chart)."""
        d = _dates(store.battery_daily())
        if d.empty:
            return []
        d = d.sort_values(["strategy", "settlement_date"])
        d["cumulative_gbp"] = d.groupby("strategy")["net_gbp"].cumsum()
        cols = ["settlement_date", "strategy", "net_gbp", "cumulative_gbp"]
        return [SimulationDailyPoint(**r) for r in records(d[cols])]

    @app.get(
        "/simulation/schedule/{delivery_date}", response_model=list[Schedule], tags=["battery"]
    )
    def schedule_for(delivery_date: date) -> list[Schedule]:
        """Battery schedules for a day: the live one if issued, else every backtest strategy."""
        out = []
        live = _dates(store.live_schedules())
        if not live.empty:
            day = live[live["settlement_date"] == delivery_date]
            for strategy, g in day.groupby("strategy"):
                out.append(_schedule(delivery_date, "live", strategy, g))
        if not out:
            sim = _dates(store.battery_schedules())
            if not sim.empty:
                day = sim[sim["settlement_date"] == delivery_date]
                for strategy in STRATEGIES:
                    g = day[day["strategy"] == strategy]
                    if not g.empty:
                        out.append(_schedule(delivery_date, "backtest", strategy, g))
        if not out:
            raise HTTPException(404, f"no schedule for {delivery_date}")
        return out

    @app.get("/live/metrics", response_model=LiveMetrics, tags=["monitoring"])
    def live_metrics() -> LiveMetrics:
        """Live forecast accuracy and battery P&L for days settled since deployment."""
        return LiveMetrics(
            forecast_daily=records(_dates(store.live_forecast_metrics())),
            battery_daily=records(_dates(store.live_battery_daily())),
        )

    return app


def _schedule(d: date, source: str, strategy: str, g: pd.DataFrame) -> Schedule:
    g = g.sort_values("settlement_period")
    pts = []
    for r in records(g):
        pts.append(
            SchedulePoint(
                settlement_period=r["settlement_period"],
                start_time_utc=r["start_time_utc"],
                charge_mw=r["charge_mw"],
                discharge_mw=r["discharge_mw"],
                soc_mwh=r["soc_mwh"],
                decision_price=r.get("decision_price"),
                actual_price=r.get("actual_price"),
            )
        )
    return Schedule(delivery_date=d, source=source, strategy=strategy, points=pts)


app = create_app()

__all__ = ["app", "create_app"]
