"""The container's entry point: ``train`` one night, ``score`` one morning.

    python -m cli train --archive ./archive --artifacts ./artifacts --run-id 2026-10-19 --days 21
    python -m cli score --archive ./archive --artifacts ./artifacts --run-id 2026-10-19

``--archive`` and ``--artifacts`` are directories or ``s3://`` prefixes; the
Fargate task gets them from its environment. Keys written:

    models/candidate/<run-id>/model.joblib, metrics.json      (train)
    predictions/dt=<run-id>/predictions.jsonl, summary.json   (score)

The gate (lambda/gate.py) is what moves a candidate to models/current/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta

import model as model_mod
from archive import day_range, iter_readings
from features import build_examples, hourly_table, latest_examples
from storage import Store

CURRENT_MODEL = "models/current/model.joblib"
THRESHOLD_C = 30.0  # the brief: warn when a field is forecast above 30 °C


def _as_of(run_id: str) -> date:
    """The run id is the morning the predictions are for; the data ends the night before."""
    return date.fromisoformat(run_id) - timedelta(days=1)


def train(archive: Store, artifacts: Store, run_id: str, days: int) -> dict:
    t0 = time.perf_counter()
    stats: dict[str, int] = {}
    table = hourly_table(iter_readings(archive, day_range(_as_of(run_id), days), stats))
    t_read = time.perf_counter() - t0
    ex = build_examples(table)
    champion = (
        model_mod.loads(artifacts.get(CURRENT_MODEL)) if artifacts.exists(CURRENT_MODEL) else None
    )
    fitted, metrics = model_mod.train_and_evaluate(ex, champion)
    prefix = f"models/candidate/{run_id}/"
    artifacts.put(prefix + "model.joblib", model_mod.dumps(fitted))
    artifacts.put(prefix + "metrics.json", metrics.to_json().encode())
    return {
        "run_id": run_id,
        "days": days,
        "devices": len(table),
        **stats,
        "read_seconds": round(t_read, 2),
        "lines_per_second": int(stats["lines"] / t_read) if t_read > 0 else None,
        "total_seconds": round(time.perf_counter() - t0, 2),
        **json.loads(metrics.to_json()),
    }


def score(archive: Store, artifacts: Store, run_id: str, table_writer=None) -> dict:
    """Predict the coming day's peak for every device from the last two days of readings."""
    fitted = model_mod.loads(artifacts.get(CURRENT_MODEL))
    table = hourly_table(iter_readings(archive, day_range(_as_of(run_id), 2)))
    ex = latest_examples(table)
    if len(ex) == 0:
        raise RuntimeError("no device has 24 complete hours in the last two days")
    preds = fitted.predict(ex.X)
    lines = []
    over = 0
    for device, hour, pred in zip(ex.devices, ex.hours, preds, strict=True):
        item = {
            "device": device,
            "dt": run_id,
            "as_of": datetime.fromtimestamp((int(hour) + 1) * 3600, tz=UTC).isoformat(),
            "predicted_max_24h": round(float(pred), 2),
            "over_threshold": bool(pred > THRESHOLD_C),
        }
        over += item["over_threshold"]
        lines.append(json.dumps(item))
        if table_writer is not None:
            table_writer.put_item(Item=item)
    summary = {
        "run_id": run_id,
        "devices_scored": len(lines),
        "devices_over_threshold": over,
        "threshold_c": THRESHOLD_C,
    }
    prefix = f"predictions/dt={run_id}/"
    artifacts.put(prefix + "predictions.jsonl", ("\n".join(lines) + "\n").encode())
    artifacts.put(prefix + "summary.json", json.dumps(summary, indent=2).encode())
    return summary


def _dynamodb_writer(table_name: str):
    import boto3  # only when a table is given, i.e. in the Fargate task

    table = boto3.resource("dynamodb").Table(table_name)
    writer = table.batch_writer()
    writer.__enter__()
    return writer


class _FloatSafeWriter:
    """DynamoDB wants Decimal, not float; convert on the way in."""

    def __init__(self, writer) -> None:
        self.writer = writer

    def put_item(self, Item: dict) -> None:  # noqa: N803 (boto3's spelling)
        from decimal import Decimal

        self.writer.put_item(
            Item={k: Decimal(str(v)) if isinstance(v, float) else v for k, v in Item.items()}
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="nightly train / morning score")
    parser.add_argument("command", choices=["train", "score"])
    parser.add_argument("--archive", default=os.environ.get("ARCHIVE_ROOT"))
    parser.add_argument("--artifacts", default=os.environ.get("ARTIFACTS_ROOT"))
    parser.add_argument("--run-id", default=os.environ.get("RUN_ID"))
    parser.add_argument("--days", type=int, default=int(os.environ.get("TRAINING_DAYS", "90")))
    parser.add_argument("--table", default=os.environ.get("PREDICTIONS_TABLE"))
    args = parser.parse_args(argv)
    if not (args.archive and args.artifacts and args.run_id):
        parser.error("--archive, --artifacts and --run-id are required (or their env vars)")
    archive, artifacts = Store(args.archive), Store(args.artifacts)
    if args.command == "train":
        result = train(archive, artifacts, args.run_id, args.days)
    else:
        writer = _FloatSafeWriter(_dynamodb_writer(args.table)) if args.table else None
        try:
            result = score(archive, artifacts, args.run_id, writer)
        finally:
            if writer is not None:
                writer.writer.__exit__(None, None, None)
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
