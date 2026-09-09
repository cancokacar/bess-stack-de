import copy

import numpy as np
import pytest
import yaml

from bess_stack.config import Scenario
from bess_stack.data.prices import day_ahead_prices, synthetic_day_ahead_hourly
from bess_stack.model.dispatch import run_rolling_horizon, solve_window

REFERENCE = "scenarios/reference.yaml"


@pytest.fixture
def raw():
    with open(REFERENCE) as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def scenario(raw):
    return Scenario.from_dict(raw)


def sawtooth(scenario, days=1, low=10.0, high=200.0):
    """Cheap night, expensive evening: an unambiguous arbitrage signal."""
    hourly = np.full(24 * days, 60.0)
    for day in range(days):
        hourly[day * 24 + 2 : day * 24 + 6] = low
        hourly[day * 24 + 18 : day * 24 + 22] = high
    return np.repeat(hourly, scenario.market.steps_per_hour)


def test_buys_low_and_sells_high(scenario):
    prices = sawtooth(scenario)
    charge, discharge, _ = solve_window(scenario, prices, soc_initial_mwh=2.0)
    dt = 1.0 / scenario.market.steps_per_hour
    assert np.average(prices, weights=charge + 1e-12) < np.average(
        prices, weights=discharge + 1e-12
    )
    assert discharge.sum() * dt > 0


def test_never_charges_and_discharges_at_once(scenario):
    charge, discharge, _ = solve_window(scenario, sawtooth(scenario), soc_initial_mwh=2.0)
    assert np.all((charge < 1e-6) | (discharge < 1e-6))


def test_state_of_charge_stays_inside_bounds(scenario):
    _, _, soc = solve_window(scenario, sawtooth(scenario), soc_initial_mwh=10.0)
    battery = scenario.battery
    assert soc.min() >= battery.soc_min * battery.energy_mwh - 1e-6
    assert soc.max() <= battery.soc_max * battery.energy_mwh + 1e-6


def test_power_is_constant_within_each_hourly_product_block(scenario):
    """Day-ahead settles hourly, so sub-hourly steps inside an hour must match."""
    charge, discharge, _ = solve_window(scenario, sawtooth(scenario), soc_initial_mwh=2.0)
    per_block = scenario.market.steps_per_hour
    for series in (charge, discharge):
        blocks = series.reshape(-1, per_block)
        assert np.allclose(blocks, blocks[:, [0]], atol=1e-6)


def test_power_never_exceeds_the_binding_limit(scenario):
    charge, discharge, _ = solve_window(scenario, sawtooth(scenario), soc_initial_mwh=2.0)
    cap = min(scenario.battery.power_mw, scenario.grid.connection_limit_mw)
    assert max(charge.max(), discharge.max()) <= cap + 1e-6


def test_degradation_cost_suppresses_marginal_cycling(raw):
    """The point of pricing throughput: a dearer cycle is a cycle not taken.

    400 EUR/MWh is far above the sawtooth spread, so it suppresses cycling
    outright. It also exceeds the replacement-cost bound in Degradation.validate,
    which is why the augmentation price moves with it: the two are tied by that
    invariant, so an absurd marginal cost needs the absurd replacement price that
    would justify it rather than a physically impossible scenario.
    """
    cheap = copy.deepcopy(raw)
    cheap["degradation"]["marginal_cost_eur_per_mwh"] = 0.0
    dear = copy.deepcopy(raw)
    dear["degradation"]["marginal_cost_eur_per_mwh"] = 400.0
    dear["degradation"]["augmentation"]["cost_eur_per_kwh"] = 8000.0  # bound: 0.05 * 8000

    cheap_scenario = Scenario.from_dict(cheap)
    prices = sawtooth(cheap_scenario, days=2, low=40.0, high=90.0)
    _, cheap_discharge, _ = solve_window(cheap_scenario, prices, 2.0)
    _, dear_discharge, _ = solve_window(Scenario.from_dict(dear), prices, 2.0)

    assert dear_discharge.sum() < cheap_discharge.sum()


def test_rolling_horizon_covers_every_step_and_carries_state(scenario):
    prices = sawtooth(scenario, days=3)
    result = run_rolling_horizon(scenario, prices)

    assert len(result.charge_mw) == len(prices)
    assert len(result.soc_mwh) == len(prices)
    assert result.throughput_mwh > 0
    assert result.net_revenue_eur == pytest.approx(
        result.gross_revenue_eur - result.degradation_cost_eur
    )
    # State carries across window boundaries rather than resetting.
    assert not np.allclose(result.soc_mwh[:: scenario.dispatch.commit_step_hours], 
                           scenario.battery.soc_initial * scenario.battery.energy_mwh)


def test_synthetic_prices_have_plausible_german_structure():
    hourly = synthetic_day_ahead_hourly(2025)
    assert len(hourly) == 8760
    by_hour = hourly.reshape(-1, 24).mean(axis=0)
    # Evening peak above midday, which is the shape arbitrage actually trades.
    assert by_hour[19] > by_hour[13]
    # Solar depression drives some hours negative without them being injected.
    assert (hourly < 0).any()


def test_price_series_matches_market_resolution(scenario):
    prices = day_ahead_prices(scenario, hours=48)
    assert len(prices) == 48 * scenario.market.steps_per_hour
