<!-- GENERATED FILE. Source: docs/formulation.tex -- edit that, not this.
     Regenerate with the pandoc command in CLAUDE.md.
     Equation numbers do not survive the conversion; the numbers used in the prose
     ((2.1), (2.4), ...) match the compiled PDF, not anything numbered below. -->

# Scope and Conventions

This document states the optimization model implemented in
`./src/bess_stack/model/dispatch.py`. The project models

> a degradation-aware, rolling-horizon dispatch optimizer for a single
> grid-connected BESS in the German market, co-optimizing day-ahead
> arbitrage, FCR capacity, and aFRR capacity + energy, that outputs an
> annual revenue stack and feeds a project-level IRR calculation.

**Section 2 describes what is implemented. Section 3 describes what is
not.** Every equation in Section 2 corresponds to a statement in
`solve_window`; no equation there describes intended behaviour. Section
3 formulates the reserve products from the parameters already present in
`scenarios/reference.yaml`, none of which the code reads today. The
distinction is maintained deliberately: a formulation that does not say
which half is running is not a specification but an advertisement.

## Units and sign conventions

Power is in MW, energy in MWh, prices and costs in EUR/MWh, and capacity
prices in EUR/MW/h. One time step spans $`\Delta t`$ hours. Charging and
discharging power are separate non-negative variables rather than one
signed variable, which is what allows the complementarity condition of
constraint group B1 to be written as a linear constraint. State of
charge is carried as an energy $`E_t`$ in MWh rather than a percentage;
the scenario file states its bounds as fractions and the model
multiplies them by the nominal capacity on load.

# Day-Ahead Arbitrage Model (Implemented)

## Index sets

| Symbol | Description |
|:---|:---|
| $`t \in \mathcal{T}`$ | Time steps within one optimization window, $`\mathcal{T} = \{0, 1, \dots, N-1\}`$ |
| $`\mathcal{B}_j`$ | Steps belonging to day-ahead product block $`j`$, so that $`\bigcup_j \mathcal{B}_j = \mathcal{T}`$ |

## Parameters

Each parameter names the field it is read from, so that the document and
the configuration cannot drift apart unnoticed. Source fields are dotted
paths into `scenarios/reference.yaml`; those beginning with a stream
name (`day_ahead`, `fcr`, `afrr`) are relative to `revenue_streams`.

| Symbol | Description | Unit | Source field |
|:---|:---|:---|:---|
| $`\Delta t`$ | Length of one time step | h | `market.resolution_minutes` |
| $`\pi_t`$ | Day-ahead price in step $`t`$ | EUR/MWh | price series argument |
| $`\kappa`$ | Spread retention, $`1 - \text{haircut}`$ | – | `day_ahead.spread_haircut` |
| $`f`$ | Trading fee per MWh traded | EUR/MWh | `day_ahead.fees_eur_per_mwh` |
| $`g^{c}`$ | Network fee on energy charged | EUR/MWh | `grid.charging_fees_eur_per_mwh` |
| $`g^{d}`$ | Network fee on energy discharged | EUR/MWh | `grid.discharging_fees_eur_per_mwh` |
| $`c^{\deg}`$ | Marginal degradation cost | EUR/MWh | `degradation.marginal_cost_eur_per_mwh` |
| $`\bar{P}`$ | Binding power cap | MW | $`\min`$(`battery.power_mw`, `grid.connection_limit_mw`) |
| $`E^{\text{nom}}`$ | Nominal usable energy | MWh | `battery.energy_mwh` |
| $`\eta^{c}`$ | One-way charging efficiency | – | `battery.efficiency.charge` |
| $`\eta^{d}`$ | One-way discharging efficiency | – | `battery.efficiency.discharge` |
| $`\sigma`$ | Self-discharge, fraction of $`E^{\text{nom}}`$ per day | 1/d | `battery.efficiency.self_discharge_per_day` |
| $`P^{\text{aux}}`$ | Parasitic auxiliary load | MW | `battery.aux_load_mw` |
| $`L`$ | Idle energy loss per step, see (2.5) | MWh | derived |
| $`E^{\min}`$ | Lower energy bound | MWh | `battery.soc.min` $`\times\, E^{\text{nom}}`$ |
| $`E^{\max}`$ | Upper energy bound | MWh | `battery.soc.max` $`\times\, E^{\text{nom}}`$ |
| $`E_{\text{init}}`$ | Energy at window start | MWh | carried between windows |
| $`E^{\text{term}}`$ | Required energy at window end | MWh | `dispatch.terminal_soc_fraction` $`\times\, E^{\text{nom}}`$ |
| $`m`$ | Time steps per day-ahead block | – | `day_ahead.product_resolution_minutes` |
| $`H`$ | Optimization horizon | h | `dispatch.horizon_hours` |
| $`C`$ | Committed step of the rolling horizon | h | `dispatch.commit_step_hours` |

## Decision variables

| Variable | Domain | Unit | Description |
|:---|:---|:---|:---|
| $`P^{c}_{t}`$ | $`[0, \bar{P}]`$ | MW | Charging power in step $`t`$ |
| $`P^{d}_{t}`$ | $`[0, \bar{P}]`$ | MW | Discharging power in step $`t`$ |
| $`u_{t}`$ | $`\{0,1\}`$ | – | Mode indicator, $`1`$ = charging permitted, $`0`$ = discharging permitted |
| $`E_{t}`$ | $`[E^{\min}, E^{\max}]`$ | MWh | Stored energy at the end of step $`t`$ |

## Objective function

The model maximizes net trading revenue over the window:

``` math
\max \; \sum_{t \in \mathcal{T}}
  \Big[ \big( \pi_t \kappa - f - g^{d} - c^{\deg} \big) P^{d}_{t} \Delta t
      - \big( \pi_t \kappa + f + g^{c} \big) P^{c}_{t} \Delta t \Big]
```

Equation (2.1) is the sum over all steps of revenue from selling less
the cost of buying. Its terms are:

- $`\pi_t \kappa`$ — the price actually captured. The haircut $`\kappa`$
  shaves the traded spread to represent the gap between the price used
  in the model and the price a real bid achieves.

- $`f`$, $`g^{c}`$, $`g^{d}`$ — per-MWh fees. Note the asymmetry of
  sign: fees subtract from the sale price and *add* to the purchase
  price, so they narrow the spread from both ends rather than acting as
  a levy on net position.

- $`c^{\deg}`$ — the marginal cost of degradation, charged per MWh
  discharged. This is not a cash cost and nobody invoices it. It is the
  shadow price of consuming battery life, and it is what makes the model
  degradation-aware rather than merely degradation-reporting: a cycle
  whose spread does not cover $`c^{\deg}`$ is declined. Its admissible
  range is bounded above by the cost of making good the capacity one
  cycle consumes,
  ``` math
  c^{\deg} \le \phi \cdot \kappa^{\text{aug}} \cdot 1000 ,
  ```
  where $`\phi`$ is `degradation.cyclic_fade_per_full_cycle` and
  $`\kappa^{\text{aug}}`$ is
  `degradation.augmentation.cost_eur_per_kwh`. Nominal energy cancels
  out of the ratio, because fade per cycle is a fraction of
  beginning-of-life capacity and one equivalent full cycle discharges
  exactly that. `config.py` enforces the bound. There is no
  corresponding lower bound: establishing one needs a discount factor
  keyed to the augmentation date, which depends on the cycle count,
  which is an outcome of dispatch rather than an input to it. Satisfying
  the bound therefore shows the value is not indefensibly high, not that
  it is right.

- $`\Delta t`$ — converts power in MW to energy in MWh over the step.

Because degradation is charged on discharge only, throughput is measured
as discharged energy throughout this document.

## Operational constraints

### B1 — Charge/discharge complementarity and power limits

A battery may not charge and discharge in the same step. The restriction
is enforced through the binary mode indicator, which simultaneously
imposes the power limit:

``` math
\begin{aligned}
P^{c}_{t} &\le \bar{P} \, u_{t} & \forall t \in \mathcal{T} \\
P^{d}_{t} &\le \bar{P} \, (1 - u_{t}) & \forall t \in \mathcal{T}
\end{aligned}
```

where $`\bar{P}`$ is the smaller of the battery power rating and the
grid connection limit, and $`u_t`$ selects the mode.

This binary is not a modelling nicety and must not be relaxed to obtain
a linear program. Under the relaxation, at sufficiently negative prices
the optimizer charges and discharges simultaneously: the two positions
cancel in the objective at zero net cost, while round-trip losses
destroy stored energy. Destroying energy for free is valuable when
prices are negative, because it lets a full battery keep buying. The
behaviour is an artefact of the relaxation rather than a dispatch
strategy, and the binary is what forbids it.

### B2 — State-of-charge dynamics

Stored energy evolves under charging, discharging and idle losses:

``` math
E_{t} = E_{t-1}
      + \eta^{c} P^{c}_{t} \Delta t
      - \frac{P^{d}_{t} \Delta t}{\eta^{d}}
      - L
      \qquad \forall t \in \mathcal{T}
```

with $`E_{-1} = E_{\text{init}}`$, and the idle loss per step

``` math
L = \left( \frac{\sigma E^{\text{nom}}}{24} + P^{\text{aux}} \right) \Delta t .
```

where $`\eta^{c}`$ *multiplies* the charging term and $`\eta^{d}`$
*divides* the discharging term. The asymmetry is deliberate and is the
correct treatment of one-way efficiencies: of the energy drawn from the
grid only a fraction $`\eta^{c}`$ is stored, whereas delivering
$`P^{d}_{t}`$ to the grid requires drawing $`P^{d}_{t} / \eta^{d}`$ from
storage. The product $`\eta^{c} \eta^{d}`$ is the round-trip efficiency.
The loss term $`L`$ is independent of dispatch and applies in every
step, including idle ones.

### B3 — Energy bounds

``` math
E^{\min} \le E_{t} \le E^{\max} \qquad \forall t \in \mathcal{T}
```

where the bounds are the scenario’s state-of-charge fractions scaled by
nominal capacity. In the implementation these are variable bounds rather
than explicit rows.

### B4 — Product block coherence

Day-ahead energy settles in hourly blocks, so dispatch may not vary
within a block even when the model runs at a finer resolution:

``` math
\begin{aligned}
P^{c}_{t} &= P^{c}_{t-1} & \forall t : t \bmod m \ne 0 \\
P^{d}_{t} &= P^{d}_{t-1} & \forall t : t \bmod m \ne 0
\end{aligned}
```

where $`m`$ is the number of model steps in one product block. With a
15-minute resolution and a 60-minute day-ahead product, $`m = 4`$. This
is also the machinery the four-hour reserve blocks of Section 3 will
reuse, which is why it is expressed generally rather than hard-coded to
the hourly case.

### B5 — Terminal state of charge

``` math
E_{N-1} \ge E^{\text{term}}
```

where $`E^{\text{term}}`$ is the required closing energy. The constraint
exists because stored energy has no value beyond the horizon: without it
the optimizer empties the battery into the final step of every window,
since selling is free revenue and the consequence falls outside the
model. It is applied at the far end of the window, not at the end of the
committed region, so that it shapes end effects rather than the
decisions actually taken.

## Rolling-horizon procedure

The window model above is solved repeatedly across the price series.
With $`h = H / \Delta t`$ steps of horizon and $`c = C / \Delta t`$
steps of commitment:

1.  Set $`E_{\text{init}}`$ to the opening state of charge.

2.  Solve (2.1)–(2.9) over the next $`h`$ steps.

3.  Retain the first $`c`$ steps of the solution as committed dispatch.

4.  Set $`E_{\text{init}}`$ to $`E_{c-1}`$ from the solved window.

5.  Advance by $`c`$ steps and repeat until the series is exhausted.

The horizon exceeds the commitment ($`h > c`$) so that the uncommitted
tail absorbs the distortion of B5. A final window shorter than one
commitment step receives no terminal target, since there is no tail left
to absorb it.

Setting `dispatch.mode` to `perfect_foresight` collapses the procedure
to a single window spanning the entire series. This reports an upper
bound on achievable revenue rather than an expectation, and the same is
true of the rolling mode while `dispatch.forecast.kind` remains
`perfect`: each window sees its own prices exactly.

## Post-optimization network charges

Network charges that fall outside the exemption of §118(6) EnWG are
applied in `./src/bess_stack/finance/grid_fees.py` to an already-solved
dispatch, rather than by re-solving each project year under that year’s
tariff. For a year $`y`$ the charge is

``` math
\Phi_y = \big( \gamma_y - \gamma^{\text{priced}} \big) \sum_{t} P^{c}_{t} \Delta t
       + \mathbb{1}[\,y \text{ not exempt}\,] \cdot \rho \cdot \bar{P}^{\text{conn}} \cdot 1000
```

where $`\gamma_y`$ is the per-MWh charging fee applicable in year $`y`$,
$`\gamma^{\text{priced}}`$ is the fee the optimizer already carried in
(2.1) via $`g^{c}`$, $`\rho`$ is the successor capacity charge in
EUR/kW/year and $`\bar{P}^{\text{conn}}`$ is the connection capacity in
MW. Subtracting the fee already priced is what prevents a charge from
being billed twice.

This is an approximation and its error is measured rather than assumed.
Because the retained dispatch was optimized without the fee, it remains
feasible but no longer optimal once the fee exists, so *revenue is
understated*; and because the marginal cycles the fee should have
suppressed are still present, *throughput and cycle count are
overstated*. Measured against a re-solved run on a full synthetic year,
a 5 EUR/MWh charging fee understates revenue by 1.0% while overstating
throughput by 13.3%; at 25 EUR/MWh the figures are 50.7% and 101.1%. The
asymmetry matters, because the augmentation trigger keys off cycle count
and therefore fires early long before the revenue figure looks wrong.
The error is exactly zero while the exemption holds.

# Reserve Products (Specified, Not Implemented)

**Nothing in this section is implemented.** The parameters below are
present in `scenarios/reference.yaml`, but `config.py` defines no FCR or
aFRR dataclass and does not read them, and `solve_window` builds no
reserve variables. What follows is the formulation these parameters
imply, recorded so the schema and the intended model can be checked
against each other before either is built.

## Additional sets and parameters

Let $`k \in \mathcal{K}`$ index four-hour reserve blocks, with
$`\mathcal{T}_k`$ the model steps in block $`k`$ and $`H^{\text{blk}}`$
the block length in hours.

| Symbol | Description | Source field |
|:---|:---|:---|
| $`c^{\text{FCR}}`$ | FCR capacity price (EUR/MW/h) | `fcr.capacity_price_eur_per_mw_h` |
| $`h^{\text{FCR}}`$ | Energy headroom held per MW awarded (h) | `fcr.reserve_energy_hours` |
| $`R^{\min}`$ | Minimum bid size (MW) | `fcr.min_bid_mw`, `afrr.min_bid_mw` |
| $`c^{a\pm}`$ | aFRR capacity price, each direction | `afrr.{positive,negative}.capacity_price_eur_per_mw_h` |
| $`\pi^{a\pm}`$ | aFRR energy price, each direction | `afrr.{positive,negative}.energy_price_eur_per_mwh` |
| $`\alpha^{\pm}`$ | Expected activation rate | `afrr.{positive,negative}.activation_rate` |
| $`h^{a}`$ | aFRR energy headroom per MW awarded (h) | `afrr.reserve_energy_hours` |

## Additional variables

$`R^{\text{FCR}}_{k} \ge 0`$ is the symmetric FCR award in block $`k`$;
$`R^{a+}_{k}, R^{a-}_{k} \ge 0`$ are the directional aFRR awards. Binary
indicators $`y^{\bullet}_{k} \in \{0,1\}`$ carry the minimum bid size.

## Revenue terms

The objective (2.1) gains capacity revenue and expected activation
revenue:

``` math
+ \sum_{k \in \mathcal{K}} H^{\text{blk}}
  \Big[ c^{\text{FCR}} R^{\text{FCR}}_{k}
      + c^{a+} R^{a+}_{k} + c^{a-} R^{a-}_{k}
      + \alpha^{+} \pi^{a+} R^{a+}_{k}
      - \alpha^{-} \pi^{a-} R^{a-}_{k} \Big]
```

where the sign on the negative-direction energy term reflects that
$`\pi^{a-}`$ is quoted as payment *to* the provider for absorbing
energy, so a negative price is revenue. This convention must be pinned
down against settlement rules before implementation; it is stated here
precisely because it is the kind of sign that is easy to get backwards
and hard to detect afterwards.

## R1 — Minimum bid size

``` math
R^{\min} y^{\bullet}_{k} \le R^{\bullet}_{k} \le \bar{P} \, y^{\bullet}_{k}
\qquad \forall k \in \mathcal{K}
```

where $`\bullet`$ ranges over the three products. An award is either
zero or at least $`R^{\min}`$, which is a semi-continuous variable and
the reason reserve participation introduces binaries of its own.

## R2 — Power headroom

Capacity sold must be deliverable on top of energy dispatch:

``` math
\begin{aligned}
P^{d}_{t} + R^{\text{FCR}}_{k} + R^{a+}_{k} &\le \bar{P}
  & \forall k, \; \forall t \in \mathcal{T}_k \\
P^{c}_{t} + R^{\text{FCR}}_{k} + R^{a-}_{k} &\le \bar{P}
  & \forall k, \; \forall t \in \mathcal{T}_k
\end{aligned}
```

## R3 — Energy headroom

Capacity must also be sustainable for the prequalification duration,
which is what $`h^{\text{FCR}}`$ and $`h^{a}`$ encode:

``` math
\begin{aligned}
E_{t} - \big( h^{\text{FCR}} R^{\text{FCR}}_{k} + h^{a} R^{a+}_{k} \big)
  &\ge E^{\min} & \forall k, \; \forall t \in \mathcal{T}_k \\
E_{t} + \big( h^{\text{FCR}} R^{\text{FCR}}_{k} + h^{a} R^{a-}_{k} \big)
  &\le E^{\max} & \forall k, \; \forall t \in \mathcal{T}_k
\end{aligned}
```

where $`h^{\text{FCR}} = 0.5`$ reflects the German requirement that full
FCR activation be sustainable for 30 minutes in both directions.
Understating it permits the same MW to be sold to reserve and to
arbitrage at once.

## Known gap: activation does not move the state of charge

In the formulation above, aFRR energy enters revenue through the
expected activation rate $`\alpha^{\pm}`$ but does not appear in the
state-of-charge dynamics (2.4). Real activation moves energy and
therefore constrains subsequent dispatch. Closing that gap requires
either activation as a deterministic energy flow inside B2, or a
stochastic formulation over activation scenarios. Until it is closed,
reserve participation will be overvalued, because the model collects
activation revenue without paying its state-of-charge consequence.
