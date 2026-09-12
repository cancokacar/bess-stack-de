"""Day-ahead price series.

Only the `synthetic` source is implemented. It exists so the dispatch model can
be exercised and tested without a data licence or an API key, and it is not a
forecast: the shapes below are stylised German day-ahead behaviour, not a fit to
any published series. Revenue computed on synthetic prices carries no claim
about the real market.
"""

from __future__ import annotations

import numpy as np

from ..config import Scenario

HOURS_PER_YEAR = 8760


def synthetic_day_ahead_hourly(year: int, seed: int = 42) -> np.ndarray:
    """Stylised hourly German day-ahead prices in EUR/MWh for one year.

    Superposes a seasonal level, a weekday/weekend offset, a twin-peaked diurnal
    shape with a midday solar depression that deepens in summer, and AR(1) noise.
    Negative prices arise from the summer solar depression rather than being
    injected, which is roughly how they arise in the real zone.
    """
    rng = np.random.default_rng(seed)
    hours = np.arange(HOURS_PER_YEAR)
    hour_of_day = hours % 24
    day_of_year = hours // 24
    day_of_week = day_of_year % 7

    # Winter-heavy seasonal level.
    seasonal = 80.0 + 25.0 * np.cos(2 * np.pi * (day_of_year - 15) / 365.0)
    weekend = np.where(day_of_week >= 5, -12.0, 0.0)

    # Solar depression is deepest around the summer solstice (day 172).
    solar_strength = 1.0 + 0.8 * np.cos(2 * np.pi * (day_of_year - 172) / 365.0)
    morning = 22.0 * np.exp(-0.5 * ((hour_of_day - 8.0) / 2.0) ** 2)
    evening = 30.0 * np.exp(-0.5 * ((hour_of_day - 19.0) / 2.2) ** 2)
    midday = 34.0 * solar_strength * np.exp(-0.5 * ((hour_of_day - 13.0) / 2.6) ** 2)
    diurnal = morning + evening - midday

    # AR(1) noise, so prices are serially correlated rather than white.
    phi, sigma = 0.80, 15.0
    shocks = rng.normal(0.0, sigma, HOURS_PER_YEAR)
    noise = np.empty(HOURS_PER_YEAR)
    noise[0] = shocks[0]
    for t in range(1, HOURS_PER_YEAR):
        noise[t] = phi * noise[t - 1] + np.sqrt(1 - phi**2) * shocks[t]

    return np.clip(seasonal + weekend + diurnal + noise, -150.0, 900.0)


def upsample(hourly: np.ndarray, steps_per_hour: int) -> np.ndarray:
    """Repeat each hourly price across the sub-hourly steps inside that hour.

    Day-ahead settles hourly, so the price is constant within the hour even when
    dispatch is modelled at a finer resolution.
    """
    return np.repeat(hourly, steps_per_hour)


def day_ahead_prices(scenario: Scenario, hours: int | None = None) -> np.ndarray:
    """Day-ahead prices at the scenario's market resolution."""
    source = scenario.market.price_source
    if source == "smard":
        from .smard import day_ahead_prices as smard_day_ahead_prices

        return smard_day_ahead_prices(scenario, hours=hours)
    if source != "synthetic":
        raise NotImplementedError(
            f"market.price_source '{source}' is declared in the schema but not implemented; "
            "only 'synthetic' and 'smard' are available"
        )
    hourly = synthetic_day_ahead_hourly(scenario.market.year)
    if hours is not None:
        hourly = hourly[:hours]
    return upsample(hourly, scenario.market.steps_per_hour)
