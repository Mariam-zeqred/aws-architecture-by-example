"""Fleet watchdog: one metric for the fleet instead of one alarm per device.

Every five minutes (EventBridge schedule) this scans the status table and
counts devices whose last message is older than STALE_AFTER_S. It publishes
the count as one CloudWatch metric, which one alarm watches. The scan reads
the whole table, which is fine up to roughly 10^5 devices (section 5 of the
chapter prices it); beyond that, exercise 5 asks for the index that avoids it.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any


def find_stale(status_table: Any, now_s: int, stale_after_s: int) -> list[dict[str, Any]]:
    """Return ``[{"device", "silent_for_s"}, ...]`` for every stale device."""
    cutoff_ms = (now_s - stale_after_s) * 1000
    stale: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"ProjectionExpression": "device, received"}
    while True:
        page = status_table.scan(**kwargs)
        for item in page.get("Items", []):
            received_ms = int(item["received"])
            if received_ms < cutoff_ms:
                stale.append(
                    {"device": item["device"], "silent_for_s": now_s - received_ms // 1000}
                )
        if "LastEvaluatedKey" not in page:
            return sorted(stale, key=lambda d: d["device"])
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def metric_datum(stale_count: int, namespace: str) -> dict[str, Any]:
    """The ``put_metric_data`` call body: one number, no per-device dimension.

    A per-device dimension would make this one metric per device again, and
    CloudWatch bills per metric; the device list goes to the log instead.
    """
    return {
        "Namespace": namespace,
        "MetricData": [{"MetricName": "StaleDevices", "Value": stale_count, "Unit": "Count"}],
    }


def run(status_table: Any, cloudwatch: Any, now_s: int, stale_after_s: int, namespace: str) -> dict:
    stale = find_stale(status_table, now_s, stale_after_s)
    cloudwatch.put_metric_data(**metric_datum(len(stale), namespace))
    return {"stale": len(stale), "devices": [d["device"] for d in stale[:50]]}


def lambda_handler(event: dict[str, Any], context: Any) -> dict:
    import boto3  # lazy so tests never need it

    table = boto3.resource("dynamodb").Table(os.environ["STATUS_TABLE"])
    result = run(
        table,
        boto3.client("cloudwatch"),
        now_s=int(time.time()),
        stale_after_s=int(os.environ.get("STALE_AFTER_S", "600")),
        namespace=os.environ.get("METRIC_NAMESPACE", "FarmTelemetry"),
    )
    print(json.dumps(result))
    return result
