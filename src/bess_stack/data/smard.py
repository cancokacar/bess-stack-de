"""Day-ahead prices from SMARD, the Bundesnetzagentur market data platform.

Licensed CC BY 4.0 and attributed to ``Bundesnetzagentur | SMARD.de``. The
README's "Data provenance" section carries the licence wording and the reason
the reserve markets are not reachable the same way.

The API serves one JSON file per week, so a calendar year is roughly 52 fetches.
Parsing is deliberately separated from fetching: the cases worth testing are all
in the parser, and they can then be tested against committed fixtures with no
network.

Nothing here interpolates. A gap in the published series raises, because a model
that invents a price to fill a hole reports revenue that was never available.
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import certifi
import numpy as np

from ..config import Scenario

BASE_URL = "https://www.smard.de/app/chart_data"
FILTER_DAY_AHEAD = 4169
REGION_DE_LU = "DE-LU"
RESOLUTION = "quarterhour"
STEP_MS = 15 * 60 * 1000
BERLIN = ZoneInfo("Europe/Berlin")

DEFAULT_CACHE_DIR = Path("data/raw/smard")

# macOS Python installs frequently ship without a usable CA bundle, which turns
# every fetch into an opaque CERTIFICATE_VERIFY_FAILED. certifi removes that
# variable rather than asking each user to repair their interpreter.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class SmardError(RuntimeError):
    """The published series could not be used as it stands."""


def index_url() -> str:
    """URL of the list of available weekly start timestamps."""
    return f"{BASE_URL}/{FILTER_DAY_AHEAD}/{REGION_DE_LU}/index_{RESOLUTION}.json"


def series_url(week_start_ms: int) -> str:
    """URL of one week of quarter-hourly prices."""
    stem = f"{FILTER_DAY_AHEAD}_{REGION_DE_LU}_{RESOLUTION}_{week_start_ms}"
    return f"{BASE_URL}/{FILTER_DAY_AHEAD}/{REGION_DE_LU}/{stem}.json"


def parse_week(payload: dict) -> tuple[np.ndarray, np.ndarray]:
    """Split one weekly payload into epoch-millisecond stamps and prices.

    Null prices are preserved as NaN here rather than dropped, so that the
    decision about what to do with a gap belongs to the caller and is made once.
    """
    series = payload.get("series")
    if series is None:
        raise SmardError("payload has no 'series' key; this is not a SMARD chart file")
    if not series:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=float)
    stamps = np.array([point[0] for point in series], dtype=np.int64)
    prices = np.array(
        [np.nan if point[1] is None else float(point[1]) for point in series], dtype=float
    )
    return stamps, prices


def _fetch_json(url: str, timeout: float = 60.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout, context=_SSL_CONTEXT) as response:
        return json.load(response)


def fetch_week(week_start_ms: int, cache_dir: Path | None = None) -> dict:
    """One weekly payload, from the on-disk cache when it is already there.

    The cache lives under ``data/raw/`` which .gitignore already blocks, so a
    populated cache cannot be committed by accident.
    """
    directory = DEFAULT_CACHE_DIR if cache_dir is None else Path(cache_dir)
    path = directory / f"{FILTER_DAY_AHEAD}_{REGION_DE_LU}_{RESOLUTION}_{week_start_ms}.json"
    if path.exists():
        return json.loads(path.read_text())
    payload = _fetch_json(series_url(week_start_ms))
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return payload


def fetch_index(cache_dir: Path | None = None) -> list[int]:
    """Weekly start timestamps the platform currently offers."""
    directory = DEFAULT_CACHE_DIR if cache_dir is None else Path(cache_dir)
    path = directory / f"index_{RESOLUTION}.json"
    if path.exists():
        payload = json.loads(path.read_text())
    else:
        payload = _fetch_json(index_url())
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
    return sorted(int(t) for t in payload["timestamps"])


def year_bounds_ms(year: int) -> tuple[int, int]:
    """Half-open [start, end) of a calendar year in Berlin local time.

    Local rather than UTC, because a German delivery year begins at local
    midnight and the DST offset either side of it is not the same.
    """
    start = datetime(year, 1, 1, tzinfo=BERLIN)
    end = datetime(year + 1, 1, 1, tzinfo=BERLIN)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def stitch(pieces: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    """Concatenate weekly pieces into one series, ordered and de-duplicated.

    Weeks can overlap at their seams, so the same timestamp may arrive twice.
    """
    if not pieces:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=float)
    stamps = np.concatenate([s for s, _ in pieces])
    prices = np.concatenate([p for _, p in pieces])
    order = np.argsort(stamps, kind="stable")
    stamps, prices = stamps[order], prices[order]
    keep = np.ones(len(stamps), dtype=bool)
    keep[1:] = stamps[1:] != stamps[:-1]
    return stamps[keep], prices[keep]


def check_contiguous(stamps: np.ndarray) -> None:
    """Every step must be exactly 15 minutes apart in epoch time.

    This is the check that makes daylight saving a non-event: local clocks jump,
    epoch time does not, so a correctly stitched series has a constant step
    across both transitions and the day simply contains 92 or 100 steps instead
    of 96. A break here means a missing interval or a mis-stitched seam, not DST.
    """
    if len(stamps) < 2:
        return
    gaps = np.diff(stamps)
    bad = np.flatnonzero(gaps != STEP_MS)
    if bad.size:
        first = int(stamps[bad[0]])
        when = datetime.fromtimestamp(first / 1000, BERLIN)
        raise SmardError(
            f"series is not contiguous at 15-minute steps: {bad.size} break(s), the first "
            f"after {when.isoformat()} with a gap of {int(gaps[bad[0]]) / 60000:g} minutes"
        )


def load_quarter_hourly(
    start_ms: int, end_ms: int, cache_dir: Path | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Stitched prices over the half-open range [start_ms, end_ms)."""
    available = fetch_index(cache_dir)
    if not available:
        raise SmardError("the SMARD index listed no weeks")
    # A week whose start precedes the range can still carry part of it, so step
    # back one week from the first start at or after `start_ms`.
    starts = [t for t in available if start_ms - 7 * 24 * 3600 * 1000 <= t < end_ms]
    if not starts:
        raise SmardError(
            f"no published week overlaps [{start_ms}, {end_ms}); the platform's first week "
            f"begins at {available[0]}"
        )
    pieces = [parse_week(fetch_week(t, cache_dir)) for t in starts]
    stamps, prices = stitch(pieces)
    inside = (stamps >= start_ms) & (stamps < end_ms)
    stamps, prices = stamps[inside], prices[inside]

    missing = np.flatnonzero(np.isnan(prices))
    if missing.size:
        when = datetime.fromtimestamp(int(stamps[missing[0]]) / 1000, BERLIN)
        raise SmardError(
            f"{missing.size} of {len(prices)} published prices are null, the first at "
            f"{when.isoformat()}. Nothing here interpolates: decide what the gap means "
            "before using the series"
        )
    check_contiguous(stamps)
    return stamps, prices


def day_ahead_prices(scenario: Scenario, hours: int | None = None) -> np.ndarray:
    """Day-ahead prices for the scenario's market year, at market resolution."""
    if scenario.market.resolution_minutes != 15:
        raise SmardError(
            f"SMARD serves this series at quarter-hourly resolution; the scenario asks for "
            f"{scenario.market.resolution_minutes} minutes"
        )
    start_ms, end_ms = year_bounds_ms(scenario.market.year)
    if hours is not None:
        end_ms = min(end_ms, start_ms + int(hours * 3600 * 1000))
    _, prices = load_quarter_hourly(start_ms, end_ms)
    return prices


def expected_steps(year: int) -> int:
    """Quarter-hourly steps in a Berlin calendar year, DST and leap day included."""
    start, end = year_bounds_ms(year)
    return (end - start) // STEP_MS
