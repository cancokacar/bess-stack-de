"""Cashflow and return-metric tests.

These use a synthetic DispatchResult rather than a solved one: the arithmetic
under test is the cashflow, and solving a real year takes ~81 s.
"""

import copy

import numpy as np
import pytest
import yaml

from bess_stack.config import Scenario, ScenarioError
from bess_stack.finance import cashflow
from bess_stack.model.dispatch import DispatchResult


def raw():
    with open("scenarios/reference.yaml") as fh:
        return yaml.safe_load(fh)


def year_result(margin_eur: float = 500_000.0, discharged_mwh: float = 11_526.0):
    """A full-year DispatchResult with a known margin and throughput.

    Prices are flat and charging is zero, so the cash margin is exactly
    price * discharged and nothing has to be inferred from a solve.
    """
    steps = 35_040
    dt = 0.25
    discharge = np.full(steps, discharged_mwh / (steps * dt))
    price = margin_eur / discharged_mwh
    return DispatchResult(
        charge_mw=np.zeros(steps),
        discharge_mw=discharge,
        soc_mwh=np.full(steps, 10.0),
        prices_eur_per_mwh=np.full(steps, price),
        resolution_hours=dt,
        degradation_cost_eur_per_mwh=4.0,
    )


def test_a_short_run_is_refused_rather_than_annualised():
    """The trap the README documents: January pro-rated is not a year."""
    result = year_result()
    week = DispatchResult(
        charge_mw=result.charge_mw[:672],
        discharge_mw=result.discharge_mw[:672],
        soc_mwh=result.soc_mwh[:672],
        prices_eur_per_mwh=result.prices_eur_per_mwh[:672],
        resolution_hours=0.25,
        degradation_cost_eur_per_mwh=4.0,
    )
    with pytest.raises(cashflow.CashflowError, match="full year"):
        cashflow.build(Scenario.from_dict(raw()), week)


def test_capex_and_row_count():
    cf = cashflow.build(Scenario.from_dict(raw()), year_result())
    assert cf.capex_eur == pytest.approx(250.0 * 20.0 * 1000.0)   # 5,000,000
    assert len(cf.rows) == 20
    assert [r.year for r in cf.rows][:2] == [2025, 2026]


def test_energy_margin_is_cash_and_excludes_the_degradation_charge():
    scenario = Scenario.from_dict(raw())
    result = year_result(margin_eur=500_000.0)
    assert cashflow.energy_margin_eur(scenario, result) == pytest.approx(500_000.0)
    # net_revenue_eur subtracts a non-cash charge, so the two must differ.
    assert result.net_revenue_eur < cashflow.energy_margin_eur(scenario, result)


def test_a_fee_separates_cash_margin_from_gross_revenue():
    d = raw()
    d["revenue_streams"]["day_ahead"]["fees_eur_per_mwh"] = 5.0
    scenario = Scenario.from_dict(d)
    result = year_result(margin_eur=500_000.0, discharged_mwh=11_526.0)
    margin = cashflow.energy_margin_eur(scenario, result)
    assert margin == pytest.approx(500_000.0 - 5.0 * 11_526.0)
    assert margin < result.gross_revenue_eur


def test_annual_fade_is_calendar_plus_cyclic():
    scenario = Scenario.from_dict(raw())
    result = year_result(discharged_mwh=11_526.0)
    cycles = 11_526.0 / 20.0
    assert cashflow.annual_fade_fraction(scenario, result) == pytest.approx(
        0.015 + 0.00005 * cycles
    )


def test_capacity_fades_and_augmentation_restores_it():
    cf = cashflow.build(Scenario.from_dict(raw()), year_result())
    fractions = [r.capacity_fraction for r in cf.rows]
    assert fractions[0] < 1.0
    spends = [r.augmentation_eur for r in cf.rows]
    assert sum(1 for s in spends if s > 0) <= 2          # max_events
    first = next(i for i, s in enumerate(spends) if s > 0)
    assert fractions[first] == pytest.approx(1.0)        # restore_to_capacity_fraction
    # The trigger compares against capacity *before* the event, which is the only
    # value that can be below the threshold; the year before is still above it,
    # which is precisely why no event fired then.
    assert cf.rows[first].capacity_before_augmentation_fraction <= 0.80
    assert fractions[first - 1] > 0.80


def test_revenue_scales_with_remaining_capacity():
    cf = cashflow.build(Scenario.from_dict(raw()), year_result(margin_eur=500_000.0))
    row = cf.rows[0]
    assert row.energy_margin_eur == pytest.approx(500_000.0 * row.capacity_fraction)


def test_tax_is_zero_while_depreciation_shelters_the_year():
    """A 5m capex over 20 years shelters 250k/y, which exceeds this EBITDA."""
    cf = cashflow.build(Scenario.from_dict(raw()), year_result(margin_eur=200_000.0))
    assert cf.rows[0].depreciation_eur == pytest.approx(5_000_000.0 / 20.0)
    assert cf.rows[0].taxable_income_eur < 0
    assert cf.rows[0].tax_eur == 0.0


def test_tax_is_charged_once_income_exceeds_the_shield():
    cf = cashflow.build(Scenario.from_dict(raw()), year_result(margin_eur=3_000_000.0))
    row = cf.rows[0]
    assert row.taxable_income_eur > 0
    assert row.tax_eur == pytest.approx(0.30 * row.taxable_income_eur)


def test_a_shorter_tax_life_front_loads_the_shield():
    """The sensitivity the scenario comment flags as resting on a placeholder."""
    slow = cashflow.build(Scenario.from_dict(raw()), year_result(margin_eur=3_000_000.0))
    d = raw()
    d["finance"]["depreciation"]["years"] = 10
    fast = cashflow.build(Scenario.from_dict(d), year_result(margin_eur=3_000_000.0))
    assert fast.rows[0].depreciation_eur > slow.rows[0].depreciation_eur
    assert fast.rows[0].tax_eur < slow.rows[0].tax_eur
    assert fast.irr_post_tax > slow.irr_post_tax


def test_pre_and_post_tax_irr_differ_once_tax_bites():
    cf = cashflow.build(Scenario.from_dict(raw()), year_result(margin_eur=3_000_000.0))
    assert cf.irr_pre_tax is not None and cf.irr_post_tax is not None
    assert cf.irr_post_tax < cf.irr_pre_tax


def test_zero_tax_rate_collapses_the_two_irrs():
    d = raw()
    d["finance"]["tax_rate"] = 0.0
    cf = cashflow.build(Scenario.from_dict(d), year_result(margin_eur=3_000_000.0))
    assert cf.irr_post_tax == pytest.approx(cf.irr_pre_tax)


def test_npv_is_zero_at_the_irr():
    cf = cashflow.build(Scenario.from_dict(raw()), year_result(margin_eur=3_000_000.0))
    assert cashflow.npv(cf.irr_post_tax, cf.post_tax_flows) == pytest.approx(0.0, abs=1.0)


def test_a_project_that_never_pays_back_has_no_irr():
    """None rather than 0: printing 0% would read as break-even."""
    cf = cashflow.build(Scenario.from_dict(raw()), year_result(margin_eur=1_000.0))
    assert cf.irr_post_tax is None
    assert cf.payback_years is None
    assert cf.npv_post_tax_eur < 0


def test_lcos_counts_costs_over_discounted_energy():
    cf = cashflow.build(Scenario.from_dict(raw()), year_result(margin_eur=3_000_000.0))
    lcos = cf.lcos_eur_per_mwh
    assert lcos is not None and lcos > 0
    # Capex alone over discounted energy is a floor the full measure must exceed.
    energy = sum(r.discharged_mwh / 1.07 ** i for i, r in enumerate(cf.rows, start=1))
    assert lcos > cf.capex_eur / energy


def test_every_name_in_outputs_metrics_resolves():
    cf = cashflow.build(Scenario.from_dict(raw()), year_result(margin_eur=3_000_000.0))
    for name in raw()["outputs"]["metrics"]:
        cf.metric(name)                                   # must not raise
    with pytest.raises(KeyError, match="no metric named"):
        cf.metric("irr")


def test_nominal_basis_and_leverage_are_refused_not_guessed():
    d = raw()
    d["finance"]["basis"] = "nominal"
    with pytest.raises(ScenarioError, match="not implemented"):
        Scenario.from_dict(d)
    d = raw()
    d["finance"]["debt"]["enabled"] = True
    with pytest.raises(ScenarioError, match="unlevered"):
        Scenario.from_dict(d)


def test_loss_carryforward_shelters_a_later_year():
    lean = copy.deepcopy(raw())
    lean["finance"]["loss_carryforward"] = False
    with_cf = cashflow.build(Scenario.from_dict(raw()), year_result(margin_eur=400_000.0))
    without = cashflow.build(Scenario.from_dict(lean), year_result(margin_eur=400_000.0))
    assert sum(r.tax_eur for r in with_cf.rows) <= sum(r.tax_eur for r in without.rows)


def test_running_past_end_of_life_is_detected():
    """The check Degradation.validate cannot do, because it sees calendar fade only.

    Augmentation is capped at max_events, so a heavily cycled pack exhausts its
    events and then keeps fading while the cashflow keeps booking revenue.
    """
    cf = cashflow.build(Scenario.from_dict(raw()), year_result(discharged_mwh=11_526.0))
    year = cf.first_past_end_of_life_year
    assert year is not None
    breached = next(r for r in cf.rows if r.year == year)
    assert breached.capacity_fraction < 0.70
    assert sum(1 for r in cf.rows if r.augmentation_eur > 0) == 2   # events exhausted


def test_a_lightly_cycled_pack_stays_within_its_life():
    cf = cashflow.build(Scenario.from_dict(raw()), year_result(discharged_mwh=2_000.0))
    assert cf.first_past_end_of_life_year is None
