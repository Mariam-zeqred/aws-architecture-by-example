"""Consumer of ``OrderPlaced``: charge the farm once, then say ``OrderPaid``.

Fed by the payment queue in batches of up to ten SQS records. For each
record the function reads the order, and only an order still ``placed`` is
charged. The provider call carries the order id as its idempotency key, so
if this function dies after the provider answered but before the status
was written, the retry asks the provider again and gets the *same* charge
back, not a second one. The status move ``placed -> paid`` is conditional,
so of two deliveries racing each other exactly one publishes.

Records that fail for a reason a retry could fix (the provider timed out)
are reported back to Lambda one by one; the good ones in the same batch are
deleted from the queue. Records nothing can fix (not JSON, no such order)
are reported too, and after ``maxReceiveCount`` deliveries SQS moves them to
the dead-letter queue, where a person looks.
"""

from __future__ import annotations

import json
import os
from typing import Any

from orders_common import Conflict, advance, announce, batch_response, event_entry, unwrap

_resources: dict[str, Any] = {}


class ProviderUnavailable(Exception):
    """The provider timed out or returned 5xx: retry later with the same key."""


class StubProvider:
    """A stand-in for a payment provider's API.

    Approves every charge except totals ending in 99 cents (so the decline
    path can be exercised without a card) and remembers idempotency keys
    like a real provider does. The memory is this process only: a real
    provider keeps the key server-side, which is what makes the retry safe
    across Lambda containers. See the chapter's section 4.
    """

    def __init__(self) -> None:
        self.charges: dict[str, dict[str, Any]] = {}

    def charge(self, amount_cents: int, idempotency_key: str) -> dict[str, Any]:
        if idempotency_key in self.charges:
            return self.charges[idempotency_key]
        result = {
            "charge_id": f"ch_{idempotency_key[:8]}",
            "status": "declined" if amount_cents % 100 == 99 else "approved",
            "amount_cents": amount_cents,
        }
        self.charges[idempotency_key] = result
        return result


def charge_order(order_id: str, table: Any, provider: Any, publish) -> str:
    """Process one ``OrderPlaced``. Returns a one-word outcome for the log.

    Raises ``ProviderUnavailable`` (retry) or ``LookupError`` (dead-letter).
    """
    order = table.get_item(Key={"order_id": order_id}).get("Item")
    if order is None:
        raise LookupError(f"no order {order_id}")
    if order["status"] != "placed":
        # Seen before. The status was written but maybe never announced (the
        # function died between the update and the publish), so announce it
        # again; every consumer treats a repeat as a duplicate.
        return announce(order, publish)

    result = provider.charge(int(order["total_cents"]), idempotency_key=order_id)
    if result["status"] == "approved":
        to, detail_type = "paid", "OrderPaid"
        fields = {"charge_id": result["charge_id"]}
        detail = {"order_id": order_id, "charge_id": result["charge_id"]}
    else:
        to, detail_type = "payment_failed", "PaymentFailed"
        fields = {"reason": result["status"]}
        detail = {"order_id": order_id, "reason": result["status"]}
    try:
        advance(table, order_id, "placed", to, **fields)
    except Conflict:
        return "duplicate, lost the race"  # the other delivery publishes
    publish(event_entry(detail_type, detail))
    return to


def charge_batch(records: list[dict[str, Any]], table: Any, provider: Any, publish) -> dict:
    """The SQS batch: return the ids Lambda should put back on the queue."""
    failed: list[str] = []
    for record in records:
        try:
            detail_type, detail = unwrap(record)
            if detail_type != "OrderPlaced":
                raise LookupError(f"unexpected event {detail_type}")
            outcome = charge_order(detail["order_id"], table, provider, publish)
            print(json.dumps({"order_id": detail["order_id"], "outcome": outcome}))
        except ProviderUnavailable as exc:
            print(json.dumps({"message_id": record["messageId"], "retry": str(exc)}))
            failed.append(record["messageId"])
        except (LookupError, KeyError, ValueError, TypeError) as exc:
            # Nothing a retry fixes; after maxReceiveCount SQS dead-letters it.
            print(json.dumps({"message_id": record["messageId"], "poison": repr(exc)}))
            failed.append(record["messageId"])
    return batch_response(failed)


def _aws_resources() -> dict[str, Any]:
    if not _resources:
        import boto3  # imported lazily so tests never need it

        bus = os.environ["BUS_NAME"]
        events = boto3.client("events")

        def publish(entry: dict) -> None:
            result = events.put_events(Entries=[dict(entry, EventBusName=bus)])
            if result.get("FailedEntryCount"):
                raise RuntimeError(f"PutEvents failed: {result['Entries']}")

        _resources.update(
            table=boto3.resource("dynamodb").Table(os.environ["ORDERS_TABLE"]),
            provider=StubProvider(),  # the only provider shipped; see the README
            publish=publish,
        )
    return _resources


def lambda_handler(event: dict[str, Any], context: Any) -> dict:
    r = _aws_resources()
    return charge_batch(event["Records"], r["table"], r["provider"], r["publish"])
