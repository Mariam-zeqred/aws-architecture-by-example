"""The gate: decide whether last night's candidate becomes the current model.

Invoked by the state machine after the training task, with
``{"run_id": "2026-10-19"}``. Reads ``models/candidate/<run_id>/metrics.json``
(written by the task, with the candidate, the "same as yesterday" baseline
and the current champion all scored on the same hold-out week) and applies
two rules:

1. the candidate must beat the baseline, otherwise the model is not earning
   its keep and the fleet is better served by yesterday's numbers;
2. if there is a current model, the candidate must be at least as good as it
   on the same rows; "worse by a little" is still worse.

Promotion is a copy to ``models/current/`` plus a pointer file naming the
run, so the scoring task and a human can both see which night's model is
live. The function returns the decision; the state machine only needs
``model_available`` to continue.

Run locally with ``python gate.py --artifacts DIR --run-id 2026-10-19``.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

CANDIDATE = "models/candidate/{run_id}/"
CURRENT = "models/current/"
FILES = ("model.joblib", "metrics.json")


class S3Store:
    def __init__(self, bucket: str, client=None) -> None:
        self.bucket = bucket
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.ClientError:
            return False
        return True

    def copy(self, src: str, dst: str) -> None:
        self.client.copy_object(
            Bucket=self.bucket, CopySource={"Bucket": self.bucket, "Key": src}, Key=dst
        )


class LocalStore:
    """The same four calls on a directory, for the local run and the tests."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def put(self, key: str, data: bytes) -> None:
        (self.root / key).parent.mkdir(parents=True, exist_ok=True)
        (self.root / key).write_bytes(data)

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()

    def copy(self, src: str, dst: str) -> None:
        self.put(dst, self.get(src))


def decide(metrics: dict) -> tuple[bool, str]:
    """(promote?, reason) from the metrics the training task wrote."""
    if metrics["mae"] > metrics["baseline_mae"]:
        return False, (
            f"candidate MAE {metrics['mae']:.3f} does not beat the yesterday baseline "
            f"{metrics['baseline_mae']:.3f}"
        )
    if metrics.get("champion_mae") is None:
        return True, f"no current model; candidate MAE {metrics['mae']:.3f}"
    if metrics["mae"] <= metrics["champion_mae"]:
        return True, (
            f"candidate MAE {metrics['mae']:.3f} <= current model {metrics['champion_mae']:.3f} "
            "on the same hold-out"
        )
    return False, (
        f"candidate MAE {metrics['mae']:.3f} is worse than the current model "
        f"{metrics['champion_mae']:.3f} on the same hold-out"
    )


def gate(store, run_id: str) -> dict:
    candidate = CANDIDATE.format(run_id=run_id)
    metrics = json.loads(store.get(candidate + "metrics.json"))
    promoted, reason = decide(metrics)
    if promoted:
        for name in FILES:
            store.copy(candidate + name, CURRENT + name)
        pointer = {"run_id": run_id, "promoted_at": datetime.now(UTC).isoformat()}
        store.put(CURRENT + "pointer.json", json.dumps(pointer).encode())
    return {
        "run_id": run_id,
        "promoted": promoted,
        "reason": reason,
        "model_available": store.exists(CURRENT + "model.joblib"),
        "candidate_mae": metrics["mae"],
        "baseline_mae": metrics["baseline_mae"],
        "champion_mae": metrics.get("champion_mae"),
    }


def lambda_handler(event: dict, context=None, store=None) -> dict:
    store = store or S3Store(os.environ["ARTIFACTS_BUCKET"])
    result = gate(store, event["run_id"])
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="run the gate against a local artifacts dir")
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    print(json.dumps(gate(LocalStore(args.artifacts), args.run_id), indent=2))
