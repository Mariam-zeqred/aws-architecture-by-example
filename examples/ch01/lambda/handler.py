"""The whole API: three routes, one table, standard library + boto3 only.

Routes (all behind CloudFront at ``/api/*``):
    GET  /api/health                     -> {"ok": true}
    GET  /api/readings/{device}?limit=N  -> newest N readings for one device
    POST /api/readings                   -> store one reading

The table is injected (``table=``) so the logic is unit-testable without AWS.
"""

from __future__ import annotations

import json
import os
import re
import time
from decimal import Decimal
from typing import Any

DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
LIMITS = {"temp": (-40.0, 85.0), "hum": (0.0, 100.0)}  # the sensor's own range
RETENTION_SECONDS = 30 * 24 * 3600
DEFAULT_LIMIT, MAX_LIMIT = 50, 500

_table = None


def _default_table():
    """Create the boto3 table resource on first use (keeps cold start short)."""
    global _table
    if _table is None:
        import boto3

        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def _response(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, default=_plain),
    }


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(type(value))


def validate_reading(body: dict) -> dict:
    """Return a clean item or raise ValueError with a message the client can show."""
    device = str(body.get("device", ""))
    if not DEVICE_RE.match(device):
        raise ValueError("device must be 2-32 chars of a-z, 0-9 or '-'")
    item: dict[str, Any] = {"device": device}
    for field, (lo, hi) in LIMITS.items():
        if field not in body:
            raise ValueError(f"{field} is required")
        try:
            value = float(body[field])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a number") from exc
        if not lo <= value <= hi:
            raise ValueError(f"{field} must be between {lo} and {hi}")
        item[field] = Decimal(str(round(value, 2)))  # DynamoDB has no float type
    ts = int(body.get("ts") or time.time())
    item["ts"] = ts
    item["expires"] = ts + RETENTION_SECONDS
    return item


def handle(event: dict, table=None) -> dict:
    table = table if table is not None else _default_table()
    method = event["requestContext"]["http"]["method"]
    path = event["rawPath"]

    if method == "GET" and path == "/api/health":
        return _response(200, {"ok": True})

    if method == "GET" and path.startswith("/api/readings/"):
        device = event.get("pathParameters", {}).get("device", "")
        if not DEVICE_RE.match(device):
            return _response(400, {"error": "bad device id"})
        raw_limit = (event.get("queryStringParameters") or {}).get("limit", DEFAULT_LIMIT)
        try:
            limit = max(1, min(int(raw_limit), MAX_LIMIT))
        except ValueError:
            return _response(400, {"error": "limit must be an integer"})
        from boto3.dynamodb.conditions import Key

        result = table.query(
            KeyConditionExpression=Key("device").eq(device),
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
        return _response(200, {"device": device, "readings": result.get("Items", [])})

    if method == "POST" and path == "/api/readings":
        try:
            body = json.loads(event.get("body") or "{}")
            item = validate_reading(body)
        except (json.JSONDecodeError, ValueError) as exc:
            return _response(400, {"error": str(exc)})
        table.put_item(Item=item)
        return _response(201, {"stored": item})

    return _response(404, {"error": "no such route"})


def lambda_handler(event, _context):
    return handle(event)
