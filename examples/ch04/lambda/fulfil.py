"""Consumer of ``OrderPaid``: take the stock out of the shelf exactly once.

One DynamoDB transaction does three things or none of them: moves the order
``paid -> reserved`` (condition: it is ``paid``), and for every line takes
``qty`` off the SKU's ``available`` (condition: enough is there). A second
delivery of the same ``OrderPaid`` fails the first condition and is done.
An order the shelf cannot fill fails a stock condition; it is moved to
``backordered`` and a person is told, because the farm has already paid.

The transaction is passed in as a function (``transact(items)``) so tests
and the simulator supply one over an in-memory table.
"""

from __future__ import annotations

import json
import os
from typing import Any

from orders_common import Conflict, advance, announce, batch_response, event_entry, unwrap

_resources: dict[str, Any] = {}


class TransactionCanceled(Exception):
    """``reasons[i]`` is the cancellation code of item ``i`` or ``None`` if it was fine."""

    def __init__(self, reasons: list[str | None]) -> None:
        super().__init__(reasons)
        self.reasons = reasons


def merged_lines(lines: list[dict[str, Any]]) -> dict[str, int]:
    """SKU -> total quantity. A transaction may not touch the same item twice."""
    out: dict[str, int] = {}
    for line in lines:
        out[line["sku"]] = out.get(line["sku"], 0) + int(line["qty"])
    return out


def reservation(order: dict[str, Any], orders_table: str, stock_table: str) -> list[dict]:
    """The transaction items: the status move first, then one update per SKU."""
    items = [
        {
            "Update": {
                "TableName": orders_table,
                "Key": {"order_id": order["order_id"]},
                "UpdateExpression": "SET #s = :to",
                "ConditionExpression": "#s = :from",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {":from": "paid", ":to": "reserved"},
            }
        }
    ]
    for sku, qty in merged_lines(order["lines"]).items():
        items.append(
            {
                "Update": {
                    "TableName": stock_table,
                    "Key": {"sku": sku},
                    "UpdateExpression": "SET available = available - :q",
                    "ConditionExpression": "available >= :q",
                    "ExpressionAttributeValues": {":q": qty},
                }
            }
        )
    return items


def fulfil_order(order_id: str, table: Any, transact, publish, names: tuple[str, str]) -> str:
    """Process one ``OrderPaid``. Returns a one-word outcome for the log."""
    order = table.get_item(Key={"order_id": order_id}).get("Item")
    if order is None:
        raise LookupError(f"no order {order_id}")
    if order["status"] != "paid":
        return announce(order, publish)  # written before, maybe never announced
    try:
        transact(reservation(order, *names))
    except TransactionCanceled as exc:
        if exc.reasons and exc.reasons[0] not in (None, "None"):
            return "duplicate, lost the race"  # the status condition failed
        short = [
            sku
            for sku, reason in zip(merged_lines(order["lines"]), exc.reasons[1:], strict=True)
            if reason not in (None, "None")
        ]
        try:
            advance(table, order_id, "paid", "backordered", short_skus=short)
        except Conflict:
            return "duplicate, lost the race"
        publish(event_entry("OrderBackordered", {"order_id": order_id, "short_skus": short}))
        return "backordered"
    publish(event_entry("OrderReserved", {"order_id": order_id}))
    return "reserved"


def fulfil_batch(
    records: list[dict[str, Any]], table: Any, transact, publish, names: tuple[str, str]
) -> dict:
    failed: list[str] = []
    for record in records:
        try:
            detail_type, detail = unwrap(record)
            if detail_type != "OrderPaid":
                raise LookupError(f"unexpected event {detail_type}")
            outcome = fulfil_order(detail["order_id"], table, transact, publish, names)
            print(json.dumps({"order_id": detail["order_id"], "outcome": outcome}))
        except (LookupError, KeyError, ValueError, TypeError) as exc:
            print(json.dumps({"message_id": record["messageId"], "poison": repr(exc)}))
            failed.append(record["messageId"])
    return batch_response(failed)


def _aws_resources() -> dict[str, Any]:
    if not _resources:
        import boto3  # imported lazily so tests never need it
        from boto3.dynamodb.types import TypeSerializer

        client = boto3.client("dynamodb")
        ser = TypeSerializer()
        bus = os.environ["BUS_NAME"]
        events = boto3.client("events")

        def transact(items: list[dict]) -> None:
            low = []
            for item in items:
                u = dict(item["Update"])
                u["Key"] = {k: ser.serialize(v) for k, v in u["Key"].items()}
                u["ExpressionAttributeValues"] = {
                    k: ser.serialize(v) for k, v in u["ExpressionAttributeValues"].items()
                }
                low.append({"Update": u})
            try:
                client.transact_write_items(TransactItems=low)
            except client.exceptions.TransactionCanceledException as exc:
                reasons = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
                raise TransactionCanceled(reasons) from exc

        def publish(entry: dict) -> None:
            result = events.put_events(Entries=[dict(entry, EventBusName=bus)])
            if result.get("FailedEntryCount"):
                raise RuntimeError(f"PutEvents failed: {result['Entries']}")

        _resources.update(
            table=boto3.resource("dynamodb").Table(os.environ["ORDERS_TABLE"]),
            transact=transact,
            publish=publish,
            names=(os.environ["ORDERS_TABLE"], os.environ["STOCK_TABLE"]),
        )
    return _resources


def lambda_handler(event: dict[str, Any], context: Any) -> dict:
    r = _aws_resources()
    return fulfil_batch(event["Records"], r["table"], r["transact"], r["publish"], r["names"])
