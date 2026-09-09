# bess-stack-de

Revenue-stacking model for battery energy storage systems (BESS) in Germany.

A degradation-aware, rolling-horizon dispatch optimizer for a single grid-connected BESS in the German market, co-optimizing day-ahead arbitrage, FCR capacity, and aFRR capacity + energy, that outputs an annual revenue stack and feeds a project-level IRR calculation.

## Out of scope for v1

These are deliberate exclusions, not oversights. Each is excluded because
modelling it with the data available would produce a number that looks
authoritative and is not.

- **Intraday continuous trading.** Cannot be modelled honestly without order
  book data; a naive intraday model mostly measures its own assumptions.
- **Imbalance / reBAP exposure.** Requires a position-and-deviation model and a
  view on balancing group behaviour, neither of which is in scope.
- **Multi-asset portfolios.** One grid-connected BESS at one point of
  interconnection. No portfolio pooling, no shared prequalification.
- **AC/DC topology detail.** The battery is a single power/energy abstraction at
  the POI, with round-trip efficiency and a parasitic load standing in for
  inverter, transformer and BOP behaviour.

## Known limitations

Simplifications inside the modelled scope that a reader should price in:

- **Reserve capacity prices are flat scalars.** FCR and aFRR capacity prices are
  single values, so the reserve-versus-arbitrage decision never sees the joint
  distribution of day-ahead spread and reserve prices. This biases the revenue
  stack systematically; replacing the scalars with price series is the first
  upgrade after v1.
- **aFRR energy is an expected activation rate.** Activation is a deterministic
  fraction of awarded capacity rather than a settled activation signal, so aFRR
  energy never binds state of charge the way real activation does.
- **Perfect foresight is the default.** `dispatch.forecast.kind: perfect` makes
  reported arbitrage revenue an upper bound, not an expectation. The
  forecast-error modes exist in the schema but are uncalibrated.
- **Reserve market price provenance is unresolved.** `market.price_source`
  covers day-ahead only; FCR and aFRR clearing results come from a separate
  source and are currently placeholders.
- **The grid fee exemption may not survive the horizon.**
  `grid.grid_fee_exemption_until_year` is set to 2044, the last exempt year of
  the 20 years from a 2025 commissioning under section 118(6) EnWG — exactly
  the modelled life, so the reference case never pays a post-exemption charge.
  Lowering it is how the risk gets stressed. That end year is a risk rather
  than a fact: sentence 12 of the provision empowers the Bundesnetzagentur to
  deviate from it, including its temporal scope, and in the AgNes proceeding
  (Orientierungspunkte, 30 January 2026) the agency states that a full
  exemption is not sustainable under EU law and that ending it early at a
  cut-off date would be legally possible, with protection of legitimate
  expectations still under examination. The successor regime is expected to be
  a capacity charge plus an energy charge levied only on storage losses; the
  `grid.post_exemption` fields carry that structure but are zeroed, because the
  agency has published a design and no figures. This is a material IRR driver
  and the single assumption most worth tracking.
- **Post-exemption grid fees are applied after dispatch, not inside it.**
  `finance.grid_fees` charges an already-solved year rather than re-solving it
  under that year's tariff, because re-solving every project year multiplies an
  81-second run by the project life. The shortcut biases the two halves of the
  answer in opposite directions. Revenue is *understated*: a dispatch optimised
  without the fee is still feasible once the fee exists, so it can only score
  worse than one re-optimised against it, which errs against the project rather
  than for it. Throughput and cycle count are *overstated*, because the marginal
  cycles the fee should have suppressed are all still in there. The second is the
  dangerous one, since the augmentation trigger keys off cycle count and will
  fire too early on an overstated one.
  Measured on the synthetic year, against a re-solved run: a 5 EUR/MWh charging
  fee understates revenue by 1.0 % but overstates throughput by 13.3 %; at
  15 EUR/MWh it is 13.3 % and 50.9 %; at 25 EUR/MWh, 50.7 % and 101.1 %. Note the
  asymmetry. At the fee where revenue still looks safe the cycle count is already
  13 % high, so the metric that reassures you is not the metric that breaks first.
  The error is exactly zero while the exemption holds, which is the whole of the
  reference case, so nothing today depends on it. Re-solve per year before
  anything does.
  Those figures are reproducible, not remembered:
  `scripts/measure_grid_fee_error.py` regenerates them in about six minutes, and
  `--check` fails if they have drifted from what this paragraph claims. Two
  conditions in that script are load-bearing. It measures a full year, because
  January has the narrowest spreads in the synthetic series and a fee bites
  hardest there, so a sample month overstates the error. And it solves at
  `mip_gap` 0 rather than the scenario's 0.005, because a 0.5 % optimality gap is
  the same order as the smallest error being measured — at the default gap the
  two runs could sit at opposite edges of their tolerances and manufacture a
  difference that is not there.

- **Tax is a single blended rate.** German trade tax varies with the municipal
  Hebesatz, and the depreciation life is a placeholder rather than a confirmed
  AfA figure, so `irr_post_tax` will move once the site and tax life are fixed.
  Both IRRs are reported by name; neither is quoted as plain "IRR".

## Layout

```
src/bess_stack/
├── data/      # market/price data loading and preparation
├── model/     # dispatch and revenue-stacking model
└── finance/   # cashflow, NPV/IRR, financing assumptions
scenarios/     # scenario definitions (YAML)
tests/
```

## Install

```bash
pip install -e ".[dev]"
```

## Usage

```python
from bess_stack import __version__
```

Scenarios live in `scenarios/`; `reference.yaml` is the baseline case.
