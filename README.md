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
- **Grid fee exemption is an assumption.** `grid.grid_fee_exemption` is set true
  on the basis of the storage exemption under section 118(6) EnWG. It is a
  material IRR driver and must be verified for the modelled year.
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
