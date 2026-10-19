"""Readings -> hourly rows -> supervised examples.

The model answers one question per device: *what will the highest hourly
temperature be over the next 24 hours?* Its inputs are the last 24 hourly
means and the hour of day. Everything is built from the archive with
plain dictionaries first, so a 90-day window of a 1 000-device fleet never
needs more memory than its hourly table (about 2 million small tuples).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from archive import Reading

LAGS = 24  # hourly means fed to the model
HORIZON = 24  # hours ahead the target looks: the coming day
N_FEATURES = LAGS + 2  # lags + sin/cos of the hour of day


@dataclass
class Hour:
    total: float = 0.0
    count: int = 0
    low: float = math.inf
    high: float = -math.inf

    def add(self, temp: float) -> None:
        self.total += temp
        self.count += 1
        self.low = min(self.low, temp)
        self.high = max(self.high, temp)

    @property
    def mean(self) -> float:
        return self.total / self.count


HourlyTable = dict[str, dict[int, Hour]]  # device -> hour index (ts // 3600) -> Hour


def hourly_table(readings: Iterable[Reading]) -> HourlyTable:
    """Aggregate a stream of readings into mean/min/max per device-hour."""
    table: HourlyTable = {}
    for r in readings:
        hours = table.setdefault(r.device, {})
        hour = hours.get(r.ts // 3600)
        if hour is None:
            hour = hours[r.ts // 3600] = Hour()
        hour.add(r.temp)
    return table


def _features(hours: dict[int, Hour], h: int) -> list[float] | None:
    """The feature vector for the example whose "now" is the end of hour ``h``."""
    lags = []
    for k in range(LAGS):
        hour = hours.get(h - k)
        if hour is None:
            return None  # a gap in the last 24 hours: no example, rather than a guess
        lags.append(hour.mean)
    angle = 2 * math.pi * ((h + 1) % 24) / 24  # the hour the forecast starts
    return [*lags, math.sin(angle), math.cos(angle)]


def _target(hours: dict[int, Hour], h: int) -> float | None:
    highs = []
    for k in range(1, HORIZON + 1):
        hour = hours.get(h + k)
        if hour is None:
            return None
        highs.append(hour.high)
    return max(highs)


@dataclass
class Examples:
    X: np.ndarray  # (n, N_FEATURES) float32
    y: np.ndarray  # (n,) float32; NaN where there is no target yet
    devices: list[str]
    hours: np.ndarray  # (n,) int64, the "now" hour index of each row

    def __len__(self) -> int:
        return len(self.devices)


def build_examples(table: HourlyTable, *, with_target: bool = True) -> Examples:
    """Every complete (device, hour) window; with the target where it exists."""
    X, y, devices, hours = [], [], [], []
    for device, device_hours in table.items():
        for h in sorted(device_hours):
            feats = _features(device_hours, h)
            if feats is None:
                continue
            target = _target(device_hours, h)
            if with_target and target is None:
                continue
            X.append(feats)
            y.append(math.nan if target is None else target)
            devices.append(device)
            hours.append(h)
    return Examples(
        X=np.asarray(X, dtype=np.float32).reshape(-1, N_FEATURES),
        y=np.asarray(y, dtype=np.float32),
        devices=devices,
        hours=np.asarray(hours, dtype=np.int64),
    )


def latest_examples(table: HourlyTable) -> Examples:
    """One row per device: the most recent hour with a full 24-hour history."""
    X, devices, hours = [], [], []
    for device, device_hours in table.items():
        for h in sorted(device_hours, reverse=True):
            feats = _features(device_hours, h)
            if feats is not None:
                X.append(feats)
                devices.append(device)
                hours.append(h)
                break
    return Examples(
        X=np.asarray(X, dtype=np.float32).reshape(-1, N_FEATURES),
        y=np.full(len(devices), math.nan, dtype=np.float32),
        devices=devices,
        hours=np.asarray(hours, dtype=np.int64),
    )


def yesterday_baseline(X: np.ndarray) -> np.ndarray:
    """Predict the same window as yesterday: the bar a model must clear.

    The next 24 hours, one day earlier, are exactly the 24 lags, so the
    baseline is the highest hourly mean of the last day.
    """
    return X[:, :LAGS].max(axis=1)
