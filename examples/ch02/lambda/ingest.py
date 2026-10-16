"""Hot path: one IoT rule message in, two DynamoDB writes out.

The rule's SQL adds ``device`` (from the topic, so the device cannot lie
about its identity) and ``received`` (the broker's clock, in ms) to the
payload the device sent. This function validates the reading, writes one
history item that expires after HISTORY_DAYS, and overwrites the device's
status item only if the reading is newer than the one already there, so
redelivered or out-of-order messages cannot roll the status back.

Anything that fails validation is sent to the rejects queue with a reason
and is never written. The tables and the queue are passed in as objects so
tests run against fakes with no AWS account.
"""

from __future__ import annotations

import json
import os
import re
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
LIMITS = {"temp": (-40.0, 85.0), "hum": (0.0, 100.0)}  # the sensor's own range
MAX_FUTURE_S = 300  # a device clock more than 5 min ahead of the broker is wrong
MAX_AGE_S = 7 * 86_400  # older than a week: buffered too long, or the clock is wrong

_resources: dict[str, Any] = {}


class Rejected(ValueError):
    """The reading is not written; ``str(exc)`` is the reason sent to the queue."""


def _two_places(x: float) -> Decimal:
    # DynamoDB has no float type, and the sensor has no third decimal.
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def validate(message: dict[str, Any]) -> dict[str, Any]:
    """Return a clean record or raise ``Rejected``.

    ``ts`` is the device's timestamp in seconds; ``received`` is the broker's
    in milliseconds (from ``timestamp()`` in the rule SQL). If the device's
    clock is missing or implausible, the broker's time is used and the
    record is marked ``clock = "server"`` so the history says so.
    """
    device = message.get("device")
    if not isinstance(device, str) or not DEVICE_RE.match(device):
        raise Rejected("device id missing or malformed")
    received = message.get("received")
    if not isinstance(received, int | float) or received <= 0:
        raise Rejected("received (broker timestamp) missing")
    received_s = int(received // 1000)

    record: dict[str, Any] = {"device": device, "received": int(received)}
    for field, (lo, hi) in LIMITS.items():
        value = message.get(field)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise Rejected(f"{field} must be a number")
        if not lo <= value <= hi:
            raise Rejected(f"{field} out of sensor range [{lo}, {hi}]")
        record[field] = _two_places(float(value))

    ts = message.get("ts")
    if isinstance(ts, int | float) and not isinstance(ts, bool):
        ts = int(ts)
        if ts > received_s + MAX_FUTURE_S or ts < received_s - MAX_AGE_S:
            record["clock"] = "server"
            ts = received_s
    else:
        record["clock"] = "server"
        ts = received_s
    record["ts"] = ts
    return record


def ingest(
    message: dict[str, Any],
    status_table: Any,
    history_table: Any,
    send_reject,
    history_days: int = 30,
) -> dict[str, Any]:
    """Process one message. Returns what happened, for logs and tests."""
    try:
        record = validate(message)
    except Rejected as exc:
        send_reject(json.dumps({"reason": str(exc), "message": message}))
        return {"outcome": "rejected", "reason": str(exc)}

    history_item = dict(record, expires=record["ts"] + history_days * 86_400)
    history_table.put_item(Item=history_item)

    outcome = "written"
    try:
        status_table.put_item(
            Item=record,
            # Only move the status forward in time. A redelivered QoS 1
            # message or a buffered backlog arriving late cannot overwrite a
            # newer reading with an older one.
            ConditionExpression="attribute_not_exists(ts) OR ts < :ts",
            ExpressionAttributeValues={":ts": record["ts"]},
        )
    except Exception as exc:  # boto3 raises a generated class; match by name
        if type(exc).__name__ != "ConditionalCheckFailedException":
            raise
        outcome = "written, status kept (older than current)"
    return {"outcome": outcome, "device": record["device"], "ts": record["ts"]}


def _aws_resources() -> dict[str, Any]:
    if not _resources:
        import boto3  # imported lazily so tests never need it

        ddb = boto3.resource("dynamodb")
        sqs = boto3.client("sqs")
        url = os.environ["REJECTS_URL"]
        _resources.update(
            status=ddb.Table(os.environ["STATUS_TABLE"]),
            history=ddb.Table(os.environ["HISTORY_TABLE"]),
            send_reject=lambda body: sqs.send_message(QueueUrl=url, MessageBody=body),
        )
    return _resources


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point. The IoT rule's Lambda action delivers one message per call."""
    r = _aws_resources()
    started = time.monotonic()
    result = ingest(
        event,
        r["status"],
        r["history"],
        r["send_reject"],
        history_days=int(os.environ.get("HISTORY_DAYS", "30")),
    )
    result["ms"] = round((time.monotonic() - started) * 1000, 1)
    print(json.dumps(result))  # one line per message in CloudWatch Logs
    return result
