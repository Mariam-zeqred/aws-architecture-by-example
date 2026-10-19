"""Write a SYNTHETIC chapter 2 archive so the pipeline can run with no fleet.

The data is made up. Each device has a fixed offset from a fleet-wide
daily curve: a sine with its peak at 15:00, a slow random walk for the
weather, an occasional hotter day, and sensor noise. Humidity moves against
temperature. Nothing here is measured from a real field; the README and the
chapter say so wherever a number from this data appears.

Usage: ``python synth_archive.py OUT_DIR --devices 10 --days 21 --end 2026-10-18``
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import random
from datetime import UTC, date, datetime, timedelta

from storage import Store

READING_INTERVAL_S = 60
OBJECTS_PER_DAY = 4  # Firehose would write ~288 (one per 5 min); fewer files keeps the demo small


def write_synthetic_archive(
    store: Store,
    *,
    devices: int,
    days: int,
    end: date,
    seed: int = 0,
    interval_s: int = READING_INTERVAL_S,
) -> dict[str, int]:
    """Write ``days`` days ending ``end`` for ``devices`` devices; return counts."""
    rng = random.Random(seed)
    offsets = [rng.uniform(-1.5, 1.5) for _ in range(devices)]
    weather = 0.0
    lines = 0
    per_day = 86_400 // interval_s
    per_object = per_day // OBJECTS_PER_DAY
    for d in range(days):
        day = end - timedelta(days=days - 1 - d)
        weather = max(-4.0, min(4.0, weather + rng.gauss(0, 0.8)))
        hot_day = 3.0 if rng.random() < 0.15 else 0.0  # about one day in seven is a heat event
        midnight = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
        for obj in range(OBJECTS_PER_DAY):
            rows = []
            for i in range(obj * per_object, (obj + 1) * per_object):
                ts = midnight + i * interval_s
                hour = (i * interval_s) / 3600
                fleet = 22.0 + weather + (6.0 + hot_day) * math.sin(2 * math.pi * (hour - 9) / 24)
                for n in range(devices):
                    temp = fleet + offsets[n] + rng.gauss(0, 0.3)
                    hum = max(20.0, min(95.0, 70.0 - 1.5 * (temp - 22.0) + rng.gauss(0, 2.0)))
                    rows.append(
                        json.dumps(
                            {
                                "temp": round(temp, 2),
                                "hum": round(hum, 1),
                                "ts": ts,
                                "device": f"field-{n + 1}",
                                "received": ts * 1000 + rng.randint(50, 900),
                            },
                            separators=(",", ":"),
                        )
                    )
            lines += len(rows)
            key = f"raw/dt={day.isoformat()}/part-{obj:03d}.gz"
            store.put(key, gzip.compress(("\n".join(rows) + "\n").encode()))
    return {"devices": devices, "days": days, "lines": lines}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out", help="directory or s3://bucket/prefix")
    parser.add_argument("--devices", type=int, default=10)
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 18))
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    counts = write_synthetic_archive(
        Store(args.out), devices=args.devices, days=args.days, end=args.end, seed=args.seed
    )
    print(json.dumps({"synthetic": True, **counts}))


if __name__ == "__main__":
    main()
