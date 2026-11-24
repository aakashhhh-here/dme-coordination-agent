"""Mocked geocoding: ZIP centroid table + haversine. No network, no LLM."""

import json
from functools import cache
from math import asin, cos, radians, sin, sqrt

from dme import config

Point = tuple[float, float]


@cache
def _table() -> dict[str, Point]:
    raw = json.loads((config.DATA_DIR / "zip_centroids.json").read_text())
    return {k: tuple(v) for k, v in raw.items() if not k.startswith("_")}


def locate(zip_code: str) -> Point | None:
    return _table().get(zip_code[:5])


def miles_between(zip_a: str, zip_b: str) -> float | None:
    a, b = locate(zip_a), locate(zip_b)
    if a is None or b is None:
        return None
    lat1, lon1, lat2, lon2 = map(radians, (*a, *b))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 3958.8 * asin(sqrt(h))
