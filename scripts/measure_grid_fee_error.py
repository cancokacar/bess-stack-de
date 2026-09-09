"""Measure what it costs to apply network charges after dispatch instead of inside it.

``finance.grid_fees`` charges an already-solved dispatch rather than re-solving
each project year under that year's tariff. This script measures the resulting
error, by running the same year twice per fee: once exempt, which is the dispatch
the shortcut keeps, and once with the fee in the objective, which is the dispatch
a re-solve would have produced.

The figures printed here are the ones quoted in README.md under "Post-exemption
grid fees are applied after dispatch, not inside it". Nothing in the test suite
asserts them -- a full year takes minutes, and ``tests/test_grid_fees.py``
deliberately pins only the *direction* of each bias, on a 4-day window, to stay
inside the "1-4 day windows" rule in CLAUDE.md. This script is how the magnitudes
get regenerated:

    .venv/bin/python scripts/measure_grid_fee_error.py            # regenerate
    .venv/bin/python scripts/measure_grid_fee_error.py --check    # detect drift

Two measurement conditions are load-bearing:

* **A full year, not a sample month.** January has the narrowest spreads in the
  synthetic series, so a fee bites hardest there and a January sample overstates
  the error.
* **mip_gap 0, not the scenario's 0.005.** A 0.5 % optimality gap is the same
  order as the smallest error being measured, so at the scenario default the
  5 EUR/MWh revenue figure would be indistinguishable from solver slack. The two
  runs being compared could sit at opposite edges of their gaps and manufacture a
  difference that is not there.

``RECORDED`` below duplicates the numbers in README.md, which is the price of
keeping the README's prose readable. ``--check`` exists so the duplication is
caught rather than trusted: when it fails, update both together.
"""

from __future__ import annotations

import argparse
import copy
import sys
import time
from pathlib import Path

import yaml

from bess_stack.config import Scenario
from bess_stack.data.prices import day_ahead_prices
from bess_stack.model.dispatch import DispatchResult, run_rolling_horizon

REFERENCE = Path(__file__).resolve().parents[1] / "scenarios" / "reference.yaml"

DEFAULT_FEES = (5.0, 15.0, 25.0)

# (revenue understated %, throughput overstated %) per fee, as quoted in README.md.
# Regenerate with this script and update both together.
RECORDED: dict[float, tuple[float, float]] = {
    5.0: (1.0, 13.3),
    15.0: (13.3, 50.9),
    25.0: (50.7, 101.1),
}

# Percentage points of tolerance when comparing a fresh run against RECORDED.
# The solve is deterministic at mip_gap 0, so this only absorbs rounding in the
# one-decimal figures the README quotes.
CHECK_TOLERANCE_PP = 0.15


def _scenario(raw: dict, mip_gap: float, charging_fee: float | None = None) -> Scenario:
    d = copy.deepcopy(raw)
    d["dispatch"]["solver"]["mip_gap"] = mip_gap
    if charging_fee is not None:
        d["grid"]["charging_fees_eur_per_mwh"] = charging_fee
    return Scenario.from_dict(d)


def _net_after_fee(result: DispatchResult, fee: float) -> float:
    """Net revenue with the fee charged on drawn energy.

    ``DispatchResult.net_revenue_eur`` values dispatch at raw prices less
    degradation, so the fee is subtracted here whether or not the optimizer saw
    it. That is what makes the two runs comparable.
    """
    return result.net_revenue_eur - fee * result.charged_mwh


def measure(
    fee: float, unaware: DispatchResult, raw: dict, mip_gap: float, hours: int | None
) -> tuple[float, float]:
    """Return (revenue understated %, throughput overstated %) for `fee`."""
    aware_scenario = _scenario(raw, mip_gap, charging_fee=fee)
    aware = run_rolling_horizon(aware_scenario, day_ahead_prices(aware_scenario, hours))

    post_processed = _net_after_fee(unaware, fee)
    re_solved = _net_after_fee(aware, fee)
    if re_solved == 0.0 or aware.throughput_mwh == 0.0:
        raise RuntimeError(
            f"re-solved run at {fee} EUR/MWh is degenerate "
            f"(net {re_solved:.1f} EUR, throughput {aware.throughput_mwh:.1f} MWh); "
            "the percentages below that point are meaningless"
        )

    revenue_pct = (re_solved - post_processed) / abs(re_solved) * 100.0
    throughput_pct = (
        (unaware.throughput_mwh - aware.throughput_mwh) / aware.throughput_mwh * 100.0
    )
    return revenue_pct, throughput_pct


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="days to model; default is the full synthetic year",
    )
    parser.add_argument(
        "--fees",
        type=float,
        nargs="+",
        default=list(DEFAULT_FEES),
        help="charging fees in EUR/MWh to measure",
    )
    parser.add_argument(
        "--mip-gap",
        type=float,
        default=0.0,
        help="solver gap; 0 by default, see the module docstring",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare against RECORDED and exit non-zero on drift",
    )
    args = parser.parse_args()

    hours = None if args.days is None else args.days * 24
    raw = yaml.safe_load(REFERENCE.read_text())

    span = "full synthetic year" if args.days is None else f"{args.days} days"
    print(f"Measuring on the {span} at mip_gap {args.mip_gap}.\n")

    started = time.monotonic()
    unaware_scenario = _scenario(raw, args.mip_gap)
    unaware = run_rolling_horizon(
        unaware_scenario, day_ahead_prices(unaware_scenario, hours)
    )
    print(
        f"exempt dispatch: {unaware.net_revenue_eur:>12,.0f} EUR net, "
        f"{unaware.throughput_mwh:>9,.0f} MWh discharged, "
        f"{unaware.charged_mwh:>9,.0f} MWh drawn "
        f"({time.monotonic() - started:.0f} s)\n"
    )

    print(f"{'fee EUR/MWh':>12}  {'revenue low %':>14}  {'throughput high %':>18}")
    results: dict[float, tuple[float, float]] = {}
    for fee in args.fees:
        revenue_pct, throughput_pct = measure(fee, unaware, raw, args.mip_gap, hours)
        results[fee] = (revenue_pct, throughput_pct)
        print(f"{fee:>12.0f}  {revenue_pct:>14.1f}  {throughput_pct:>18.1f}")

    print(f"\nTotal {time.monotonic() - started:.0f} s.")

    if not args.check:
        return 0

    if args.days is not None:
        print("\n--check compares against full-year figures; re-run without --days.")
        return 2

    drifted = []
    for fee, (revenue_pct, throughput_pct) in results.items():
        recorded = RECORDED.get(fee)
        if recorded is None:
            continue
        if (
            abs(revenue_pct - recorded[0]) > CHECK_TOLERANCE_PP
            or abs(throughput_pct - recorded[1]) > CHECK_TOLERANCE_PP
        ):
            drifted.append((fee, recorded, (revenue_pct, throughput_pct)))

    if drifted:
        print("\nDRIFT from the figures recorded in README.md:")
        for fee, (rec_r, rec_t), (got_r, got_t) in drifted:
            print(
                f"  {fee:.0f} EUR/MWh: README says {rec_r:.1f} / {rec_t:.1f}, "
                f"measured {got_r:.1f} / {got_t:.1f}"
            )
        print("Update RECORDED and the README paragraph together.")
        return 1

    print("\nMatches the figures recorded in README.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
