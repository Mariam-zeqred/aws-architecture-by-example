"""Train, evaluate on a time split, compare with the champion, save, load."""

from __future__ import annotations

import io
import json
from dataclasses import asdict, dataclass

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor

from features import Examples, yesterday_baseline

HOLDOUT_HOURS = 7 * 24


@dataclass
class Metrics:
    rows: int
    holdout_rows: int
    mae: float  # candidate on the hold-out week
    baseline_mae: float  # "same as yesterday" on the same rows
    champion_mae: float | None  # the current model on the same rows, if there is one
    sklearn_version: str = sklearn.__version__

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def time_split(ex: Examples, holdout_hours: int = HOLDOUT_HOURS) -> tuple[np.ndarray, np.ndarray]:
    """Train on everything before the last ``holdout_hours``; test on them.

    A random split would put Tuesday 14:00 in the test set and Tuesday 13:00
    and 15:00 in the training set, and the model would look better than it
    is. The hold-out must be *later* than everything it is compared with.
    """
    cutoff = int(ex.hours.max()) - holdout_hours
    test = ex.hours > cutoff
    return ~test, test


def fit(X: np.ndarray, y: np.ndarray) -> HistGradientBoostingRegressor:
    model = HistGradientBoostingRegressor(
        max_iter=200, learning_rate=0.05, max_leaf_nodes=15, random_state=0
    )
    model.fit(X, y)
    return model


def mae(model, X: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean(np.abs(model.predict(X) - y)))


def train_and_evaluate(
    ex: Examples, champion=None
) -> tuple[HistGradientBoostingRegressor, Metrics]:
    """Fit on the training part; score candidate, baseline and champion on the hold-out."""
    train, test = time_split(ex)
    if train.sum() == 0 or test.sum() == 0:
        raise ValueError(f"not enough data for a time split: {len(ex)} rows")
    model = fit(ex.X[train], ex.y[train])
    metrics = Metrics(
        rows=int(train.sum()),
        holdout_rows=int(test.sum()),
        mae=mae(model, ex.X[test], ex.y[test]),
        baseline_mae=float(np.mean(np.abs(yesterday_baseline(ex.X[test]) - ex.y[test]))),
        champion_mae=None if champion is None else mae(champion, ex.X[test], ex.y[test]),
    )
    return model, metrics


def dumps(model) -> bytes:
    buf = io.BytesIO()
    joblib.dump(model, buf)
    return buf.getvalue()


def loads(data: bytes):
    return joblib.load(io.BytesIO(data))
