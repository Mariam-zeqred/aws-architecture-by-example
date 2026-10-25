"""The chapter 5 API, deployed unchanged in both regions.

Three routes behind one HTTP API:

* ``GET /health``: reads one item from the orders table and answers 200 with
  the region that served it. Route 53 calls this from outside both regions;
  because it reads the table, a region whose DynamoDB is unavailable fails
  the check too, and DNS moves to the standby.
* ``GET /orders/{order_id}``: the order as stored in *this* region's replica.
* ``POST /orders``: chapter 4's guard one, a conditional put on the client's
  order id, with one extra attribute: ``accepted_in``, the region that took
  the order. The chapter's section 4 says why that attribute matters.

Nothing here publishes events: in the standby, the charge and fulfil
consumers of chapter 4 are not running, and the chapter explains why they
must not run in two regions at once.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

REGION = os.environ.get("AWS_REGION", "local")
ROLE = os.environ.get("REGION_ROLE", "primary")
HEALTH_KEY = "__health__"  # an item that is allowed not to exist; the read is the test

_table = None


def table():
    """The boto3 table, created on first use so tests can inject a fake."""
    global _table
    if _table is None:
        import boto3

        _table = boto3.resource("dynamodb").Table(os.environ["ORDERS_TABLE"])
    return _table


def set_table(fake: Any) -> None:
    global _table
    _table = fake


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({**body, "served_by": REGION, "role": ROLE}, default=str),
    }


def health() -> dict[str, Any]:
    table().get_item(Key={"order_id": HEALTH_KEY})  # raises if the table is unreachable
    return _response(200, {"ok": True})


def get_order(order_id: str) -> dict[str, Any]:
    item = table().get_item(Key={"order_id": order_id}).get("Item")
    if item is None:
        return _response(404, {"error": "no such order"})
    return _response(200, {"order": item})


def accept(body: str | None) -> dict[str, Any]:
    try:
        data = json.loads(body or "")
        order_id = str(uuid.UUID(data["order_id"]))
        lines = data["lines"]
        if not isinstance(lines, list) or not lines:
            raise ValueError("lines must be a non-empty list")
        for line in lines:
            if int(line["qty"]) <= 0 or int(line["unit_cents"]) < 0:
                raise ValueError("qty must be positive and unit_cents non-negative")
        farm = str(data["farm"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _response(400, {"error": f"bad order: {exc}"})

    order = {
        "order_id": order_id,
        "farm": farm,
        "lines": lines,
        "total_cents": sum(int(li["qty"]) * int(li["unit_cents"]) for li in lines),
        "status": "placed",
        "accepted_in": REGION,  # where the write happened; the other region sees it later
    }
    try:
        table().put_item(Item=order, ConditionExpression="attribute_not_exists(order_id)")
    except Exception as exc:  # ConditionalCheckFailedException, by name below
        if type(exc).__name__ != "ConditionalCheckFailedException":
            raise
        stored = table().get_item(Key={"order_id": order_id}, ConsistentRead=True).get("Item")
        return _response(200, {"order": stored, "note": "already accepted"})
    return _response(201, {"order": order})


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    if method == "GET" and path == "/health":
        return health()
    if method == "GET" and path.startswith("/orders/"):
        return get_order(event.get("pathParameters", {}).get("order_id") or path[len("/orders/") :])
    if method == "POST" and path == "/orders":
        return accept(event.get("body"))
    return _response(404, {"error": f"no route for {method} {path}"})
