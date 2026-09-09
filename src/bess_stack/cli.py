"""Run one scenario and print its revenue summary.

This is the only user-facing entry point. It loads a scenario, solves the dispatch and
prints what came out, and it is written to be read by someone who has not read the code.

That last part is most of the design. Day-ahead is the only stream ``model.dispatch``
implements, and nothing in ``finance/`` computes the return metrics that
``scenarios/reference.yaml`` names in ``outputs.metrics``. A summary that printed a net
revenue figure without saying either of those things would be the failure the README's
scope section is written against -- a number that looks authoritative and is not. So the
caveat naming the missing streams is printed *above* the figures rather than below them,
and the metrics that do not exist are named rather than omitted, because a reader who has
seen the scenario file arrives expecting an IRR and silence invites them to read net
revenue as one.

The implementation lives here rather than in ``scripts/`` because
``[tool.hatch.build.targets.wheel]`` packages only ``src/bess_stack``: a
``[project.scripts]`` entry point must resolve to an installed module, so
``scripts/run_scenario.py`` is a shim over this and the two cannot drift apart.

    .venv/bin/python scripts/run_scenario.py scenarios/reference.yaml            # ~81 s
    .venv/bin/python scripts/run_scenario.py scenarios/reference.yaml --days 7   # ~2 s

Or, after ``pip install -e ".[dev]"`` regenerates the console script::

    .venv/bin/bess-stack-run scenarios/reference.yaml --days 7
"""

from __future__ import annotations

import argparse
import sys
import textwrap
import time

import yaml

from bess_stack.config import Scenario, ScenarioError
from bess_stack.data.prices import day_ahead_prices
from bess_stack.finance.grid_fees import (
    annual_grid_fee_charges,
    first_charged_year,
    net_revenue_after_grid_fees,
)
from bess_stack.model.dispatch import DispatchResult, run_rolling_horizon

# The synthetic series is one 8760 h year. Past that, day_ahead_prices slices
# hourly[:hours] and silently returns the whole year, so a larger --days would label a
# full-year answer as something shorter.
FULL_YEAR_DAYS = 365

# Widest label is "equivalent full cycles"; the value field is wide enough for a
# nine-figure euro number with separators. Together they put the right edge of every
# number in the same column, which is the property tests/test_cli.py pins.
LABEL_WIDTH = 24
VALUE_WIDTH = 12

# Streams the scenario schema carries that model.dispatch does not implement.
UNMODELLED_STREAMS = ("fcr", "afrr")

# Prose is wrapped rather than hand-broken, because most of it interpolates values whose
# width is not known here -- the metric list and the stream names both vary in length.
WRAP_WIDTH = 78


def _wrap(text: str, indent: str = "") -> list[str]:
    return textwrap.wrap(
        " ".join(text.split()),
        width=WRAP_WIDTH,
        initial_indent=indent,
        subsequent_indent=indent,
        # Both off so that hyphenated prose ("pro-rata") and long identifiers
        # ("lcos_eur_per_mwh", "revenue_streams.afrr") survive intact -- a metric name
        # broken across two lines is not greppable.
        break_on_hyphens=False,
        break_long_words=False,
    )


def _row(label: str, value: float, unit: str = "", decimals: int = 0) -> str:
    return f"  {label:<{LABEL_WIDTH}}{value:>{VALUE_WIDTH},.{decimals}f} {unit}".rstrip()


def _int_row(label: str, value: int) -> str:
    """A row whose value is a count or a year, so it takes no thousands separator."""
    return f"  {label:<{LABEL_WIDTH}}{value:>{VALUE_WIDTH}d}"


def _join(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _preamble_lines(scenario: Scenario, days: int | None) -> list[str]:
    market, dispatch, battery = scenario.market, scenario.dispatch, scenario.battery
    span = "full year" if days is None else f"first {days} days"
    lines = _wrap(
        f"Scenario '{scenario.name}' on {market.price_source} {market.bidding_zone} prices "
        f"for {market.year}: {battery.power_mw:g} MW / {battery.energy_mwh:g} MWh, {span} at "
        f"{market.resolution_minutes} min resolution, {dispatch.mode} over "
        f"{dispatch.horizon_hours} h committing {dispatch.commit_step_hours} h, "
        f"mip_gap {dispatch.solver.mip_gap:g}."
    )
    if days is not None:
        lines += _wrap(
            "Figures cover the modelled span and are not annualised: the span starts in "
            "January, which has the narrowest spreads in the synthetic series, so a "
            "pro-rata year would come out low."
        )
    return lines


def _unmodelled_lines(scenario: Scenario) -> list[str]:
    """The caveat naming streams the scenario enables that dispatch cannot model."""
    streams = scenario.raw.get("revenue_streams", {})
    enabled = [
        f"revenue_streams.{name}"
        for name in UNMODELLED_STREAMS
        if streams.get(name, {}).get("enabled", False)
    ]
    if not enabled:
        return []
    verb = "is" if len(enabled) == 1 else "are"
    them = "it" if len(enabled) == 1 else "them"
    return _wrap(
        f"Day-ahead only. {_join(enabled)} {verb} enabled in this scenario but "
        f"model.dispatch does not implement {them}, so every figure below is the "
        "day-ahead leg alone and understates the stack the scenario describes."
    )


def _dispatch_lines(scenario: Scenario, result: DispatchResult) -> list[str]:
    return [
        "Dispatch",
        _row("gross revenue", result.gross_revenue_eur, "EUR"),
        _row("degradation cost", result.degradation_cost_eur, "EUR"),
        _row("net revenue", result.net_revenue_eur, "EUR"),
        _row("energy discharged", result.throughput_mwh, "MWh"),
        _row("energy charged", result.charged_mwh, "MWh"),
        # One decimal, because a short run turns 10.8 cycles into 11 and loses the point.
        _row("equivalent full cycles", result.equivalent_full_cycles(scenario.battery.energy_mwh),
             decimals=1),
    ]


def _network_charge_lines(scenario: Scenario, result: DispatchResult) -> list[str]:
    first = first_charged_year(scenario)
    last_year = scenario.market.year + scenario.project_lifetime_years - 1

    if first is None:
        # Naming the end year matters more than printing a zero: CLAUDE.md calls it the
        # single assumption most worth tracking, and twenty rows of 0 EUR would hide it.
        until = scenario.grid.grid_fee_exemption_until_year
        return ["Network charges"] + _wrap(
            f"The section 118(6) EnWG exemption runs to {until} and the modelled life is "
            f"{scenario.market.year}-{last_year}, so no project year is charged. Network "
            "charges are 0 EUR in every year and net revenue after charges equals the net "
            "revenue above.",
            indent="  ",
        )

    charged = [c for c in annual_grid_fee_charges(scenario, result) if not c.exempt]
    head = charged[0]
    lines = [
        "Network charges",
        _int_row("first charged year", head.year),
        _int_row("charged years", len(charged)),
        _row("energy charge", head.energy_charge_eur, "EUR"),
        _row("capacity charge", head.capacity_charge_eur, "EUR"),
        _row("net revenue after charges",
             net_revenue_after_grid_fees(scenario, result, head.year), "EUR"),
    ]
    # Today the tariff is constant across charged years, so this never fires. It exists so
    # that a per-year term in charging_fees_in cannot make the display silently wrong.
    totals = {round(c.total_eur, 6) for c in charged}
    if len(totals) > 1:
        lines += _wrap(
            f"charge varies across charged years: {min(totals):,.0f} to {max(totals):,.0f} EUR",
            indent="  ",
        )
    lines += _wrap(
        "Figures are for the first charged year. A lower bound: the dispatch was not "
        "re-optimised against the fee, so revenue is understated while the throughput and "
        "cycle count above are overstated. See finance/grid_fees.py.",
        indent="  ",
    )
    return lines


def _not_computed_lines(scenario: Scenario) -> list[str]:
    metrics = scenario.raw.get("outputs", {}).get("metrics", [])
    lines = ["Not computed"]
    if metrics:
        # Read from the file rather than hardcoded, so this stays true if the list
        # changes, and so the names appear verbatim -- no bare "irr" is ever emitted.
        body = (
            f"outputs.metrics names {_join([str(m) for m in metrics])}. Nothing in "
            "finance/ computes them, so this stops at annual net revenue."
        )
    else:
        body = "No return metrics are computed; this stops at annual net revenue."
    lines += _wrap(f"{body} Do not read the net revenue above as a return.", indent="  ")
    return lines


def summary_lines(
    scenario: Scenario,
    result: DispatchResult,
    days: int | None,
    elapsed_s: float,
) -> list[str]:
    """The whole report, as lines. Pure: takes a solved result and does no I/O."""
    blocks = [
        _preamble_lines(scenario, days),
        _unmodelled_lines(scenario),
        _dispatch_lines(scenario, result),
        _network_charge_lines(scenario, result),
        _not_computed_lines(scenario),
        [f"Total {elapsed_s:.0f} s."],
    ]
    lines: list[str] = []
    for block in blocks:
        if not block:
            continue
        if lines:
            lines.append("")
        lines += block
    return lines


def main(argv: list[str] | None = None) -> int:
    # argv is a parameter so the exit codes are testable without a subprocess; argparse
    # treats None exactly as it treats sys.argv[1:].
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "scenario",
        help="path to a scenario YAML; scenarios/ is not installed, so there is no default",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help=f"days to model; default is the full synthetic year, maximum {FULL_YEAR_DAYS}",
    )
    args = parser.parse_args(argv)

    if args.days is not None and not 1 <= args.days <= FULL_YEAR_DAYS:
        print(
            f"error: --days must be between 1 and {FULL_YEAR_DAYS}; past that the synthetic "
            "series runs out and a full year would be returned under a shorter label",
            file=sys.stderr,
        )
        return 2

    try:
        scenario = Scenario.from_yaml(args.scenario)
    except (FileNotFoundError, IsADirectoryError):
        # str() on these gives an errno string, so name the path explicitly.
        print(f"error: cannot read scenario '{args.scenario}'", file=sys.stderr)
        return 1
    except yaml.YAMLError as exc:
        print(f"error: {args.scenario} is not valid YAML: {exc}", file=sys.stderr)
        return 1
    except ScenarioError as exc:
        # These messages are written to be read by a human; a traceback would bury them.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    hours = None if args.days is None else args.days * 24
    started = time.monotonic()
    try:
        prices = day_ahead_prices(scenario, hours)
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # A RuntimeError out of solve_window is a model failure rather than user error, and
    # is deliberately left to raise.
    result = run_rolling_horizon(scenario, prices)

    for line in summary_lines(scenario, result, args.days, time.monotonic() - started):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
