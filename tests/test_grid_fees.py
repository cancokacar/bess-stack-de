import copy

import numpy as np
import pytest
import yaml

from bess_stack.config import Scenario
from bess_stack.finance.grid_fees import (
    annual_grid_fee_charges,
    first_charged_year,
    grid_fee_charge,
    net_revenue_after_grid_fees,
)
from bess_stack.model.dispatch import DispatchResult, run_rolling_horizon

REFERENCE = "scenarios/reference.yaml"


@pytest.fixture
def raw():
    with open(REFERENCE) as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def scenario(raw):
    return Scenario.from_dict(raw)


def result_drawing(charged_mwh: float, discharged_mwh: float = 0.0) -> DispatchResult:
    """A DispatchResult with known charged and discharged energy, no solving needed."""
    dt = 0.25
    return DispatchResult(
        charge_mw=np.array([charged_mwh / dt]),
        discharge_mw=np.array([discharged_mwh / dt]),
        soc_mwh=np.zeros(1),
        prices_eur_per_mwh=np.zeros(1),
        resolution_hours=dt,
        degradation_cost_eur_per_mwh=4.0,
    )


def charged_scenario(raw, energy_fee=15.0, capacity_fee=0.0, until=2020):
    """Reference case with the exemption already lapsed and a real successor tariff."""
    d = copy.deepcopy(raw)
    d["grid"]["grid_fee_exemption_until_year"] = until
    d["grid"]["post_exemption"]["charging_fees_eur_per_mwh"] = energy_fee
    d["grid"]["post_exemption"]["capacity_charge_eur_per_kw_year"] = capacity_fee
    return Scenario.from_dict(d)


def test_exempt_year_carries_no_charge(scenario):
    charge = grid_fee_charge(scenario, result_drawing(1000.0), year=2030)
    assert charge.exempt
    assert charge.total_eur == 0.0


def test_charged_year_bills_energy_and_capacity(raw):
    s = charged_scenario(raw, energy_fee=15.0, capacity_fee=8.0)
    charge = grid_fee_charge(s, result_drawing(1000.0), year=2025)
    assert not charge.exempt
    assert charge.energy_charge_eur == pytest.approx(15.0 * 1000.0)
    # 8 EUR/kW/yr on a 10 MW connection is 8 * 10 * 1000.
    assert charge.capacity_charge_eur == pytest.approx(80_000.0)


def test_null_exemption_year_means_never_exempt(raw):
    d = copy.deepcopy(raw)
    d["grid"]["grid_fee_exemption_until_year"] = None
    d["grid"]["post_exemption"]["charging_fees_eur_per_mwh"] = 5.0
    charge = grid_fee_charge(Scenario.from_dict(d), result_drawing(100.0), year=2025)
    assert not charge.exempt
    assert charge.energy_charge_eur == pytest.approx(500.0)


def test_fee_already_in_the_objective_is_not_billed_twice(raw):
    """Guards the coupling to model.dispatch: what was priced there is not charged here."""
    s = charged_scenario(raw, energy_fee=15.0)
    charge = grid_fee_charge(
        s, result_drawing(1000.0), year=2025, dispatch_charging_fee_eur_per_mwh=15.0
    )
    assert charge.energy_charge_eur == pytest.approx(0.0)


def test_annual_series_spans_the_project_life_and_flips_on_the_end_year(raw):
    s = charged_scenario(raw, energy_fee=15.0, until=2029)
    charges = annual_grid_fee_charges(s, result_drawing(1000.0))
    assert len(charges) == s.project_lifetime_years
    by_year = {c.year: c for c in charges}
    assert by_year[2029].exempt and by_year[2029].total_eur == 0.0
    assert not by_year[2030].exempt and by_year[2030].total_eur > 0.0


def test_reference_case_never_reaches_a_charged_year(scenario):
    """2025..2044 exemption against a 20 year life: the successor tariff is never paid."""
    assert first_charged_year(scenario) is None


def test_first_charged_year_found_when_the_exemption_lapses_early(raw):
    assert first_charged_year(charged_scenario(raw, until=2029)) == 2030


def test_net_revenue_subtracts_the_year_charge(raw):
    s = charged_scenario(raw, energy_fee=15.0)
    result = result_drawing(1000.0, discharged_mwh=800.0)
    expected = result.net_revenue_eur - 15.0 * 1000.0
    assert net_revenue_after_grid_fees(s, result, 2025) == pytest.approx(expected)


def sawtooth(scenario, days, low=30.0, high=110.0):
    hourly = np.full(24 * days, 60.0)
    for day in range(days):
        hourly[day * 24 + 2 : day * 24 + 6] = low
        hourly[day * 24 + 18 : day * 24 + 22] = high
    return np.repeat(hourly, scenario.market.steps_per_hour)


def test_post_processing_biases_revenue_down_and_throughput_up(raw):
    """Measures the approximation this module is built on, in both directions.

    Post-processing keeps a dispatch optimised without the fee. That dispatch is
    still feasible once the fee exists, so it can only score worse than one
    re-optimised against it: revenue comes out low. It also keeps the marginal
    cycles the fee should have suppressed: throughput comes out high.
    """
    fee = 25.0

    exempt = copy.deepcopy(raw)
    exempt["dispatch"]["solver"]["mip_gap"] = 0.0
    exempt_scenario = Scenario.from_dict(exempt)

    # The same battery, with the fee visible to the optimizer instead.
    reoptimised = copy.deepcopy(exempt)
    reoptimised["grid"]["charging_fees_eur_per_mwh"] = fee
    reoptimised_scenario = Scenario.from_dict(reoptimised)

    prices = sawtooth(exempt_scenario, days=4)
    unaware = run_rolling_horizon(exempt_scenario, prices)
    aware = run_rolling_horizon(reoptimised_scenario, prices)

    post_processed = unaware.net_revenue_eur - fee * unaware.charged_mwh
    re_solved = aware.net_revenue_eur - fee * aware.charged_mwh

    assert post_processed <= re_solved + 1e-6, "re-optimising cannot do worse"
    assert unaware.throughput_mwh >= aware.throughput_mwh - 1e-6
    # The fee has to actually change the dispatch, or this proves nothing.
    assert unaware.throughput_mwh > aware.throughput_mwh + 1e-6
