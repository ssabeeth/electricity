"""Streamlit dashboard over the FastAPI service.

    uv run streamlit run src/elecprice/serving/dashboard.py

Set ELEC_API_URL to the API base URL (default http://localhost:8000), or to
``inprocess`` to call the API app directly without running a server.
ELEC_RECORD_REPO points the track-record links at the GitHub repository that
publishes the record (its ``track-record`` branch).
"""

from __future__ import annotations

import os
from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

API_URL = os.environ.get("ELEC_API_URL", "http://localhost:8000")
RECORD_REPO = os.environ.get("ELEC_RECORD_REPO", "https://github.com/ssabeeth/electricity")
RECORD_BRANCH = "track-record"
BLUE, GREY, BLACK, ORANGE = "#2a6fdb", "#9a9a9a", "#1b1b1b", "#d9822b"
STRATEGY_COLORS = {
    "perfect_foresight": BLACK,
    "forecast_lgbm": BLUE,
    "forecast_naive": GREY,
    "naive_fixed": ORANGE,
}
STRATEGY_NAMES = {
    "perfect_foresight": "Perfect foresight (upper bound)",
    "forecast_lgbm": "Scheduled on the LightGBM forecast",
    "forecast_naive": "Scheduled on the seasonal-naive forecast",
}


class ApiError(RuntimeError):
    pass


@st.cache_resource
def _client():
    if API_URL == "inprocess":
        from fastapi.testclient import TestClient

        from elecprice.serving.api import create_app

        return TestClient(create_app())
    import httpx

    return httpx.Client(base_url=API_URL, timeout=30)


@st.cache_data(ttl=300, show_spinner=False)
def api(path: str, **params):
    r = _client().get(path, params=params or None)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise ApiError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def to_uk(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts).dt.tz_localize("UTC").dt.tz_convert("Europe/London")


def fan_chart(points: list[dict], baseline: list[dict] | None, title: str) -> go.Figure:
    df = pd.DataFrame(points)
    t = to_uk(df["start_time_utc"])
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(x=t, y=df["p90"], line={"width": 0}, showlegend=False, hoverinfo="skip")
    )
    fig.add_trace(
        go.Scatter(
            x=t,
            y=df["p10"],
            fill="tonexty",
            fillcolor="rgba(42,111,219,0.18)",
            line={"width": 0},
            name="P10-P90",
        )
    )
    fig.add_trace(go.Scatter(x=t, y=df["p50"], line={"color": BLUE, "width": 2}, name="P50"))
    if baseline:
        b = pd.DataFrame(baseline)
        fig.add_trace(
            go.Scatter(
                x=to_uk(b["start_time_utc"]),
                y=b["p50"],
                line={"color": GREY, "dash": "dash", "width": 1},
                name="Seasonal naive",
            )
        )
    if "actual" in df and df["actual"].notna().any():
        fig.add_trace(
            go.Scatter(x=t, y=df["actual"], line={"color": BLACK, "width": 1.2}, name="Actual")
        )
    fig.update_layout(
        title=title,
        yaxis_title="£/MWh",
        height=380,
        margin={"l": 10, "r": 10, "t": 50, "b": 10},
        legend={"orientation": "h", "y": -0.15},
    )
    return fig


def schedule_chart(schedule: dict) -> go.Figure:
    df = pd.DataFrame(schedule["points"])
    t = to_uk(df["start_time_utc"])
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=t, y=df["discharge_mw"] - df["charge_mw"], name="Net export (MW)", marker_color=BLUE
        )
    )
    fig.add_trace(
        go.Scatter(
            x=t, y=df["soc_mwh"], name="State of charge (MWh)", yaxis="y2", line={"color": BLACK}
        )
    )
    fig.update_layout(
        height=300,
        yaxis={"title": "MW (+ discharge / - charge)"},
        yaxis2={"title": "MWh", "overlaying": "y", "side": "right", "rangemode": "tozero"},
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        legend={"orientation": "h", "y": -0.2},
    )
    return fig


def page_forecast() -> None:
    fc = api("/forecast/latest")
    if not fc:
        st.info("No forecasts yet. Run the daily pipeline (`make daily`) or the backtest.")
        return
    d = fc["delivery_date"]
    label = "Live forecast" if fc["source"] == "live" else "Latest backtest forecast"
    c1, c2, c3 = st.columns(3)
    c1.metric("Delivery day", d)
    c2.metric("Source", label)
    c3.metric("Model version", fc.get("model_version") or "walk-forward")
    if fc.get("cutoff_utc"):
        cutoff = to_uk(pd.Series([fc["cutoff_utc"]])).iloc[0]
        st.caption(
            f"Decision cutoff: {cutoff:%a %d %b %Y %H:%M} UK. Nothing issued later was used."
        )
    if fc["source"] == "live":
        path = f"forecasts/{d}.csv"
        blob = f"{RECORD_REPO}/blob/{RECORD_BRANCH}/{path}"
        history = f"{RECORD_REPO}/commits/{RECORD_BRANCH}/{path}"
        st.caption(
            f"Published in the track record as [{path}]({blob}); its [history]({history}) "
            "shows it was committed before the day began."
        )
    st.plotly_chart(fan_chart(fc["points"], fc["baseline"], f"{label} for {d}"), width="stretch")
    schedules = api(f"/simulation/schedule/{d}") or []
    sched = next((s for s in schedules if s["strategy"] == "forecast_lgbm"), None)
    if sched:
        st.subheader("Battery schedule from this forecast (1 MW / 2 MWh)")
        st.plotly_chart(schedule_chart(sched), width="stretch")


def page_backtest() -> None:
    m = api("/backtest/metrics")
    if not m:
        st.info("Backtest not run yet (`make backtest`).")
        return
    s = pd.DataFrame(m["summary"])
    allf = s[s["scope"] == "all"].set_index("model")
    lg, nv = allf.loc["lgbm_quantile"], allf.loc["seasonal_naive"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pinball skill vs baseline", f"{lg['pinball_skill_vs_baseline']:.0%}")
    c2.metric(
        "MAE (P50)",
        f"£{lg['mae_p50']:.1f}",
        f"{lg['mae_p50'] - nv['mae_p50']:.1f} vs baseline",
        delta_color="inverse",
    )
    c3.metric("P10-P90 coverage", f"{lg['coverage']:.1%}", "nominal 80%", delta_color="off")
    c4.metric("Half-hours scored", f"{int(lg['n']):,}")
    meta = m["meta"]
    st.caption(
        f"{meta.get('n_folds')} monthly walk-forward folds, {meta.get('first_test_month')} to "
        f"{meta.get('last_test_month')}. Expanding window; folds 1-{meta.get('selection_folds')} "
        "were used for model selection."
    )

    st.subheader("Forecast fan chart")
    latest = pd.to_datetime(
        pd.DataFrame(api("/backtest/coverage", window=7))["settlement_date"]
    ).max()
    start = st.date_input("Week starting", value=(latest - timedelta(days=6)).date())
    rng = api("/forecast/range", start=str(start), end=str(start + timedelta(days=6)))
    if rng:
        df = pd.DataFrame(rng)
        lgp = df[df["model"] == "lgbm_quantile"].to_dict("records")
        nvp = df[df["model"] == "seasonal_naive"].to_dict("records")
        st.plotly_chart(fan_chart(lgp, nvp, f"Week from {start}"), width="stretch")
    else:
        st.info("No backtest forecasts in that week.")

    st.subheader("Coverage over time")
    window = st.slider("Rolling window (days)", 7, 90, 30)
    cov = pd.DataFrame(api("/backtest/coverage", window=window))
    fig = go.Figure()
    for model, color, name in (
        ("lgbm_quantile", BLUE, "LightGBM"),
        ("seasonal_naive", GREY, "Seasonal naive"),
    ):
        g = cov[cov["model"] == model]
        fig.add_trace(
            go.Scatter(
                x=g["settlement_date"], y=g["coverage_rolling"], name=name, line={"color": color}
            )
        )
    fig.add_hline(y=0.8, line_dash="dash", annotation_text="nominal 80%")
    fig.update_layout(height=320, yaxis={"tickformat": ".0%", "range": [0, 1]}, margin={"t": 20})
    st.plotly_chart(fig, width="stretch")

    st.subheader("Pinball loss by test month")
    folds = pd.DataFrame(m["folds"])
    fig = go.Figure()
    for model, color, name in (
        ("seasonal_naive", GREY, "Seasonal naive"),
        ("lgbm_quantile", BLUE, "LightGBM"),
    ):
        g = folds[folds["model"] == model]
        fig.add_trace(
            go.Scatter(
                x=g["month"],
                y=g["pinball_mean"],
                name=name,
                mode="lines+markers",
                line={"color": color},
            )
        )
    fig.update_layout(height=320, yaxis_title="£/MWh", margin={"t": 20})
    st.plotly_chart(fig, width="stretch")

    with st.expander("Feature importance and full summary"):
        imp = pd.Series(m["feature_importance"]).sort_values()
        st.plotly_chart(
            go.Figure(
                go.Bar(x=imp.values, y=imp.index, orientation="h", marker_color=BLUE)
            ).update_layout(height=500, margin={"t": 10}),
            width="stretch",
        )
        st.dataframe(s, hide_index=True)


def page_battery() -> None:
    summ = api("/simulation/summary")
    if not summ:
        st.info("Battery simulation not run yet (`make simulate`).")
        return
    rows = pd.DataFrame(summ["rows"]).set_index("strategy")
    lg, nv = rows.loc["forecast_lgbm"], rows.loc["forecast_naive"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Forecast-driven net £/MW/yr", f"£{lg['net_gbp_per_mw_year']:,.0f}")
    c2.metric("Share of perfect foresight", f"{lg['capture_vs_perfect']:.0%}")
    c3.metric(
        "Value of the better forecast",
        f"£{lg['net_gbp_per_mw_year'] - nv['net_gbp_per_mw_year']:,.0f}/MW/yr",
    )

    daily = pd.DataFrame(api("/simulation/daily"))
    fig = go.Figure()
    for strategy, g in daily.groupby("strategy"):
        fig.add_trace(
            go.Scatter(
                x=g["settlement_date"],
                y=g["cumulative_gbp"],
                name=rows.loc[strategy, "label"],
                line={
                    "color": STRATEGY_COLORS.get(strategy, GREY),
                    "width": 2.5 if strategy == "forecast_lgbm" else 1.4,
                },
            )
        )
    fig.update_layout(
        title="Cumulative net revenue",
        yaxis_title="£",
        height=420,
        legend={"orientation": "h", "y": -0.2},
    )
    st.plotly_chart(fig, width="stretch")
    table = rows.reset_index()[
        [
            "label",
            "net_gbp",
            "net_gbp_per_mw_year",
            "capture_vs_perfect",
            "share_of_gap_closed",
            "cycles_per_day",
        ]
    ]
    st.dataframe(
        table.style.format(
            {
                "net_gbp": "£{:,.0f}",
                "net_gbp_per_mw_year": "£{:,.0f}",
                "capture_vs_perfect": "{:.1%}",
                "share_of_gap_closed": "{:.1%}",
                "cycles_per_day": "{:.2f}",
            }
        ),
        hide_index=True,
    )
    with st.expander("Battery and market assumptions"):
        st.json(summ["params"])
        st.markdown(
            "Schedules are set from forecasts at the 09:00 D-1 cutoff and settled at actual MID. "
            "Wholesale arbitrage only: no Balancing Mechanism, ancillary or capacity revenue."
        )


def _pooled(fd: pd.DataFrame, model: str, col: str) -> float:
    """A per-day metric pooled over half-hours: each day weighted by its priced periods."""
    g = fd[fd["model"] == model]
    return float((g[col] * g["n"]).sum() / g["n"].sum())


def page_track_record() -> None:
    record = f"{RECORD_REPO}/tree/{RECORD_BRANCH}"
    st.markdown(
        f"Every live forecast is committed to the [public track record]({record}) before its "
        "delivery day begins and scored here once Elexon publishes the actual prices. The "
        "model is refitted each month on data up to two days before, as in the backtest, and "
        "each forecast names the published model that made it. Nothing in the record is "
        "edited afterwards, so unlike the backtest it cannot have been tuned with hindsight."
    )
    live = api("/live/metrics") or {"forecast_daily": [], "battery_daily": []}
    if not live["forecast_daily"]:
        st.info(
            "No days settled yet. The first forecast is scored the day after its delivery day, "
            "once actual prices are in. Historical performance comes only from the walk-forward "
            "backtest; nothing is back-filled into the record."
        )
        return
    fd = pd.DataFrame(live["forecast_daily"])
    fd["settlement_date"] = pd.to_datetime(fd["settlement_date"])
    bd = pd.DataFrame(live["battery_daily"])
    lg_cov = _pooled(fd, "lgbm_quantile", "coverage")
    skill = 1 - _pooled(fd, "lgbm_quantile", "pinball_mean") / _pooled(
        fd, "seasonal_naive", "pinball_mean"
    )
    days = fd.loc[fd["model"] == "lgbm_quantile", "settlement_date"].nunique()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Days settled", f"{days}")
    c2.metric("P10-P90 coverage", f"{lg_cov:.1%}", "nominal 80%", delta_color="off")
    c3.metric("Pinball skill vs baseline", f"{skill:.0%}")
    if not bd.empty:
        totals = bd.groupby("strategy")["net_gbp"].sum()
        c4.metric("Forecast-driven battery £", f"£{totals.get('forecast_lgbm', 0):,.0f}")
        if totals.get("perfect_foresight", 0) > 0:
            share = totals.get("forecast_lgbm", 0) / totals["perfect_foresight"]
            c4.caption(f"{share:.0%} of perfect foresight")

        bd["settlement_date"] = pd.to_datetime(bd["settlement_date"])
        fig = go.Figure()
        for strategy, g in bd.sort_values("settlement_date").groupby("strategy"):
            fig.add_trace(
                go.Scatter(
                    x=g["settlement_date"],
                    y=g["net_gbp"].cumsum(),
                    mode="lines+markers",
                    name=STRATEGY_NAMES.get(strategy, strategy),
                    line={
                        "color": STRATEGY_COLORS.get(strategy, GREY),
                        "width": 2.5 if strategy == "forecast_lgbm" else 1.4,
                    },
                )
            )
        fig.update_layout(
            title="Cumulative net battery revenue since the record began",
            yaxis_title="£",
            height=380,
            legend={"orientation": "h", "y": -0.2},
        )
        st.plotly_chart(fig, width="stretch")

    lg = fd[fd["model"] == "lgbm_quantile"].sort_values("settlement_date")
    cum = (lg["coverage"] * lg["n"]).cumsum() / lg["n"].cumsum()
    fig = go.Figure()
    fig.add_trace(
        go.Bar(x=lg["settlement_date"], y=lg["coverage"], name="Daily", marker_color=BLUE)
    )
    fig.add_trace(
        go.Scatter(x=lg["settlement_date"], y=cum, name="Since the start", line={"color": BLACK})
    )
    fig.add_hline(y=0.8, line_dash="dash", annotation_text="nominal 80%")
    fig.update_layout(
        title="Share of half-hours inside the P10-P90 interval",
        height=320,
        yaxis={"tickformat": ".0%", "range": [0, 1]},
        legend={"orientation": "h", "y": -0.2},
    )
    st.plotly_chart(fig, width="stretch")

    with st.expander("Every settled day"):
        st.dataframe(
            fd[["settlement_date", "model", "pinball_mean", "mae_p50", "coverage", "n"]],
            hide_index=True,
        )
        if not bd.empty:
            st.dataframe(bd, hide_index=True)


def main() -> None:
    st.set_page_config(page_title="GB power price forecasts", layout="wide")
    st.title("GB day-ahead electricity price forecasts")
    st.caption(
        "Half-hourly P10/P50/P90 forecasts of the Elexon Market Index Price, made only with "
        "information available at 09:00 UK the day before delivery, and valued through a "
        "1 MW / 2 MWh battery."
    )
    try:
        health = api("/health")
    except Exception as exc:  # API down: say so instead of a stack trace
        st.error(f"Cannot reach the API at {API_URL}: {exc}")
        return
    if health and health["status"] != "ok":
        st.warning(f"Some outputs are missing: {health['outputs']}")
    tabs = st.tabs(["Latest forecast", "Track record", "Backtest", "Battery £"])
    with tabs[0]:
        page_forecast()
    with tabs[1]:
        page_track_record()
    with tabs[2]:
        page_backtest()
    with tabs[3]:
        page_battery()
    with st.sidebar:
        st.markdown("### About")
        st.markdown(
            "- Target: Elexon MID (APXMIDP), not the day-ahead auction price\n"
            "- Features: point-in-time forecasts (NESO demand and wind, embedded solar, "
            "ICON weather as issued)\n"
            "- Models: LightGBM quantile regression vs a seasonal naive baseline\n"
            f"- Live record: [{RECORD_BRANCH} branch]({RECORD_REPO}/tree/{RECORD_BRANCH})\n"
            f"- Code: [{RECORD_REPO.removeprefix('https://')}]({RECORD_REPO})"
        )


main()
