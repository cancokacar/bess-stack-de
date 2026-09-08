"""Rolling-horizon day-ahead arbitrage dispatch.

This is the day-ahead leg only. FCR and aFRR are in the project's scope sentence
but not in this module yet; the block-constraint and state-of-charge machinery
here is what they will attach to.

The model is a MILP rather than an LP because of a single binary per step
forbidding simultaneous charge and discharge. Without it, at sufficiently
negative prices the optimizer charges and discharges at once to dissipate energy
through round-trip losses at zero net cost, which lets it keep buying while
full. That is an artefact of the relaxation, not a dispatch strategy.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pulp

from ..config import Scenario


@dataclass(frozen=True)
class DispatchResult:
    """Committed dispatch over the whole run, at market resolution."""

    charge_mw: np.ndarray
    discharge_mw: np.ndarray
    soc_mwh: np.ndarray
    prices_eur_per_mwh: np.ndarray
    resolution_hours: float
    degradation_cost_eur_per_mwh: float

    @property
    def gross_revenue_eur(self) -> float:
        dt = self.resolution_hours
        return float(
            np.sum(self.prices_eur_per_mwh * (self.discharge_mw - self.charge_mw) * dt)
        )

    @property
    def throughput_mwh(self) -> float:
        """Discharged energy, the basis on which degradation is charged."""
        return float(np.sum(self.discharge_mw) * self.resolution_hours)

    @property
    def degradation_cost_eur(self) -> float:
        return self.throughput_mwh * self.degradation_cost_eur_per_mwh

    @property
    def net_revenue_eur(self) -> float:
        return self.gross_revenue_eur - self.degradation_cost_eur

    def equivalent_full_cycles(self, energy_mwh: float) -> float:
        return self.throughput_mwh / energy_mwh


def _build_solver(scenario: Scenario) -> pulp.LpSolver:
    solver = scenario.dispatch.solver
    kwargs = {"msg": False, "gapRel": solver.mip_gap, "timeLimit": solver.time_limit_s}
    if solver.name == "highs":
        return pulp.HiGHS(**kwargs)
    if solver.name == "cbc":
        return pulp.PULP_CBC_CMD(**kwargs)
    raise ValueError(f"unknown dispatch.solver.name '{solver.name}'")


def solve_window(
    scenario: Scenario,
    prices: np.ndarray,
    soc_initial_mwh: float,
    terminal_soc_mwh: float | None = None,
    solver: pulp.LpSolver | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Optimise one window. Returns (charge_mw, discharge_mw, end-of-step soc_mwh)."""
    battery = scenario.battery
    grid = scenario.grid
    day_ahead = scenario.day_ahead

    n = len(prices)
    dt = 1.0 / scenario.market.steps_per_hour
    steps_per_block = day_ahead.product_resolution_minutes // scenario.market.resolution_minutes
    power_cap = min(battery.power_mw, grid.connection_limit_mw)

    problem = pulp.LpProblem("day_ahead_arbitrage", pulp.LpMaximize)
    charge = {
        t: problem.add_variable(f"charge_{t}", lowBound=0, upBound=power_cap) for t in range(n)
    }
    discharge = {
        t: problem.add_variable(f"discharge_{t}", lowBound=0, upBound=power_cap)
        for t in range(n)
    }
    is_charging = {
        t: problem.add_variable(f"is_charging_{t}", cat="Binary") for t in range(n)
    }
    soc = {
        t: problem.add_variable(
            f"soc_{t}",
            lowBound=battery.soc_min * battery.energy_mwh,
            upBound=battery.soc_max * battery.energy_mwh,
        )
        for t in range(n)
    }

    # Captured prices, after the haircut on the spread and per-MWh fees.
    keep = 1.0 - day_ahead.spread_haircut
    sell = prices * keep - day_ahead.fees_eur_per_mwh - grid.discharging_fees_eur_per_mwh
    buy = prices * keep + day_ahead.fees_eur_per_mwh + grid.charging_fees_eur_per_mwh

    problem += pulp.lpSum(
        (sell[t] - scenario.degradation.marginal_cost_eur_per_mwh) * discharge[t] * dt
        - buy[t] * charge[t] * dt
        for t in range(n)
    )

    idle_loss = (
        battery.self_discharge_per_day * battery.energy_mwh / 24.0 + battery.aux_load_mw
    ) * dt

    for t in range(n):
        problem += charge[t] <= power_cap * is_charging[t]
        problem += discharge[t] <= power_cap * (1 - is_charging[t])
        previous = soc_initial_mwh if t == 0 else soc[t - 1]
        problem += soc[t] == (
            previous
            + battery.charge_efficiency * charge[t] * dt
            - discharge[t] * dt / battery.discharge_efficiency
            - idle_loss
        )
        # Day-ahead settles in hourly blocks: power is constant within a block.
        if steps_per_block > 1 and t % steps_per_block != 0:
            problem += charge[t] == charge[t - 1]
            problem += discharge[t] == discharge[t - 1]

    if terminal_soc_mwh is not None:
        problem += soc[n - 1] >= terminal_soc_mwh

    status = problem.solve(solver or _build_solver(scenario))
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"window did not solve to optimality: {pulp.LpStatus[status]}")

    return (
        np.array([charge[t].value() or 0.0 for t in range(n)]),
        np.array([discharge[t].value() or 0.0 for t in range(n)]),
        np.array([soc[t].value() or 0.0 for t in range(n)]),
    )


def run_rolling_horizon(scenario: Scenario, prices: np.ndarray) -> DispatchResult:
    """Roll a receding horizon across `prices`, committing the leading step each time.

    Each window is optimised over `horizon_hours` but only the first
    `commit_step_hours` are kept; state of charge carries into the next window.
    The horizon beyond the committed region exists to stop the terminal state of
    charge constraint from distorting the decisions actually taken.
    """
    market = scenario.market
    dispatch = scenario.dispatch
    battery = scenario.battery

    horizon_steps = dispatch.horizon_hours * market.steps_per_hour
    commit_steps = dispatch.commit_step_hours * market.steps_per_hour
    if dispatch.mode == "perfect_foresight":
        horizon_steps = commit_steps = len(prices)

    terminal_soc = dispatch.terminal_soc_fraction * battery.energy_mwh
    solver = _build_solver(scenario)

    soc_now = battery.soc_initial * battery.energy_mwh
    charge_out: list[np.ndarray] = []
    discharge_out: list[np.ndarray] = []
    soc_out: list[np.ndarray] = []

    for start in range(0, len(prices), commit_steps):
        window = prices[start : start + horizon_steps]
        # A final window shorter than the commit step still gets optimised, but a
        # terminal target is only meaningful when there is horizon beyond the
        # committed region to absorb it.
        target = terminal_soc if len(window) > commit_steps else None
        charge, discharge, soc = solve_window(scenario, window, soc_now, target, solver)

        keep = min(commit_steps, len(window))
        charge_out.append(charge[:keep])
        discharge_out.append(discharge[:keep])
        soc_out.append(soc[:keep])
        soc_now = float(soc[keep - 1])

    return DispatchResult(
        charge_mw=np.concatenate(charge_out),
        discharge_mw=np.concatenate(discharge_out),
        soc_mwh=np.concatenate(soc_out),
        prices_eur_per_mwh=prices,
        resolution_hours=1.0 / market.steps_per_hour,
        degradation_cost_eur_per_mwh=scenario.degradation.marginal_cost_eur_per_mwh,
    )
