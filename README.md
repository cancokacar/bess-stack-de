# bess-stack-de

[![CI](https://github.com/Canny95/bess-stack-de/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Canny95/bess-stack-de/actions/workflows/ci.yml)

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
- **Reserve market prices cannot yet be redistributed.** Day-ahead provenance is
  settled — SMARD publishes under CC BY 4.0 — but the FCR and aFRR figures in
  `reference.yaml` remain invented placeholders, because regelleistung.net
  publishes its tender results under an all-rights-reserved notice with no open
  licence. This is a permission question, not an availability one. See
  [Data provenance](#data-provenance).
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

## Data provenance

Two sources, two very different licensing positions. The distinction decides what
may enter this repository, so it is recorded here verbatim rather than
summarised. Neither licence has been reviewed by a lawyer and nothing below is a
legal opinion.

### Day-ahead — SMARD (Bundesnetzagentur)

Licensed **CC BY 4.0**, stated on <https://www.smard.de/en/datennutzung>, which
permits redistribution and commercial use with attribution. The required credit
is `Bundesnetzagentur | SMARD.de`. The Bundesnetzagentur disclaims
responsibility for the correctness and completeness of the data.

Fetched from the unauthenticated chart API, one JSON file per week:

```
https://www.smard.de/app/chart_data/4169/DE-LU/index_quarterhour.json
https://www.smard.de/app/chart_data/4169/DE-LU/4169_DE-LU_quarterhour_<timestamp>.json
```

Filter `4169` is the wholesale day-ahead price, region `DE-LU`, resolution
`quarterhour`. The index returns 415 weekly start timestamps in epoch
milliseconds covering 2018-10-01 onward — the DE/LU market area's start date —
so two calendar years is roughly 104 requests. Verified by fetching it, not
inferred from documentation.

### Reserve — regelleistung.net (the four German TSOs)

The data exists and is downloadable. The [Datacenter](https://www.regelleistung.net/apps/datacenter/tenders/)
publishes Demands, Results and an Anonymous list of bids as XLSX for FCR, aFRR,
mFRR and ABLA, capacity and energy. FCR capacity clears in six four-hour blocks
(`NEGPOS_00_04` through `NEGPOS_20_24`), which is the structure
`revenue_streams.fcr.block_hours` already assumes.

What is missing is a licence. The [imprint](https://www.regelleistung.net/en-us/Imprint)
states:

> Contents and design of this website are protected by copyright. Reproduction
> of the website or parts thereof including but not limited to the contents of
> individual pages requires prior written permission from the German TSOs unless
> reproduction is authorised by law.

The site is operated jointly by 50Hertz Transmission GmbH, Amprion GmbH, TenneT
TSO GmbH and TransnetBW GmbH. There is no Creative Commons grant to point at, and
the stated default is restrictive.

**Consequence for this repository: no regelleistung-derived file may be
committed — not a fixture, not a sample, not a cached extract — until that
permission question has an answer.** Reading the data locally to calibrate is a
separate act from redistributing it, and only the second is blocked here.
Whether factual clearing prices attract copyright at all, whether the sui generis
database right applies, and whether the TSOs' publication obligation amounts to a
reuse permission are questions for someone qualified, not for this file.

One practical unknown remains: whether a date-range bulk export exists. The
Datacenter is a single-page app whose filter reads "Delivery day", singular, and
FCR tenders daily, so per-tender download would mean roughly 730 files for two
years. A programmatic route does appear to exist —
`/apps/cpp-publisher/api/v1/download/tenders/…` answers HTTP 400 rather than 404,
so the endpoint is real and the parameters were wrong.

## Prior art

Surveyed September 2026. Nothing found does exactly what this does, but the
scope decisions above were made against a known landscape rather than in
ignorance of one, and the neighbours are worth knowing before extending anything
here.

**Closest published analogue.** A comparison of Italian and German spot markets
(ScienceDirect `S2352467726002006`) models a 1 MW BESS across day-ahead energy
shifting plus FCR, aFRR and mFRR on German market structure, with a Python
implementation on GitHub archived to Zenodo. Same country, same stack. The
repository was identified from a search summary and has **not** been opened or
verified — treat it as a lead, not a citation. Structurally closer still is arXiv
`2609.03767`: an ageing-aware receding-horizon MILP over a two-day window
committing one day, evaluating four ageing-cost formulations. It diverges in
market and in output — Great Britain's NESO Dynamic Containment, Moderation and
Regulation rather than FCR and aFRR, and lifetime revenue rather than a
project-level IRR.

**Institutional tools with the same goal.** EPRI's StorageVET and DER-VET are
open-source Python, stack service values explicitly, and are unusually careful
about not double-counting one asset's capability across services — the trap that
`revenue_streams.fcr.reserve_energy_hours` exists to guard. Their service
definitions are US market constructs, so nothing there covers FCR or aFRR
prequalification. NREL's SAM is the technoeconomic reference, with degradation
driving battery replacement scheduling and an IRR over a chosen term: stronger
than this model on financial mechanics, weaker on ancillary co-optimisation.

**Degradation-aware dispatch.** Bolun Xu's work and code
(`bolunxu.github.io/codes`) is the antecedent of the `degradation.model` choice,
and the source of the claim in that field's comment that rainflow counting has no
analytical expression and cannot be embedded in the optimisation. *Evaluating
Battery Degradation Models in Rolling-Horizon BESS Arbitrage Optimization*
(`10.3390/en19041056`) compares Linear-Calendar, Energy-Throughput and
Cycle-Based rainflow models in this exact setting; `model: throughput` is their
Energy-Throughput class, so that paper is the direct evidence on what the choice
costs. NREL's BLAST-Lite (`github.com/NREL/BLAST-Lite`) is the empirical
lifetime-model library that would replace `calendar_fade_per_year` and
`cyclic_fade_per_full_cycle` with calibrated figures rather than placeholders.

**Frameworks deliberately not used.** PyPSA and PyPSA-DE, oemof.solph and
Calliope are the mature open frameworks in European energy modelling, and all
three can represent a battery. They are system-planning tools. Adopting one to
value a single asset's revenue stack would be a category error, not a shortcut —
see the scope discipline in CLAUDE.md.

**Commercial benchmark.** Modo Energy's ME BESS DE benchmark simulates a virtual
German battery cross-optimised across day-ahead, intraday, FCR and aFRR: this
model's output, computed on real market data. It is the natural external
validation target for the synthetic-price limitation recorded above. Not open
source.

**The gap.** None of the open-source tools carries German regulatory specifics —
the section 118(6) EnWG grid fee treatment, the AgNes successor regime, FCR
headroom under Art. 156(9) SO-VO. Those took primary-source verification to get
right here, and no upstream project will maintain them on this project's behalf.

## Layout

```
src/bess_stack/
├── cli.py     # run a scenario, print the revenue summary
├── data/      # market/price data loading and preparation
├── model/     # dispatch and revenue-stacking model
└── finance/   # cashflow, NPV/IRR, financing assumptions
scenarios/     # scenario definitions (YAML)
scripts/       # measurement and reporting entry points, run by hand
docs/          # written model: formulation.tex is canonical, .md is generated
tests/
.github/       # CI: lint and tests, plus the checks that keep docs/ honest
```

The optimization model is written out in [docs/formulation.tex](docs/formulation.tex):
notation, objective, and every constraint, each mapped to the scenario field it
reads and the line of `solve_window` it comes from. Section 2 is what runs;
section 3 formulates the FCR and aFRR products that the schema defines and the
code does not yet read. `docs/formulation.md` is generated from the `.tex` and is
there for reading in the browser; the `.tex` is the one to edit.

## Install

```bash
pip install -e ".[dev]"
```

## Usage

One scenario in, one revenue summary out:

```bash
.venv/bin/python scripts/run_scenario.py scenarios/reference.yaml            # ~81 s
.venv/bin/python scripts/run_scenario.py scenarios/reference.yaml --days 7   # ~2 s
```

`bess-stack-run scenarios/reference.yaml` is the same code, installed as a console
script; it appears once `pip install -e ".[dev]"` has been re-run, because the
installer is what generates it.

Abbreviated output for the reference case, measured rather than illustrative:

```
Dispatch
  gross revenue                473,374 EUR
  degradation cost              46,104 EUR
  net revenue                  427,270 EUR
  energy discharged             11,526 MWh
  energy charged                13,565 MWh
  equivalent full cycles         576.3
```

The full output carries five caveats, and they are the point of the command rather
than decoration on it:

- **Day-ahead only.** `revenue_streams.fcr` and `revenue_streams.afrr` are enabled in
  the reference scenario but `model.dispatch` does not implement them, so the figures
  are the day-ahead leg alone and understate the stack the scenario describes.
- **Returns repeat one modelled year.** `finance/cashflow.py` computes the
  `outputs.metrics` the scenario names, but from a single solved year whose margin
  is scaled by remaining capacity, not from a year-by-year re-solve. Revenue is
  scaled linearly with usable energy, which is conservative: the cycles a smaller
  battery declines are its least valuable.
- **The reference case outlives its own battery.** Capacity crosses
  `degradation.end_of_life_capacity_fraction` in 2041 with both permitted
  augmentation events already spent, so the last four years book revenue from a
  pack past end of life. The summary says so; the metrics do not correct for it.
- **Synthetic prices by default.** `market.price_source` is `synthetic`, so the
  headline figures measure the price generator rather than the German market.
  `smard` fetches real DE-LU prices; `entsoe` still raises.
- **Short runs are not annualised.** `--days N` models the first N days, which start
  in January — the narrowest spreads in the synthetic series — so pro-rating a short
  run understates the year. A week pro-rates to about 310,000 EUR against the
  427,270 measured above.

Scenarios live in `scenarios/`; `reference.yaml` is the baseline case. A bad path, a
malformed file or an inconsistent scenario exits 1 with one line on stderr; an
out-of-range `--days` exits 2.
