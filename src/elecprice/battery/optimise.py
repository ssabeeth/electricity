"""Daily battery arbitrage schedule by linear programming (pulp + HiGHS).

Decision variables per settlement period t (duration dt = 0.5 h):
    c_t   charge power drawn from the grid, MW      0 <= c_t <= P
    d_t   discharge power delivered to the grid, MW 0 <= d_t <= P
    s_t   state of charge at the end of t, MWh      S_min <= s_t <= S_max

    s_t = s_{t-1} + eta_c * c_t * dt - d_t * dt / eta_d,   s_0 = s_T = S_init
    sum_t d_t * dt / eta_d <= cycles * E                   (cycle limit)

    maximise  sum_t price_t * (d_t - c_t) * dt  -  deg_cost * sum_t d_t * dt

``price_t`` is whatever the caller decides with: a forecast for the
forecast-driven strategies, the actual price only for the perfect-foresight
bound. Settlement against actual prices is a separate function, so a schedule
can never depend on the prices it is paid.

The LP can in principle charge and discharge in the same period to burn
energy when prices are very negative, which a real battery cannot do. We
solve the LP first and, only if that happens, re-solve with a binary mode
variable per period (a small MILP).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pulp
import yaml

from elecprice.config import get_settings

DT_HOURS = 0.5
EPS = 1e-6


def _solver():
    # HiGHS runs in-process via highspy (fast, no subprocess per solve). PuLP's
    # bundled CBC is deprecated in PuLP 3 and kept only as a fallback.
    highs = pulp.HiGHS(msg=False)
    if highs.available():
        return highs
    return pulp.PULP_CBC_CMD(msg=False)


@dataclass(frozen=True)
class BatteryParams:
    power_mw: float = 1.0
    energy_mwh: float = 2.0
    round_trip_efficiency: float = 0.90
    soc_min_frac: float = 0.0
    soc_max_frac: float = 1.0
    initial_soc_frac: float = 0.0
    max_cycles_per_day: float = 1.5
    degradation_cost_gbp_per_mwh: float = 10.0
    naive_charge_window: tuple[float, float] = (1.0, 5.0)
    naive_discharge_window: tuple[float, float] = (16.5, 19.5)

    @property
    def eta_charge(self) -> float:
        return math.sqrt(self.round_trip_efficiency)

    @property
    def eta_discharge(self) -> float:
        return math.sqrt(self.round_trip_efficiency)

    @property
    def soc_min(self) -> float:
        return self.soc_min_frac * self.energy_mwh

    @property
    def soc_max(self) -> float:
        return self.soc_max_frac * self.energy_mwh

    @property
    def soc_initial(self) -> float:
        return self.initial_soc_frac * self.energy_mwh

    @classmethod
    def load(cls, path: Path | None = None) -> BatteryParams:
        path = path or get_settings().config_dir / "battery.yaml"
        raw = yaml.safe_load(Path(path).read_text())
        for k in ("naive_charge_window", "naive_discharge_window"):
            if k in raw:
                raw[k] = tuple(raw[k])
        return cls(**raw)


@dataclass(frozen=True)
class Schedule:
    charge_mw: np.ndarray
    discharge_mw: np.ndarray
    soc_mwh: np.ndarray  # end-of-period state of charge
    used_milp: bool = False

    @property
    def net_export_mw(self) -> np.ndarray:
        return self.discharge_mw - self.charge_mw


def _solve(
    prices: np.ndarray,
    p: BatteryParams,
    *,
    binary: bool,
    allowed_charge: np.ndarray | None = None,
    allowed_discharge: np.ndarray | None = None,
) -> Schedule:
    n = len(prices)
    prob = pulp.LpProblem("battery", pulp.LpMaximize)
    c = [prob.add_variable(f"c{t}", 0, p.power_mw) for t in range(n)]
    d = [prob.add_variable(f"d{t}", 0, p.power_mw) for t in range(n)]
    s = [prob.add_variable(f"s{t}", p.soc_min, p.soc_max) for t in range(n)]

    prob += pulp.lpSum(
        float(prices[t]) * (d[t] - c[t]) * DT_HOURS
        - p.degradation_cost_gbp_per_mwh * d[t] * DT_HOURS
        for t in range(n)
    )
    for t in range(n):
        prev = s[t - 1] if t > 0 else p.soc_initial
        prob += s[t] == prev + p.eta_charge * c[t] * DT_HOURS - d[t] * DT_HOURS / p.eta_discharge
        if binary:
            u = prob.add_variable(f"u{t}", cat=pulp.LpBinary)
            prob += c[t] <= p.power_mw * u
            prob += d[t] <= p.power_mw * (1 - u)
        if allowed_charge is not None and not allowed_charge[t]:
            prob += c[t] == 0
        if allowed_discharge is not None and not allowed_discharge[t]:
            prob += d[t] == 0
    prob += s[n - 1] == p.soc_initial
    prob += (
        pulp.lpSum(d[t] * DT_HOURS / p.eta_discharge for t in range(n))
        <= p.max_cycles_per_day * p.energy_mwh
    )

    status = prob.solve(_solver())
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"battery optimisation failed: {pulp.LpStatus[status]}")
    val = lambda xs: np.array([max(0.0, x.value() or 0.0) for x in xs])  # noqa: E731
    return Schedule(val(c), val(d), np.array([x.value() for x in s]), used_milp=binary)


def optimise_day(
    prices: np.ndarray,
    params: BatteryParams,
    *,
    allowed_charge: np.ndarray | None = None,
    allowed_discharge: np.ndarray | None = None,
) -> Schedule:
    """Profit-maximising schedule for one day given *decision* prices (£/MWh)."""
    prices = np.asarray(prices, dtype=float)
    if np.isnan(prices).any():
        raise ValueError("decision prices contain NaN")
    kw = {"allowed_charge": allowed_charge, "allowed_discharge": allowed_discharge}
    sched = _solve(prices, params, binary=False, **kw)
    if np.any((sched.charge_mw > EPS) & (sched.discharge_mw > EPS)):
        sched = _solve(prices, params, binary=True, **kw)
    return sched


def fixed_rule_schedule(local_hour: np.ndarray, params: BatteryParams) -> Schedule:
    """Price-blind rule used as the lower bound.

    Charge at full power from the start of the night window until full, then
    discharge at full power from the start of the evening window back to the
    initial state of charge. Written as an explicit rule rather than an LP,
    because an LP with a flat signal inside each window has many equally
    optimal schedules and the result would depend on solver tie-breaking.
    """
    h = np.asarray(local_hour, dtype=float)
    c_lo, c_hi = params.naive_charge_window
    d_lo, d_hi = params.naive_discharge_window
    n = len(h)
    charge, discharge, soc = np.zeros(n), np.zeros(n), np.zeros(n)
    level = params.soc_initial
    budget = params.max_cycles_per_day * params.energy_mwh  # energy out of storage
    for t in range(n):
        if c_lo <= h[t] < c_hi:
            room = params.soc_max - level
            charge[t] = min(params.power_mw, room / (params.eta_charge * DT_HOURS))
            level += params.eta_charge * charge[t] * DT_HOURS
        elif d_lo <= h[t] < d_hi:
            available = min(level - params.soc_initial, budget)
            discharge[t] = min(params.power_mw, available * params.eta_discharge / DT_HOURS)
            out = discharge[t] * DT_HOURS / params.eta_discharge
            level -= out
            budget -= out
        soc[t] = level
    if abs(level - params.soc_initial) > 1e-9:
        # Windows too short to return to the initial state: undo the unmatched charge.
        return Schedule(np.zeros(n), np.zeros(n), np.full(n, params.soc_initial))
    return Schedule(charge, discharge, soc)


@dataclass(frozen=True)
class Settlement:
    revenue_gbp: float  # energy sold minus energy bought, at actual prices
    degradation_gbp: float
    net_gbp: float
    discharged_mwh: float  # delivered to the grid
    charged_mwh: float  # drawn from the grid
    cycles: float  # full-equivalent cycles (energy out of storage / capacity)


def settle(schedule: Schedule, actual_prices: np.ndarray, params: BatteryParams) -> Settlement:
    """Cash flows of ``schedule`` at the realised prices."""
    actual = np.asarray(actual_prices, dtype=float)
    revenue = float(np.sum(actual * schedule.net_export_mw) * DT_HOURS)
    discharged = float(schedule.discharge_mw.sum() * DT_HOURS)
    charged = float(schedule.charge_mw.sum() * DT_HOURS)
    degradation = params.degradation_cost_gbp_per_mwh * discharged
    cycles = discharged / params.eta_discharge / params.energy_mwh
    return Settlement(revenue, degradation, revenue - degradation, discharged, charged, cycles)
