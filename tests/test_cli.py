import copy

import numpy as np
import pytest
import yaml

from bess_stack.cli import LABEL_WIDTH, VALUE_WIDTH, main, summary_lines
from bess_stack.config import Scenario
from bess_stack.model.dispatch import DispatchResult

REFERENCE = "scenarios/reference.yaml"


@pytest.fixture
def raw():
    with open(REFERENCE) as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def scenario(raw):
    return Scenario.from_dict(raw)


def result_priced(gross_eur: float, discharged_mwh: float, charged_mwh: float) -> DispatchResult:
    """A DispatchResult with a known gross revenue and energy, no solving needed.

    Discharge and charge sit in separate steps so the price applies to one leg at a
    time; the price is chosen to make gross revenue land exactly on `gross_eur`.
    """
    dt = 0.25
    price = gross_eur / discharged_mwh
    return DispatchResult(
        charge_mw=np.array([0.0, charged_mwh / dt]),
        discharge_mw=np.array([discharged_mwh / dt, 0.0]),
        soc_mwh=np.zeros(2),
        prices_eur_per_mwh=np.array([price, 0.0]),
        resolution_hours=dt,
        degradation_cost_eur_per_mwh=4.0,
    )


def charged_scenario(raw, energy_fee=15.0, capacity_fee=0.0, until=2030):
    """Reference case with the exemption lapsing mid-life and a real successor tariff."""
    d = copy.deepcopy(raw)
    d["grid"]["grid_fee_exemption_until_year"] = until
    d["grid"]["post_exemption"]["charging_fees_eur_per_mwh"] = energy_fee
    d["grid"]["post_exemption"]["capacity_charge_eur_per_kw_year"] = capacity_fee
    return Scenario.from_dict(d)


def test_money_carries_thousands_separators_and_units(scenario):
    result = result_priced(473_374.0, discharged_mwh=11_526.0, charged_mwh=13_565.0)
    lines = summary_lines(scenario, result, days=None, elapsed_s=81.0)
    text = "\n".join(lines)
    assert "473,374 EUR" in text
    assert "11,526 MWh" in text
    assert "Total 81 s." in text


def test_every_dispatch_value_ends_in_the_same_column(scenario):
    """The alignment invariant: one mis-set width would go unnoticed without this."""
    result = result_priced(473_374.0, discharged_mwh=11_526.0, charged_mwh=13_565.0)
    lines = summary_lines(scenario, result, days=None, elapsed_s=81.0)

    start = lines.index("Dispatch") + 1
    rows = [line for line in lines[start : start + 6]]
    assert len(rows) == 6

    edge = 2 + LABEL_WIDTH + VALUE_WIDTH
    for row in rows:
        # The number's last digit lands on `edge`, whatever its magnitude or decimals,
        # and the label before it never runs into the value field.
        assert row[edge - 1].isdigit(), row
        value = row[2 + LABEL_WIDTH : edge]
        assert value.lstrip() == value.strip(), row
        float(value.replace(",", ""))


def test_exempt_case_names_the_end_year_and_prints_no_per_year_rows(scenario):
    result = result_priced(473_374.0, discharged_mwh=11_526.0, charged_mwh=13_565.0)
    text = "\n".join(summary_lines(scenario, result, days=None, elapsed_s=81.0))
    assert "2044" in text
    assert "2025-2044" in text
    # A table of twenty exempt years would bury the assumption that matters.
    assert "2031" not in text
    assert "first charged year" not in text


def test_charged_case_names_the_first_charged_year_and_the_lower_bound(raw):
    s = charged_scenario(raw, energy_fee=15.0, capacity_fee=8.0, until=2030)
    result = result_priced(473_374.0, discharged_mwh=11_526.0, charged_mwh=13_565.0)
    text = "\n".join(summary_lines(s, result, days=None, elapsed_s=81.0))
    assert "first charged year" in text
    assert "2031" in text
    assert "lower bound" in text
    assert "net revenue after charges" in text


def test_unmodelled_streams_are_named_when_enabled(scenario):
    result = result_priced(473_374.0, discharged_mwh=11_526.0, charged_mwh=13_565.0)
    text = "\n".join(summary_lines(scenario, result, days=None, elapsed_s=81.0))
    assert "revenue_streams.fcr" in text
    assert "revenue_streams.afrr" in text


def test_unmodelled_caveat_disappears_when_the_streams_are_off(raw):
    d = copy.deepcopy(raw)
    d["revenue_streams"]["fcr"]["enabled"] = False
    d["revenue_streams"]["afrr"]["enabled"] = False
    s = Scenario.from_dict(d)
    result = result_priced(473_374.0, discharged_mwh=11_526.0, charged_mwh=13_565.0)
    text = "\n".join(summary_lines(s, result, days=None, elapsed_s=81.0))
    assert "Day-ahead only" not in text
    # The metrics disclaimer is unconditional and must survive.
    assert "Do not read the net revenue above as a return." in text


def test_metric_names_are_printed_verbatim_so_no_bare_irr_appears(scenario):
    result = result_priced(473_374.0, discharged_mwh=11_526.0, charged_mwh=13_565.0)
    text = "\n".join(summary_lines(scenario, result, days=None, elapsed_s=81.0))
    assert "irr_pre_tax" in text
    assert "irr_post_tax" in text


def test_days_out_of_range_is_rejected_before_any_solve(capsys):
    """Returns 2 immediately: no MILP runs, so this stays inside the fast suite."""
    assert main([REFERENCE, "--days", "400"]) == 2
    assert main([REFERENCE, "--days", "0"]) == 2
    assert capsys.readouterr().out == ""


def test_missing_scenario_exits_one_with_nothing_on_stdout(capsys):
    assert main(["no-such-scenario.yaml"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no-such-scenario.yaml" in captured.err
