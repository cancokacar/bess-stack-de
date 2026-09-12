"""Project cashflow and the return metrics ``outputs.metrics`` names.

Unlevered and post-tax where the name says post-tax. The scenario's
``discount_rate`` is a post-tax WACC, which is why the reported NPV is the
post-tax one and why there is no ``npv_pre_tax``.

Two modelling choices are worth knowing before reading any number out of here.

*Degradation cost is not a cash cost.* The 4 EUR/MWh in the objective is the
shadow price of consuming battery life; it shapes dispatch and is not invoiced.
The cash consequence of cycling appears instead as augmentation capex, at the
year the trigger fires. Carrying both would count the same wear twice.

*One modelled year is repeated across the life.* The dispatch is solved once and
its margin scaled by remaining capacity, rather than re-solved per year against
that year's prices and that year's faded battery. Revenue is scaled linearly with
usable energy, which is conservative: the cycles a smaller battery declines are
its least valuable, so true retention is better than proportional.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Scenario
from ..model.dispatch import DispatchResult
from .grid_fees import grid_fee_charge

KW_PER_MW = 1000.0
KWH_PER_MWH = 1000.0

# A calendar year is 8760 hours, or 8784 in a leap year. Daylight saving nets out.
MIN_YEAR_HOURS = 8700.0
MAX_YEAR_HOURS = 8800.0


class CashflowError(ValueError):
    """The dispatch result cannot support a project cashflow."""


@dataclass(frozen=True)
class YearRow:
    """One project year, in the order the arithmetic runs."""

    year: int
    # Capacity after this year's fade but before any augmentation, and after it.
    # Both are kept because the first is what the trigger compares against, and
    # without it the output gives no way to check why an event fired.
    capacity_before_augmentation_fraction: float
    capacity_fraction: float
    discharged_mwh: float
    energy_margin_eur: float
    opex_eur: float
    network_charge_eur: float
    ebitda_eur: float
    augmentation_eur: float
    depreciation_eur: float
    taxable_income_eur: float
    tax_eur: float

    @property
    def pre_tax_cashflow_eur(self) -> float:
        return self.ebitda_eur - self.augmentation_eur

    @property
    def post_tax_cashflow_eur(self) -> float:
        return self.pre_tax_cashflow_eur - self.tax_eur


@dataclass(frozen=True)
class ProjectCashflow:
    capex_eur: float
    residual_value_eur: float
    rows: list[YearRow]
    discount_rate: float
    end_of_life_capacity_fraction: float

    @property
    def first_past_end_of_life_year(self) -> int | None:
        """First year the pack is below its own end-of-life threshold, if any.

        `Degradation.validate` cannot answer this. It sees calendar fade only,
        because cyclic fade depends on the cycle count and the cycle count is an
        outcome of dispatch rather than an input to it. This is the first place
        the real trajectory exists, so it is the first place the question can be
        asked -- and with augmentation capped at `max_events`, a scenario can run
        years past end of life while still reporting revenue.

        Reported rather than raised: the run is still informative, and the caller
        decides whether a project modelled past its own end of life is one worth
        quoting.
        """
        for row in self.rows:
            if row.capacity_fraction < self.end_of_life_capacity_fraction:
                return row.year
        return None

    @property
    def pre_tax_flows(self) -> list[float]:
        flows = [-self.capex_eur] + [r.pre_tax_cashflow_eur for r in self.rows]
        flows[-1] += self.residual_value_eur
        return flows

    @property
    def post_tax_flows(self) -> list[float]:
        flows = [-self.capex_eur] + [r.post_tax_cashflow_eur for r in self.rows]
        flows[-1] += self.residual_value_eur
        return flows

    @property
    def npv_post_tax_eur(self) -> float:
        return npv(self.discount_rate, self.post_tax_flows)

    @property
    def irr_pre_tax(self) -> float | None:
        return irr(self.pre_tax_flows)

    @property
    def irr_post_tax(self) -> float | None:
        return irr(self.post_tax_flows)

    @property
    def payback_years(self) -> float | None:
        """Undiscounted years until cumulative post-tax cash turns positive."""
        cumulative = -self.capex_eur
        for index, row in enumerate(self.rows, start=1):
            flow = row.post_tax_cashflow_eur
            if index == len(self.rows):
                flow += self.residual_value_eur
            if cumulative + flow >= 0 and flow > 0:
                return index - 1 + (-cumulative / flow)
            cumulative += flow
        return None

    @property
    def lcos_eur_per_mwh(self) -> float | None:
        """Discounted lifetime cost over discounted lifetime discharge, pre-tax.

        A cost measure by convention, so tax does not enter it.
        """
        rate = self.discount_rate
        costs = self.capex_eur
        energy = 0.0
        for index, row in enumerate(self.rows, start=1):
            factor = (1.0 + rate) ** index
            costs += (row.opex_eur + row.network_charge_eur + row.augmentation_eur) / factor
            energy += row.discharged_mwh / factor
        return None if energy == 0 else costs / energy

    def metric(self, name: str) -> float | None:
        try:
            return {
                "npv_post_tax": self.npv_post_tax_eur,
                "irr_pre_tax": self.irr_pre_tax,
                "irr_post_tax": self.irr_post_tax,
                "payback_years": self.payback_years,
                "lcos_eur_per_mwh": self.lcos_eur_per_mwh,
            }[name]
        except KeyError:
            raise KeyError(f"no metric named '{name}'") from None


def npv(rate: float, flows: list[float]) -> float:
    """Net present value, with ``flows[0]`` at time zero and undiscounted."""
    return sum(flow / (1.0 + rate) ** period for period, flow in enumerate(flows))


def irr(flows: list[float], lo: float = -0.9999, hi: float = 10.0) -> float | None:
    """Internal rate of return by bisection, or None when there is no sign change.

    Bisection rather than Newton because it cannot diverge, and a project IRR
    that fails to converge is worse than one reported as absent. None is returned
    honestly rather than substituting zero: a project that never pays back has no
    IRR, and printing 0 % would read as break-even.
    """
    low, high = npv(lo, flows), npv(hi, flows)
    if low * high > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        value = npv(mid, flows)
        if abs(value) < 1e-9:
            return mid
        if low * value <= 0:
            hi, high = mid, value
        else:
            lo, low = mid, value
    return (lo + hi) / 2.0


def energy_margin_eur(scenario: Scenario, result: DispatchResult) -> float:
    """Cash energy margin: what the trading actually earned, net of fees.

    ``DispatchResult.gross_revenue_eur`` values dispatch at the raw price and
    ``net_revenue_eur`` subtracts the non-cash degradation charge, so neither is
    the cash figure once a haircut or a fee is non-zero. This applies the same
    coefficients the objective used, then adds the degradation charge back.
    """
    day_ahead, grid = scenario.day_ahead, scenario.grid
    keep = 1.0 - day_ahead.spread_haircut
    dt = result.resolution_hours
    sell = result.prices_eur_per_mwh * keep - day_ahead.fees_eur_per_mwh
    sell = sell - grid.discharging_fees_eur_per_mwh
    buy = result.prices_eur_per_mwh * keep + day_ahead.fees_eur_per_mwh
    buy = buy + grid.charging_fees_eur_per_mwh
    return float(((sell * result.discharge_mw - buy * result.charge_mw) * dt).sum())


def annual_fade_fraction(scenario: Scenario, result: DispatchResult) -> float:
    """Capacity lost per year: calendar fade plus cyclic fade at the modelled rate.

    Linear, matching the convention the scenario file's own cost derivation uses.
    """
    degradation = scenario.degradation
    cycles = result.equivalent_full_cycles(scenario.battery.energy_mwh)
    return degradation.calendar_fade_per_year + degradation.cyclic_fade_per_full_cycle * cycles


def build(
    scenario: Scenario, result: DispatchResult, first_year: int | None = None
) -> ProjectCashflow:
    """Assemble the project cashflow from one solved dispatch year."""
    hours = len(result.charge_mw) * result.resolution_hours
    if not MIN_YEAR_HOURS <= hours <= MAX_YEAR_HOURS:
        raise CashflowError(
            f"the dispatch result covers {hours:,.0f} hours; a project cashflow needs a full "
            f"year ({MIN_YEAR_HOURS:,.0f}-{MAX_YEAR_HOURS:,.0f} h). Every figure here is "
            "treated as annual, so a short run would be silently annualised -- and the "
            "series starts in January, whose spreads are the narrowest of the year, so that "
            "annualisation understates rather than approximates"
        )
    finance, battery = scenario.finance, scenario.battery
    start = scenario.market.year if first_year is None else first_year

    capex = finance.capex_eur_per_kwh * battery.energy_mwh * KWH_PER_MWH + finance.capex_fixed_eur
    opex_year_one = finance.opex_eur_per_kw_year * battery.power_mw * KW_PER_MW
    margin_at_full_capacity = energy_margin_eur(scenario, result)
    discharged_at_full_capacity = result.throughput_mwh

    fade = annual_fade_fraction(scenario, result)
    augmentation = scenario.degradation.augmentation

    # Depreciation is tracked as a list of (annual charge, years remaining) so a
    # mid-life augmentation depreciates from its own in-service year rather than
    # being smeared over the whole project.
    schedule: list[list[float]] = [[capex / finance.depreciation.years, finance.depreciation.years]]

    capacity = 1.0
    events = 0
    carried_loss = 0.0
    rows: list[YearRow] = []

    for offset in range(finance.project_lifetime_years):
        year = start + offset
        capacity -= fade
        capacity_before = max(capacity, 0.0)
        spend = 0.0
        if (
            augmentation.enabled
            and capacity <= augmentation.trigger_capacity_fraction
            and events < augmentation.max_events
        ):
            added = augmentation.restore_to_capacity_fraction - capacity
            spend = augmentation.cost_eur_per_kwh * added * battery.energy_mwh * KWH_PER_MWH
            capacity = augmentation.restore_to_capacity_fraction
            events += 1
            if finance.depreciation.capitalise_augmentation:
                schedule.append([spend / finance.depreciation.years, finance.depreciation.years])
        capacity = max(capacity, 0.0)

        escalation = (1.0 + finance.opex_real_escalation_per_year) ** offset
        opex = opex_year_one * escalation
        margin = margin_at_full_capacity * capacity
        discharged = discharged_at_full_capacity * capacity
        charge = grid_fee_charge(scenario, result, year).total_eur
        ebitda = margin - opex - charge

        depreciation = 0.0
        for entry in schedule:
            if entry[1] > 0:
                depreciation += entry[0]
                entry[1] -= 1

        taxable = ebitda - depreciation
        if finance.loss_carryforward:
            taxable -= carried_loss
            carried_loss = max(0.0, -taxable)
        tax = finance.tax_rate * max(0.0, taxable)

        rows.append(
            YearRow(
                year=year,
                capacity_before_augmentation_fraction=capacity_before,
                capacity_fraction=capacity,
                discharged_mwh=discharged,
                energy_margin_eur=margin,
                opex_eur=opex,
                network_charge_eur=charge,
                ebitda_eur=ebitda,
                augmentation_eur=spend,
                depreciation_eur=depreciation,
                taxable_income_eur=taxable,
                tax_eur=tax,
            )
        )

    return ProjectCashflow(
        capex_eur=capex,
        residual_value_eur=finance.residual_value_fraction * capex,
        rows=rows,
        discount_rate=finance.discount_rate,
        end_of_life_capacity_fraction=scenario.degradation.end_of_life_capacity_fraction,
    )
