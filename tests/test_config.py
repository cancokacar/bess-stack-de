import copy

import pytest
import yaml

from bess_stack.config import Scenario, ScenarioError

REFERENCE = "scenarios/reference.yaml"


@pytest.fixture
def raw():
    with open(REFERENCE) as fh:
        return yaml.safe_load(fh)


def test_reference_scenario_loads():
    scenario = Scenario.from_yaml(REFERENCE)
    assert scenario.name == "reference"
    assert scenario.battery.round_trip_efficiency == pytest.approx(0.88, abs=0.01)
    assert scenario.market.steps_per_hour == 4


def test_broken_efc_identity_is_rejected(raw):
    """The identity the scenario file documents is enforced, not just commented."""
    broken = copy.deepcopy(raw)
    broken["degradation"]["cyclic_fade_per_full_cycle"] = 0.00008
    with pytest.raises(ScenarioError, match="equivalent full cycles"):
        Scenario.from_dict(broken)


def test_disabling_augmentation_under_heavy_calendar_fade_is_rejected(raw):
    broken = copy.deepcopy(raw)
    broken["degradation"]["augmentation"]["enabled"] = False
    broken["degradation"]["calendar_fade_per_year"] = 0.03  # 0.97^20 = 0.544, under the floor
    with pytest.raises(ScenarioError, match="augmentation is disabled"):
        Scenario.from_dict(broken)


def test_shortening_the_horizon_rescues_a_disabled_augmentation(raw):
    ok = copy.deepcopy(raw)
    ok["degradation"]["augmentation"]["enabled"] = False
    ok["degradation"]["calendar_fade_per_year"] = 0.03
    ok["finance"]["project_lifetime_years"] = 8
    assert Scenario.from_dict(ok).project_lifetime_years == 8


def test_calendar_only_check_does_not_clear_the_reference_case(raw):
    """Documents the limit of the check rather than overstating what it proves.

    At 1.5 %/y the reference keeps 0.985^20 = 0.739 on calendar fade alone, which
    is above the 0.70 floor, so disabling augmentation passes validation. Cyclic
    fade is what actually breaches the floor, and its size depends on how the
    battery is dispatched, which validation cannot see.
    """
    permissive = copy.deepcopy(raw)
    permissive["degradation"]["augmentation"]["enabled"] = False
    assert Scenario.from_dict(permissive).degradation.augmentation.enabled is False


def test_soc_initial_outside_bounds_is_rejected(raw):
    broken = copy.deepcopy(raw)
    broken["battery"]["soc"]["initial"] = 0.99
    with pytest.raises(ScenarioError, match="soc.initial"):
        Scenario.from_dict(broken)


def test_unimplemented_forecast_mode_is_rejected(raw):
    """The schema offers forecast modes that do not exist yet; saying so beats pretending."""
    broken = copy.deepcopy(raw)
    broken["dispatch"]["forecast"]["kind"] = "ar1"
    with pytest.raises(ScenarioError, match="not implemented"):
        Scenario.from_dict(broken)


def test_missing_field_names_its_path(raw):
    broken = copy.deepcopy(raw)
    del broken["battery"]["power_mw"]
    with pytest.raises(ScenarioError, match="battery.power_mw"):
        Scenario.from_dict(broken)


def test_marginal_cost_above_replacement_is_rejected(raw):
    """4.0 passes against a 6.00 bound; charging more than replacement does not."""
    broken = copy.deepcopy(raw)
    broken["degradation"]["marginal_cost_eur_per_mwh"] = 7.0
    with pytest.raises(ScenarioError, match="above the 6.00 EUR/MWh"):
        Scenario.from_dict(broken)


def test_marginal_cost_at_the_bound_is_accepted(raw):
    at_bound = copy.deepcopy(raw)
    at_bound["degradation"]["marginal_cost_eur_per_mwh"] = 6.0
    assert Scenario.from_dict(at_bound).degradation.marginal_cost_eur_per_mwh == 6.0


def test_marginal_cost_bound_tracks_the_augmentation_price(raw):
    """The bound is derived, not hardcoded: doubling the augmentation price doubles it."""
    dearer = copy.deepcopy(raw)
    dearer["degradation"]["augmentation"]["cost_eur_per_kwh"] = 240.0
    dearer["degradation"]["marginal_cost_eur_per_mwh"] = 7.0
    assert Scenario.from_dict(dearer).degradation.marginal_cost_eur_per_mwh == 7.0


def test_marginal_cost_is_unbounded_when_augmentation_is_disabled(raw):
    """Deliberately scoped: without augmentation the basis is finance.capex_eur_per_kwh,
    which config.py does not read, so no bound is asserted rather than a wrong one."""
    unbounded = copy.deepcopy(raw)
    unbounded["degradation"]["augmentation"]["enabled"] = False
    unbounded["degradation"]["marginal_cost_eur_per_mwh"] = 7.0
    unbounded["finance"]["project_lifetime_years"] = 10  # keep calendar fade in bounds
    assert Scenario.from_dict(unbounded).degradation.marginal_cost_eur_per_mwh == 7.0


def test_marginal_cost_far_below_the_discounted_band_is_not_flagged(raw):
    """Documents what this check does NOT prove. The discounted band is 3.82-4.41
    EUR/MWh, but the lower end depends on the augmentation date, which depends on
    the cycle count, which is an outcome of dispatch. So 0.5 loads without complaint
    even though it under-prices throughput badly."""
    cheap = copy.deepcopy(raw)
    cheap["degradation"]["marginal_cost_eur_per_mwh"] = 0.5
    assert Scenario.from_dict(cheap).degradation.marginal_cost_eur_per_mwh == 0.5


def test_disabling_day_ahead_is_rejected(raw):
    """The only stream dispatch implements cannot be switched off.

    `enabled` was the one DayAhead field no consumer read, so a scenario declaring the
    stream off still ran day-ahead dispatch and reported its revenue. Proves the flag is
    enforced, not merely parsed.
    """
    off = copy.deepcopy(raw)
    off["revenue_streams"]["day_ahead"]["enabled"] = False
    with pytest.raises(ScenarioError, match="day_ahead.enabled"):
        Scenario.from_dict(off)
