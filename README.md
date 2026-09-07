# bess-stack-de

Revenue-stacking model for battery energy storage systems (BESS) in Germany.

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
