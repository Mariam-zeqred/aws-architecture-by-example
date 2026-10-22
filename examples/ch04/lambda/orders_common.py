"""What the three functions share: the order's status machine and the event envelope.

An order is one DynamoDB item whose ``status`` only ever moves forward along
the transitions in ``NEXT``. Every function advances it with a conditional
update, so a message delivered twice (SQS standard is at-least-once) or a
function that died after writing finds the transition already made and
treats the message as done. The status item *is* the idempotency record;
there is no separate "processed messages" table to keep in step with it.
"""

from __future__ import annotations

import json
from typing import Any

SOURCE = "coop.orders"

# status -> the statuses it may move to. Anything else is refused.
NEXT: dict[str, set[str]] = {
    "placed": {"paid", "payment_failed"},
    "paid": {"reserved", "backordered"},
    "reserved": set(),
    "backordered": set(),
    "payment_failed": set(),
}
TERMINAL = {s for s, nxt in NEXT.items() if not nxt}


class Conflict(Exception):
    """The conditional update failed: the order is not in the status we expected."""


def is_conditional_failure(exc: Exception) -> bool:
    # boto3 raises a generated class; tests raise a class with the same name.
    return type(exc).__name__ in ("ConditionalCheckFailedException", "TransactionCanceledException")


def advance(table: Any, order_id: str, from_status: str, to_status: str, **fields: Any) -> None:
    """Move ``order_id`` from ``from_status`` to ``to_status`` or raise ``Conflict``.

    The condition is the whole point: two deliveries of the same message
    both try the move, exactly one succeeds, the other learns that the work
    is done. Extra ``fields`` (charge id, reason) are written in the same
    update so they can never be half-written.
    """
    if to_status not in NEXT[from_status]:
        raise ValueError(f"{from_status} -> {to_status} is not a transition")
    names = {"#s": "status"}
    values: dict[str, Any] = {":from": from_status, ":to": to_status}
    sets = ["#s = :to"]
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"] = k
        values[f":f{i}"] = v
        sets.append(f"#f{i} = :f{i}")
    try:
        table.update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="#s = :from",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception as exc:
        if is_conditional_failure(exc):
            raise Conflict(f"{order_id} is not {from_status}") from exc
        raise


# status -> the event that announces it. Re-announced on every duplicate delivery.
ANNOUNCES: dict[str, str] = {
    "paid": "OrderPaid",
    "payment_failed": "PaymentFailed",
    "reserved": "OrderReserved",
    "backordered": "OrderBackordered",
}
ANNOUNCED_FIELDS = ("charge_id", "reason", "short_skus")


def announce(order: dict[str, Any], publish) -> str:
    """Publish the event for the order's *stored* status again, and say which.

    Every function writes the status first and announces second, so a
    function that died in between left an order that is, say, ``paid`` and
    an ``OrderPaid`` nobody heard. The message comes back (the batch failed),
    the retry finds the status already moved, and announces it again. Every
    consumer treats a repeat as a duplicate, so the cost of announcing twice
    is one conditional-update failure; the cost of announcing zero times is
    an order that is paid for and never shipped.
    """
    detail_type = ANNOUNCES.get(order["status"])
    if detail_type is None:
        return f"{order['status']}, nothing to announce"
    detail = {"order_id": order["order_id"]}
    detail.update({k: order[k] for k in ANNOUNCED_FIELDS if k in order})
    publish(event_entry(detail_type, detail))
    return f"duplicate, {detail_type} announced again"


def event_entry(detail_type: str, detail: dict[str, Any], bus_name: str | None = None) -> dict:
    """One ``PutEvents`` entry. ``detail`` must be JSON; EventBridge adds id and time."""
    entry = {"Source": SOURCE, "DetailType": detail_type, "Detail": json.dumps(detail)}
    if bus_name:
        entry["EventBusName"] = bus_name
    return entry


def unwrap(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """An SQS record whose body is the EventBridge event -> (detail-type, detail).

    The body is the whole event as EventBridge delivered it to the queue
    (``version``, ``id``, ``detail-type``, ``source``, ``time``, ``detail``...).
    """
    event = json.loads(record["body"])
    return event["detail-type"], event["detail"]


def batch_response(failed_ids: list[str]) -> dict[str, list[dict[str, str]]]:
    """What a function returns so Lambda deletes the good records and retries the rest.

    Only works if the event source mapping has ``ReportBatchItemFailures``
    on; without it an exception fails the whole batch and the good records
    are redelivered with the bad one.
    """
    return {"batchItemFailures": [{"itemIdentifier": i} for i in failed_ids]}
