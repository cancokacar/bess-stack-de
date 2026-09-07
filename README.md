# bess-stack-de

Revenue-stacking model for battery energy storage systems (BESS) in Germany.

A degradation-aware, rolling-horizon dispatch optimizer for a single grid-connected BESS in the German market, co-optimizing day-ahead arbitrage, FCR capacity, and aFRR capacity + energy, that outputs an annual revenue stack and feeds a project-level IRR calculation.

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
