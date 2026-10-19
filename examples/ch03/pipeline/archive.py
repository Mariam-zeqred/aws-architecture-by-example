"""Read the chapter 2 archive: gzip JSON-lines under ``raw/dt=YYYY-MM-DD/``.

Each line is one reading as the chapter 2 rule wrote it:
``{"temp": 22.4, "hum": 55, "ts": 1792108800, "device": "field-1", "received": ...}``.
Readings are streamed, never held in memory as a list: a 1 000-device
fleet writes 1.4 million lines a day and a 90-day training window is 130
million lines, which is why the chapter measures lines per second.
"""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import Iterator
from datetime import date, timedelta

from storage import Store

RAW_PREFIX = "raw/dt="


class Reading(tuple):
    """``(device, ts, temp, hum)``; a tuple subclass so a stream of them stays small."""

    __slots__ = ()

    @property
    def device(self) -> str:
        return self[0]

    @property
    def ts(self) -> int:
        return self[1]

    @property
    def temp(self) -> float:
        return self[2]

    @property
    def hum(self) -> float:
        return self[3]


def day_range(end: date, days: int) -> list[date]:
    """The ``days`` dates ending with ``end`` inclusive, oldest first."""
    return [end - timedelta(days=i) for i in range(days - 1, -1, -1)]


def iter_lines(store: Store, days: list[date]) -> Iterator[str]:
    """Every line of every object under the given days' prefixes."""
    for day in days:
        for key in store.list(f"{RAW_PREFIX}{day.isoformat()}/"):
            data = store.get(key)
            if key.endswith(".gz"):
                data = gzip.decompress(data)
            for line in io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"):
                if line.strip():
                    yield line


def parse_line(line: str) -> Reading | None:
    """One archive line to a Reading, or None for anything malformed.

    The archive is append-only and raw: a line with a missing field or a
    non-numeric value is skipped here, not fixed, and counted by the caller.
    """
    try:
        obj = json.loads(line)
        return Reading((str(obj["device"]), int(obj["ts"]), float(obj["temp"]), float(obj["hum"])))
    except (ValueError, KeyError, TypeError):
        return None


def iter_readings(store: Store, days: list[date], stats: dict[str, int] | None = None):
    """Stream Readings for the given days; ``stats`` collects line counts."""
    stats = stats if stats is not None else {}
    stats.setdefault("lines", 0)
    stats.setdefault("skipped", 0)
    for line in iter_lines(store, days):
        stats["lines"] += 1
        reading = parse_line(line)
        if reading is None:
            stats["skipped"] += 1
            continue
        yield reading
