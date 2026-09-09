"""Network charges applied to an already-solved dispatch.

APPROXIMATION. Fees here are applied to a dispatch that was optimised without
them, rather than by re-solving each project year under that year's charges. A
charging fee narrows the effective day-ahead spread, so a battery that saw the
fee would decline its most marginal cycles. This module keeps every one of them
and subtracts money afterwards, which biases the two halves of the answer in
opposite directions:

* throughput, degradation cost and equivalent full cycles are those of the
  EXEMPT case, so they are overstated for a charged year. Anything keyed off
  cycle count -- the augmentation trigger above all -- fires too early.
* net revenue is UNDERSTATED. The kept dispatch is feasible but no longer
  optimal once the fee is in the objective, so it can only score worse than a
  re-optimised one. A charged year's revenue is a lower bound, which errs
  against the project rather than for it.

The error is exactly zero while the exemption holds, and grows with the fee.
``tests/test_grid_fees.py`` measures both directions against a re-optimised run.
Read that number before treating a charged year as anything but indicative, and
re-solve per year if it has grown large enough to matter.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Scenario
from ..model.dispatch import DispatchResult

KW_PER_MW = 1000.0


@dataclass(frozen=True)
class GridFeeCharge:
    """Network charges falling on one project year."""

    year: int
    exempt: bool
    energy_charge_eur: float
    capacity_charge_eur: float

    @property
    def total_eur(self) -> float:
        return self.energy_charge_eur + self.capacity_charge_eur


def grid_fee_charge(
    scenario: Scenario,
    result: DispatchResult,
    year: int,
    dispatch_charging_fee_eur_per_mwh: float | None = None,
) -> GridFeeCharge:
    """Network charges for `year` on the dispatch in `result`.

    Only the charges the dispatch did not already price are counted.
    `dispatch_charging_fee_eur_per_mwh` is the per-MWh charging fee the optimizer
    actually saw, defaulting to the scenario's always-applicable fee, which is
    what `model.dispatch` currently passes to the objective. If dispatch is ever
    changed to price a year's fee directly, pass that fee here so this does not
    charge it twice.
    """
    grid = scenario.grid
    already_priced = (
        grid.charging_fees_eur_per_mwh
        if dispatch_charging_fee_eur_per_mwh is None
        else dispatch_charging_fee_eur_per_mwh
    )
    exempt = grid.exempt_in(year)
    unpriced_fee = grid.charging_fees_in(year) - already_priced

    capacity_charge = (
        0.0
        if exempt
        else grid.post_exemption_capacity_charge_eur_per_kw_year
        * grid.connection_limit_mw
        * KW_PER_MW
    )
    return GridFeeCharge(
        year=year,
        exempt=exempt,
        energy_charge_eur=unpriced_fee * result.charged_mwh,
        capacity_charge_eur=capacity_charge,
    )


def annual_grid_fee_charges(
    scenario: Scenario,
    result: DispatchResult,
    first_year: int | None = None,
) -> list[GridFeeCharge]:
    """Network charges for every year of the project life.

    `result` is one modelled year of dispatch reused across the horizon, so the
    charged energy is held constant. Only the tariff varies by year.
    """
    start = scenario.market.year if first_year is None else first_year
    return [
        grid_fee_charge(scenario, result, year)
        for year in range(start, start + scenario.project_lifetime_years)
    ]


def first_charged_year(
    scenario: Scenario, first_year: int | None = None
) -> int | None:
    """First project year in which the exemption no longer applies, if any.

    `None` means the exemption outlasts the modelled life, which is the reference
    case and the reason it never exercises a post-exemption charge.
    """
    start = scenario.market.year if first_year is None else first_year
    for year in range(start, start + scenario.project_lifetime_years):
        if not scenario.grid.exempt_in(year):
            return year
    return None


def net_revenue_after_grid_fees(
    scenario: Scenario, result: DispatchResult, year: int
) -> float:
    """Dispatch net revenue for `year` less that year's network charges.

    A lower bound in a charged year, for the reason given in the module
    docstring: the dispatch was not re-optimised against the fee.
    """
    return result.net_revenue_eur - grid_fee_charge(scenario, result, year).total_eur
