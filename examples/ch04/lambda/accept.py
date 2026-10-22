"""POST /orders: accept an order exactly once and announce it.

The client sends its own ``order_id`` (a UUID it generated before the first
attempt) and retries with the same one until it gets a 2xx. The function
writes the order with a condition that the id is new. If the condition
fails, the order is already there: either an earlier attempt succeeded
and the client never saw the reply, or the client is repeating itself.
Both get a 200 with the stored order, and the event is published again if
the order has not moved on, which closes the gap between "written" and
"announced" without an outbox table: the consumers are idempotent, so an
extra ``OrderPlaced`` costs one conditional-update failure downstream.

The table and the publish function are passed in so tests run without AWS.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from orders_common import event_entry, is_conditional_failure

ORDER_ID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
FARM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,15}$")
MAX_LINES = 20
MAX_QTY = 1_000
MAX_CENTS = 50_000_000  # 500 000 in the shop's currency; above this is a typo

_resources: dict[str, Any] = {}


def _response(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def validate(body: Any) -> dict[str, Any]:
    """Return a clean order item or raise ValueError with a message for the client."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    order_id = body.get("order_id")
    if not isinstance(order_id, str) or not ORDER_ID_RE.match(order_id):
        raise ValueError("order_id must be a UUID the client generated (lower-case hex)")
    farm = body.get("farm")
    if not isinstance(farm, str) or not FARM_RE.match(farm):
        raise ValueError("farm must be 2-32 chars of a-z, 0-9 or '-'")
    lines = body.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
        raise ValueError(f"lines must be a list of 1-{MAX_LINES} items")
    clean_lines, total = [], 0
    for line in lines:
        if not isinstance(line, dict):
            raise ValueError("each line must be an object")
        sku, qty, cents = line.get("sku"), line.get("qty"), line.get("unit_cents")
        if not isinstance(sku, str) or not SKU_RE.match(sku):
            raise ValueError("sku must be 2-16 chars of A-Z, 0-9 or '-'")
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
            raise ValueError(f"qty must be an integer 1-{MAX_QTY}")
        if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0:
            raise ValueError("unit_cents must be a non-negative integer")
        clean_lines.append({"sku": sku, "qty": qty, "unit_cents": cents})
        total += qty * cents
    if total > MAX_CENTS:
        raise ValueError("order total is implausibly large")
    return {"order_id": order_id, "farm": farm, "lines": clean_lines, "total_cents": total}


def accept(body: Any, table: Any, publish, now: int | None = None) -> dict:
    """Store the order once, publish ``OrderPlaced``, return the HTTP response."""
    try:
        order = validate(body)
    except ValueError as exc:
        return _response(400, {"error": str(exc)})
    order.update(status="placed", placed_at=now or int(time.time()))
    outcome = "accepted"
    try:
        table.put_item(Item=order, ConditionExpression="attribute_not_exists(order_id)")
    except Exception as exc:
        if not is_conditional_failure(exc):
            raise
        # Seen before. Return what is stored; re-announce only if nothing has
        # happened to it yet (a lost reply from a successful first attempt).
        stored = table.get_item(Key={"order_id": order["order_id"]}).get("Item")
        if stored is None:
            raise
        if stored.get("status") != "placed":
            return _response(200, {"order_id": order["order_id"], "status": stored["status"]})
        order, outcome = stored, "already accepted, announced again"
    publish(
        event_entry(
            "OrderPlaced", {"order_id": order["order_id"], "total_cents": order["total_cents"]}
        )
    )
    return _response(
        201 if outcome == "accepted" else 200,
        {"order_id": order["order_id"], "status": "placed", "outcome": outcome},
    )


def _aws_resources() -> dict[str, Any]:
    if not _resources:
        import boto3  # imported lazily so tests never need it

        bus = os.environ["BUS_NAME"]
        events = boto3.client("events")

        def publish(entry: dict) -> None:
            entry = dict(entry, EventBusName=bus)
            result = events.put_events(Entries=[entry])
            if result.get("FailedEntryCount"):
                raise RuntimeError(f"PutEvents failed: {result['Entries']}")

        _resources.update(
            table=boto3.resource("dynamodb").Table(os.environ["ORDERS_TABLE"]), publish=publish
        )
    return _resources


def lambda_handler(event: dict[str, Any], context: Any) -> dict:
    """HTTP API v2 entry point for ``POST /orders``."""
    r = _aws_resources()
    try:
        body = json.loads(event.get("body") or "null")
    except json.JSONDecodeError:
        return _response(400, {"error": "body is not JSON"})
    response = accept(body, r["table"], r["publish"])
    print(json.dumps({"route": "POST /orders", "status": response["statusCode"]}))
    return response
