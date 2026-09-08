"""Scenario configuration: load `scenarios/*.yaml` into typed objects.

Validation lives here so that the invariants the scenario file states in
comments are actually enforced. A scenario that breaks one fails at load time
rather than quietly producing a plausible-looking number.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ScenarioError(ValueError):
    """A scenario file is internally inconsistent."""


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ScenarioError(f"missing required field '{where}.{key}'")
    return mapping[key]


@dataclass(frozen=True)
class Battery:
    power_mw: float
    energy_mwh: float
    charge_efficiency: float
    discharge_efficiency: float
    self_discharge_per_day: float
    soc_min: float
    soc_max: float
    soc_initial: float
    aux_load_mw: float

    @property
    def round_trip_efficiency(self) -> float:
        return self.charge_efficiency * self.discharge_efficiency

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Battery:
        eff = _require(d, "efficiency", "battery")
        soc = _require(d, "soc", "battery")
        return cls(
            power_mw=float(_require(d, "power_mw", "battery")),
            energy_mwh=float(_require(d, "energy_mwh", "battery")),
            charge_efficiency=float(_require(eff, "charge", "battery.efficiency")),
            discharge_efficiency=float(_require(eff, "discharge", "battery.efficiency")),
            self_discharge_per_day=float(eff.get("self_discharge_per_day", 0.0)),
            soc_min=float(_require(soc, "min", "battery.soc")),
            soc_max=float(_require(soc, "max", "battery.soc")),
            soc_initial=float(_require(soc, "initial", "battery.soc")),
            aux_load_mw=float(d.get("aux_load_mw", 0.0)),
        )

    def validate(self) -> None:
        if self.power_mw <= 0 or self.energy_mwh <= 0:
            raise ScenarioError("battery.power_mw and battery.energy_mwh must be positive")
        for name, value in (
            ("charge", self.charge_efficiency),
            ("discharge", self.discharge_efficiency),
        ):
            if not 0.0 < value <= 1.0:
                raise ScenarioError(f"battery.efficiency.{name} must be in (0, 1], got {value}")
        if not 0.0 <= self.soc_min < self.soc_max <= 1.0:
            raise ScenarioError(
                f"require 0 <= soc.min < soc.max <= 1, got {self.soc_min} and {self.soc_max}"
            )
        if not self.soc_min <= self.soc_initial <= self.soc_max:
            raise ScenarioError(
                f"soc.initial {self.soc_initial} lies outside [{self.soc_min}, {self.soc_max}]"
            )


@dataclass(frozen=True)
class Augmentation:
    enabled: bool
    trigger_capacity_fraction: float
    restore_to_capacity_fraction: float
    max_events: int
    cost_eur_per_kwh: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Augmentation:
        return cls(
            enabled=bool(d.get("enabled", False)),
            trigger_capacity_fraction=float(d.get("trigger_capacity_fraction", 0.0)),
            restore_to_capacity_fraction=float(d.get("restore_to_capacity_fraction", 1.0)),
            max_events=int(d.get("max_events", 0)),
            cost_eur_per_kwh=float(d.get("cost_eur_per_kwh", 0.0)),
        )


@dataclass(frozen=True)
class Degradation:
    model: str
    marginal_cost_eur_per_mwh: float
    calendar_fade_per_year: float
    cyclic_fade_per_full_cycle: float
    end_of_life_capacity_fraction: float
    equivalent_full_cycles_to_eol: int
    augmentation: Augmentation

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Degradation:
        return cls(
            model=str(_require(d, "model", "degradation")),
            marginal_cost_eur_per_mwh=float(
                _require(d, "marginal_cost_eur_per_mwh", "degradation")
            ),
            calendar_fade_per_year=float(_require(d, "calendar_fade_per_year", "degradation")),
            cyclic_fade_per_full_cycle=float(
                _require(d, "cyclic_fade_per_full_cycle", "degradation")
            ),
            end_of_life_capacity_fraction=float(
                _require(d, "end_of_life_capacity_fraction", "degradation")
            ),
            equivalent_full_cycles_to_eol=int(
                _require(d, "equivalent_full_cycles_to_eol", "degradation")
            ),
            augmentation=Augmentation.from_dict(d.get("augmentation", {})),
        )

    def validate(self, project_lifetime_years: int) -> None:
        if self.model != "throughput":
            raise ScenarioError(
                f"degradation.model '{self.model}' is not implemented; v1 commits to 'throughput' "
                "because rainflow counting is path dependent and not MILP representable"
            )
        if self.marginal_cost_eur_per_mwh < 0:
            raise ScenarioError("degradation.marginal_cost_eur_per_mwh must not be negative")

        # The identity documented next to these fields in the scenario file.
        implied = (
            1.0 - self.end_of_life_capacity_fraction
        ) / self.cyclic_fade_per_full_cycle
        if abs(implied - self.equivalent_full_cycles_to_eol) > 1.0:
            raise ScenarioError(
                "degradation is internally inconsistent: "
                f"(1 - {self.end_of_life_capacity_fraction}) / "
                f"{self.cyclic_fade_per_full_cycle} = {implied:.0f} equivalent full cycles, "
                f"but equivalent_full_cycles_to_eol is {self.equivalent_full_cycles_to_eol}"
            )

        # A necessary condition only. Cyclic fade adds to calendar fade, but the
        # cycle count is an outcome of dispatch rather than an input to it, so a
        # scenario that passes here can still reach end of life inside the
        # horizon once it is actually run. Passing is not a clean bill of health.
        remaining = (1.0 - self.calendar_fade_per_year) ** project_lifetime_years
        if remaining < self.end_of_life_capacity_fraction and not self.augmentation.enabled:
            raise ScenarioError(
                f"calendar fade alone leaves {remaining:.3f} of beginning-of-life capacity at "
                f"year {project_lifetime_years}, below the end-of-life threshold "
                f"{self.end_of_life_capacity_fraction}, and augmentation is disabled. Either "
                "enable augmentation or shorten finance.project_lifetime_years"
            )


@dataclass(frozen=True)
class Market:
    country: str
    bidding_zone: str
    year: int
    timezone: str
    resolution_minutes: int
    price_source: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Market:
        return cls(
            country=str(_require(d, "country", "market")),
            bidding_zone=str(_require(d, "bidding_zone", "market")),
            year=int(_require(d, "year", "market")),
            timezone=str(d.get("timezone", "Europe/Berlin")),
            resolution_minutes=int(_require(d, "resolution_minutes", "market")),
            price_source=str(d.get("price_source", "synthetic")),
        )

    @property
    def steps_per_hour(self) -> int:
        return 60 // self.resolution_minutes

    def validate(self) -> None:
        if 60 % self.resolution_minutes != 0:
            raise ScenarioError(
                f"market.resolution_minutes {self.resolution_minutes} must divide 60"
            )
        if self.price_source not in {"entsoe", "smard", "synthetic"}:
            raise ScenarioError(f"unknown market.price_source '{self.price_source}'")


@dataclass(frozen=True)
class Solver:
    name: str
    mip_gap: float
    time_limit_s: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Solver:
        return cls(
            name=str(d.get("name", "highs")),
            mip_gap=float(d.get("mip_gap", 0.005)),
            time_limit_s=float(d.get("time_limit_s", 60)),
        )


@dataclass(frozen=True)
class Dispatch:
    mode: str
    horizon_hours: int
    commit_step_hours: int
    terminal_soc_fraction: float
    forecast_kind: str
    solver: Solver

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Dispatch:
        forecast = d.get("forecast", {})
        return cls(
            mode=str(_require(d, "mode", "dispatch")),
            horizon_hours=int(_require(d, "horizon_hours", "dispatch")),
            commit_step_hours=int(_require(d, "commit_step_hours", "dispatch")),
            terminal_soc_fraction=float(_require(d, "terminal_soc_fraction", "dispatch")),
            forecast_kind=str(forecast.get("kind", "perfect")),
            solver=Solver.from_dict(d.get("solver", {})),
        )

    def validate(self, battery: Battery) -> None:
        if self.mode not in {"rolling_horizon", "perfect_foresight"}:
            raise ScenarioError(f"unknown dispatch.mode '{self.mode}'")
        if self.forecast_kind != "perfect":
            raise ScenarioError(
                f"dispatch.forecast.kind '{self.forecast_kind}' is not implemented; the "
                "forecast-error modes are declared in the schema but uncalibrated"
            )
        if self.commit_step_hours > self.horizon_hours:
            raise ScenarioError(
                f"dispatch.commit_step_hours {self.commit_step_hours} exceeds "
                f"horizon_hours {self.horizon_hours}"
            )
        if not battery.soc_min <= self.terminal_soc_fraction <= battery.soc_max:
            raise ScenarioError(
                f"dispatch.terminal_soc_fraction {self.terminal_soc_fraction} lies outside "
                f"[{battery.soc_min}, {battery.soc_max}]"
            )


@dataclass(frozen=True)
class DayAhead:
    enabled: bool
    product_resolution_minutes: int
    spread_haircut: float
    fees_eur_per_mwh: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DayAhead:
        return cls(
            enabled=bool(d.get("enabled", True)),
            product_resolution_minutes=int(d.get("product_resolution_minutes", 60)),
            spread_haircut=float(d.get("spread_haircut", 0.0)),
            fees_eur_per_mwh=float(d.get("fees_eur_per_mwh", 0.0)),
        )

    def validate(self, market: Market) -> None:
        if self.product_resolution_minutes % market.resolution_minutes != 0:
            raise ScenarioError(
                f"day_ahead.product_resolution_minutes {self.product_resolution_minutes} is not a "
                f"multiple of market.resolution_minutes {market.resolution_minutes}"
            )
        if not 0.0 <= self.spread_haircut < 1.0:
            raise ScenarioError("day_ahead.spread_haircut must be in [0, 1)")


@dataclass(frozen=True)
class Grid:
    connection_limit_mw: float
    grid_fee_exemption: bool
    charging_fees_eur_per_mwh: float
    discharging_fees_eur_per_mwh: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Grid:
        return cls(
            connection_limit_mw=float(_require(d, "connection_limit_mw", "grid")),
            grid_fee_exemption=bool(d.get("grid_fee_exemption", False)),
            charging_fees_eur_per_mwh=float(d.get("charging_fees_eur_per_mwh", 0.0)),
            discharging_fees_eur_per_mwh=float(d.get("discharging_fees_eur_per_mwh", 0.0)),
        )


@dataclass(frozen=True)
class Scenario:
    name: str
    battery: Battery
    degradation: Degradation
    market: Market
    dispatch: Dispatch
    day_ahead: DayAhead
    grid: Grid
    project_lifetime_years: int
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Scenario:
        finance = _require(d, "finance", "<root>")
        streams = _require(d, "revenue_streams", "<root>")
        scenario = cls(
            name=str(d.get("name", "unnamed")),
            battery=Battery.from_dict(_require(d, "battery", "<root>")),
            degradation=Degradation.from_dict(_require(d, "degradation", "<root>")),
            market=Market.from_dict(_require(d, "market", "<root>")),
            dispatch=Dispatch.from_dict(_require(d, "dispatch", "<root>")),
            day_ahead=DayAhead.from_dict(_require(streams, "day_ahead", "revenue_streams")),
            grid=Grid.from_dict(_require(d, "grid", "<root>")),
            project_lifetime_years=int(
                _require(finance, "project_lifetime_years", "finance")
            ),
            raw=d,
        )
        scenario.validate()
        return scenario

    @classmethod
    def from_yaml(cls, path: str | Path) -> Scenario:
        with open(path) as fh:
            return cls.from_dict(yaml.safe_load(fh))

    def validate(self) -> None:
        self.battery.validate()
        self.market.validate()
        self.degradation.validate(self.project_lifetime_years)
        self.dispatch.validate(self.battery)
        self.day_ahead.validate(self.market)
        if self.grid.connection_limit_mw <= 0:
            raise ScenarioError("grid.connection_limit_mw must be positive")
