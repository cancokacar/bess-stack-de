"""Parser tests for the SMARD loader.

Every case here runs against committed fixtures with no network. The fixtures
are real SMARD weeks chosen for the two days that break the synthetic
generator's assumption of a uniform 96-step day, and they are redistributable
because SMARD publishes under CC BY 4.0 (Bundesnetzagentur | SMARD.de).
"""

import copy
import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pytest
import yaml

from bess_stack.config import Scenario
from bess_stack.data import smard
from bess_stack.data.prices import day_ahead_prices

FIXTURES = Path("tests/data/smard")
SPRING = FIXTURES / "de-lu_2024-03-25_spring-forward-23h-day.json"
AUTUMN = FIXTURES / "de-lu_2024-10-21_autumn-back-25h-day.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def local_days(stamps: np.ndarray) -> dict:
    days: dict = {}
    for ts in stamps:
        day = datetime.fromtimestamp(int(ts) / 1000, smard.BERLIN).date()
        days[day] = days.get(day, 0) + 1
    return days


def test_spring_forward_day_has_23_hours():
    """The clocks skip an hour, so 31 March 2024 is 92 quarter-hours, not 96."""
    stamps, prices = smard.parse_week(load(SPRING))
    assert len(stamps) == len(prices) == 668          # 167 h, not 168
    counts = local_days(stamps)
    assert counts[date(2024, 3, 31)] == 92
    assert set(counts.values()) == {96, 92}


def test_autumn_back_day_has_25_hours():
    """The clocks repeat an hour, so 27 October 2024 is 100 quarter-hours."""
    stamps, prices = smard.parse_week(load(AUTUMN))
    assert len(stamps) == len(prices) == 676          # 169 h, not 168
    counts = local_days(stamps)
    assert counts[date(2024, 10, 27)] == 100
    assert set(counts.values()) == {96, 100}


@pytest.mark.parametrize("path", [SPRING, AUTUMN])
def test_epoch_step_is_constant_across_both_transitions(path):
    """The invariant that makes DST a non-event: local clocks jump, epoch does not."""
    stamps, _ = smard.parse_week(load(path))
    assert np.all(np.diff(stamps) == smard.STEP_MS)
    smard.check_contiguous(stamps)                     # must not raise


def test_duplicated_local_hour_is_two_distinct_timestamps():
    """02:00-03:00 occurs twice on 27 October; epoch keeps them apart."""
    stamps, _ = smard.parse_week(load(AUTUMN))
    local = [datetime.fromtimestamp(int(t) / 1000, smard.BERLIN) for t in stamps]
    repeated = [d for d in local if d.date() == date(2024, 10, 27) and d.hour == 2]
    assert len(repeated) == 8                          # two distinct 02:00 hours
    assert len({d.utcoffset() for d in repeated}) == 2  # CEST then CET
    assert len({int(t) for t in stamps}) == len(stamps)


def test_stitching_drops_the_duplicate_at_a_seam():
    week = smard.parse_week(load(SPRING))
    stamps, prices = smard.stitch([week, week])
    assert len(stamps) == 668
    assert np.array_equal(stamps, week[0])
    assert np.array_equal(prices, week[1])


def test_null_prices_survive_parsing_as_nan():
    """The parser records a gap; deciding what it means belongs to the caller."""
    payload = copy.deepcopy(load(SPRING))
    payload["series"][10][1] = None
    _, prices = smard.parse_week(payload)
    assert np.isnan(prices[10])
    assert not np.isnan(prices[9])


def test_a_break_in_the_series_is_reported_with_its_local_time():
    stamps, _ = smard.parse_week(load(SPRING))
    gapped = np.delete(stamps, 100)
    with pytest.raises(smard.SmardError, match="not contiguous"):
        smard.check_contiguous(gapped)


def test_payload_without_a_series_is_rejected():
    with pytest.raises(smard.SmardError, match="not a SMARD chart file"):
        smard.parse_week({"meta_data": {}})


def test_year_length_accounts_for_the_leap_day():
    """2024 is a leap year; DST nets out, the extra day does not."""
    assert smard.expected_steps(2024) == 366 * 96
    assert smard.expected_steps(2025) == 365 * 96
    assert smard.expected_steps(2024) - smard.expected_steps(2025) == 96


def test_url_construction():
    assert smard.series_url(1711321200000).endswith(
        "/4169/DE-LU/4169_DE-LU_quarterhour_1711321200000.json"
    )
    assert smard.index_url().endswith("/4169/DE-LU/index_quarterhour.json")


def _scenario(**market):
    with open("scenarios/reference.yaml") as fh:
        raw = yaml.safe_load(fh)
    raw["market"].update(market)
    return Scenario.from_dict(raw)


def test_entsoe_still_raises_and_names_what_is_available():
    with pytest.raises(NotImplementedError, match="synthetic' and 'smard'"):
        day_ahead_prices(_scenario(price_source="entsoe"))


def test_smard_refuses_a_resolution_it_does_not_serve():
    scenario = _scenario(price_source="smard", resolution_minutes=60)
    with pytest.raises(smard.SmardError, match="quarter-hourly"):
        smard.day_ahead_prices(scenario)


def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for key, value in d.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def test_real_price_scenario_asks_for_smard_at_the_resolution_it_serves():
    """Config only, deliberately.

    Dispatching de-lu-2024.yaml needs 54 cached weeks or 54 fetches, neither of
    which belongs in a test suite that has to run offline.
    """
    with open("scenarios/de-lu-2024.yaml") as fh:
        scenario = Scenario.from_dict(yaml.safe_load(fh))
    assert scenario.market.price_source == "smard"
    assert scenario.market.resolution_minutes == 15
    assert scenario.market.year == 2024
    # 2024 is a leap year and both DST transitions fall inside it; they cancel.
    assert smard.expected_steps(2024) == 366 * 96


def test_real_price_scenario_differs_from_the_reference_only_in_the_price_series():
    """The reason the two runs can be compared at all.

    de-lu-2024.yaml exists to isolate the effect of swapping the synthetic
    generator for published prices. That only holds while every other field is
    identical, and two scenario files maintained by hand drift. This test is
    what keeps the claim in that file's header true.
    """
    with open("scenarios/reference.yaml") as fh:
        reference = _flatten(yaml.safe_load(fh))
    with open("scenarios/de-lu-2024.yaml") as fh:
        real = _flatten(yaml.safe_load(fh))

    assert set(reference) == set(real), "the two scenarios no longer describe the same fields"
    differing = {k for k in reference if reference[k] != real[k]}
    assert differing == {"name", "description", "market.year", "market.price_source"}
