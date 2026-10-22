"""Run the three functions against an at-least-once world, with and without their guards.

Usage: ``python sim.py [--orders N] [--seed S]`` prints the chapter's section 4
table. Everything here is in memory and labelled as such: fake SQS queues
that deliver every message at least once and sometimes twice, a fake
payment provider that sometimes charges and then times out, a Lambda
runtime that sometimes dies right after a database write, and clients that
sometimes never see their reply and try again.

The same scenario is run twice: once through ``accept``, ``charge`` and
``fulfil`` as they are, once through "naive" versions of the same steps
with the guards removed (server-made ids, no idempotency key, unconditional
writes). Nothing in the numbers is measured from a real shop; the
probabilities are the parameters at the top.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import random
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent / "lambda"))

import accept  # noqa: E402
import charge  # noqa: E402
import fulfil  # noqa: E402
from orders_common import SOURCE, event_entry  # noqa: E402

ORDERS_TABLE, STOCK_TABLE = "orders", "stock"
MAX_RECEIVE_COUNT = 5  # the queues' redrive policy, as in stack.py
BATCH_SIZE = 10

# The unreliable world. Illustrative, not measured.
P_CLIENT_LOST_REPLY = 0.10  # the client never sees the 201 and posts again
P_DUPLICATE_DELIVERY = 0.05  # SQS standard hands a message out a second time
P_PROVIDER_LOST_RESPONSE = 0.05  # the provider charged, then the response timed out
P_CRASH_AFTER_WRITE = 0.03  # the function died after a table write, before returning
POISON_MESSAGES = 3  # messages nobody can process


# --- Fakes --------------------------------------------------------------------


class ConditionalCheckFailedException(Exception):
    pass


class FakeTable:
    """Enough of a boto3 Table for the three functions' expressions, no more."""

    def __init__(self, key: str) -> None:
        self.key, self.items = key, {}

    def get_item(self, Key):
        item = self.items.get(Key[self.key])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None, **_):
        if (
            ConditionExpression == f"attribute_not_exists({self.key})"
            and Item[self.key] in self.items
        ):
            raise ConditionalCheckFailedException()
        self.items[Item[self.key]] = dict(Item)

    def check(self, Key, ConditionExpression, names, values) -> bool:
        item = self.items.get(Key[self.key])
        if ConditionExpression is None:
            return True
        if ConditionExpression == "#s = :from":
            return item is not None and item.get(names["#s"]) == values[":from"]
        if ConditionExpression == "available >= :q":
            return item is not None and item.get("available", 0) >= values[":q"]
        raise NotImplementedError(ConditionExpression)

    def apply(self, Key, UpdateExpression, names, values) -> None:
        item = self.items.setdefault(Key[self.key], {self.key: Key[self.key]})
        for clause in UpdateExpression.removeprefix("SET ").split(", "):
            target, expr = (s.strip() for s in clause.split("=", 1))
            target = names.get(target, target)
            if " - " in expr:
                left, right = expr.split(" - ")
                item[target] = item.get(names.get(left, left), 0) - values[right]
            else:
                item[target] = values[expr]

    def update_item(
        self,
        Key,
        UpdateExpression,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
    ):
        names, values = ExpressionAttributeNames or {}, ExpressionAttributeValues or {}
        if not self.check(Key, ConditionExpression, names, values):
            raise ConditionalCheckFailedException()
        self.apply(Key, UpdateExpression, names, values)


def make_transact(tables: dict[str, FakeTable]):
    """All-or-nothing over the fake tables, reporting per-item reasons like DynamoDB."""

    def transact(items: list[dict]) -> None:
        reasons: list[str | None] = []
        for item in items:
            u = item["Update"]
            ok = tables[u["TableName"]].check(
                u["Key"],
                u.get("ConditionExpression"),
                u.get("ExpressionAttributeNames", {}),
                u.get("ExpressionAttributeValues", {}),
            )
            reasons.append(None if ok else "ConditionalCheckFailed")
        if any(reasons):
            raise fulfil.TransactionCanceled(reasons)
        for item in items:
            u = item["Update"]
            tables[u["TableName"]].apply(
                u["Key"],
                u["UpdateExpression"],
                u.get("ExpressionAttributeNames", {}),
                u.get("ExpressionAttributeValues", {}),
            )

    return transact


class Crashed(Exception):
    """The function died after the write. Lambda retries the whole batch."""


class CrashingTable:
    """Wraps a FakeTable so writes sometimes succeed and then the function dies."""

    def __init__(self, inner: FakeTable, rng: random.Random, p: float) -> None:
        self.inner, self.rng, self.p = inner, rng, p

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def update_item(self, **kw):
        self.inner.update_item(**kw)
        if self.rng.random() < self.p:
            raise Crashed("died after update_item")


def crashing_transact(transact, rng: random.Random, p: float):
    def wrapped(items):
        transact(items)
        if rng.random() < p:
            raise Crashed("died after the transaction")

    return wrapped


class FakeProvider:
    """A payment provider with server-side idempotency keys and lost responses."""

    def __init__(self, rng: random.Random, p_lost: float) -> None:
        self.rng, self.p_lost = rng, p_lost
        self.by_key: dict[str, dict] = {}
        self.charges: list[dict] = []  # every charge that actually happened

    def charge(self, amount_cents: int, idempotency_key: str | None = None) -> dict:
        if idempotency_key is not None and idempotency_key in self.by_key:
            return self.by_key[idempotency_key]
        result = {
            "charge_id": f"ch_{len(self.charges):06d}",
            "status": "declined" if amount_cents % 100 == 99 else "approved",
            "amount_cents": amount_cents,
            "key": idempotency_key,
        }
        self.charges.append(result)
        if idempotency_key is not None:
            self.by_key[idempotency_key] = result
        if self.rng.random() < self.p_lost:
            raise charge.ProviderUnavailable("gateway timeout after the charge was made")
        return result


@dataclass
class FakeQueue:
    """SQS standard: at-least-once, a redrive policy, and a dead-letter queue."""

    name: str
    rng: random.Random
    p_duplicate: float
    pending: list[dict] = field(default_factory=list)
    dlq: list[dict] = field(default_factory=list)
    receives: int = 0

    def send(self, body: str) -> None:
        self.pending.append({"messageId": uuid.uuid4().hex, "body": body, "n": 0})

    def take_batch(self) -> list[dict]:
        batch, self.pending = self.pending[:BATCH_SIZE], self.pending[BATCH_SIZE:]
        for m in batch:
            m["n"] += 1
        self.receives += 1
        return batch

    def settle(self, batch: list[dict], failed_ids: set[str], crashed: bool) -> None:
        for m in batch:
            if crashed or m["messageId"] in failed_ids:
                if m["n"] >= MAX_RECEIVE_COUNT:
                    self.dlq.append(m)
                else:
                    self.pending.append(m)  # visibility timeout expired: back it comes
            elif self.rng.random() < self.p_duplicate:
                self.pending.append(m)  # delivered twice anyway (standard queue)


class FakeBus:
    """EventBridge: rules by detail-type, each delivering to queues or a list."""

    def __init__(self) -> None:
        self.rules: dict[str, list[Any]] = {}
        self.log: Counter = Counter()

    def rule(self, detail_type: str, target: Any) -> None:
        self.rules.setdefault(detail_type, []).append(target)

    def publish(self, entry: dict) -> None:
        self.log[entry["DetailType"]] += 1
        event = {
            "version": "0",
            "id": uuid.uuid4().hex,
            "detail-type": entry["DetailType"],
            "source": SOURCE,
            "detail": json.loads(entry["Detail"]),
        }
        for target in self.rules.get(entry["DetailType"], []):
            if isinstance(target, FakeQueue):
                target.send(json.dumps(event))
            else:
                target.append(event)


def record(m: dict) -> dict:
    return {"messageId": m["messageId"], "body": m["body"], "attributes": {}}


def run_consumer(queue: FakeQueue, handler) -> int:
    """Drain a queue the way Lambda's poller would: batches until empty."""
    invocations = 0
    while queue.pending:
        batch = queue.take_batch()
        invocations += 1
        try:
            with contextlib.redirect_stdout(io.StringIO()):  # the functions' log lines
                out = handler([record(m) for m in batch])
        except Crashed:
            queue.settle(batch, set(), crashed=True)
            continue
        queue.settle(batch, {f["itemIdentifier"] for f in out["batchItemFailures"]}, False)
    return invocations


# --- Naive versions: the same steps with the guards removed ----------------------


def naive_accept(body: dict, table: FakeTable, publish) -> dict:
    order = dict(body, order_id=uuid.uuid4().hex, status="placed")  # server-made id
    order["total_cents"] = sum(ln["qty"] * ln["unit_cents"] for ln in order["lines"])
    table.put_item(Item=order)
    publish(event_entry("OrderPlaced", {"order_id": order["order_id"]}))
    return {"statusCode": 201}


def naive_charge(
    records, table: FakeTable, provider: FakeProvider, publish, charged: Counter
) -> dict:
    failed = []
    for r in records:
        detail = json.loads(r["body"])["detail"]
        order = table.get_item(Key={"order_id": detail["order_id"]}).get("Item")
        if order is None:
            failed.append(r["messageId"])
            continue
        try:
            result = provider.charge(int(order["total_cents"]))  # no idempotency key
        except charge.ProviderUnavailable:
            charged[order["order_id"]] += 1  # the charge happened; the reply was lost
            failed.append(r["messageId"])
            continue
        charged[order["order_id"]] += 1
        to = "paid" if result["status"] == "approved" else "payment_failed"
        table.update_item(  # unconditional
            Key={"order_id": order["order_id"]},
            UpdateExpression="SET #s = :to",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":to": to},
        )
        if to == "paid":
            publish(event_entry("OrderPaid", {"order_id": order["order_id"]}))
    return {"batchItemFailures": [{"itemIdentifier": i} for i in failed]}


def naive_fulfil(records, orders: FakeTable, stock: FakeTable, publish) -> dict:
    for r in records:
        detail = json.loads(r["body"])["detail"]
        order = orders.get_item(Key={"order_id": detail["order_id"]})["Item"]
        for sku, qty in fulfil.merged_lines(order["lines"]).items():
            stock.update_item(  # no condition on the status, none on the shelf
                Key={"sku": sku},
                UpdateExpression="SET available = available - :q",
                ExpressionAttributeValues={":q": qty},
            )
        orders.update_item(
            Key={"order_id": order["order_id"]},
            UpdateExpression="SET #s = :to",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":to": "reserved"},
        )
        publish(event_entry("OrderReserved", {"order_id": order["order_id"]}))
    return {"batchItemFailures": []}


# --- The scenario -----------------------------------------------------------------

SKUS = {"FEED-25KG": 50_000, "SHT31-NODE": 500, "DRIP-100M": 2_000, "GLOVES-L": 10_000}


def make_orders(rng: random.Random, n: int) -> list[dict]:
    orders = []
    for i in range(n):
        lines = [
            {"sku": sku, "qty": rng.randint(1, 20), "unit_cents": price}
            for sku, price in rng.sample(sorted(SKUS.items()), rng.randint(1, 3))
        ]
        if i % 25 == 24:  # every 25th order is declined by the provider (ends in 99)
            lines[0]["unit_cents"], lines[0]["qty"] = 99, 1
            lines = lines[:1]
        order_id = str(uuid.UUID(int=rng.getrandbits(128)))
        orders.append({"order_id": order_id, "farm": f"farm-{i % 40:03d}", "lines": lines})
    return orders


def run(n_orders: int, seed: int, guarded: bool) -> dict[str, Any]:
    rng = random.Random(seed)
    orders = make_orders(random.Random(seed), n_orders)
    tables = {ORDERS_TABLE: FakeTable("order_id"), STOCK_TABLE: FakeTable("sku")}
    shelf = {sku: 100_000 for sku in SKUS}
    shelf["SHT31-NODE"] = 2_400  # scarce: the last orders for it must come back as backordered
    for sku, qty in shelf.items():
        tables[STOCK_TABLE].items[sku] = {"sku": sku, "available": qty}
    provider = FakeProvider(rng, P_PROVIDER_LOST_RESPONSE)
    bus = FakeBus()
    payment = FakeQueue("payment", rng, P_DUPLICATE_DELIVERY)
    fulfilment = FakeQueue("fulfilment", rng, P_DUPLICATE_DELIVERY)
    staff: list[dict] = []
    bus.rule("OrderPlaced", payment)
    bus.rule("OrderPaid", fulfilment)
    for dt in ("PaymentFailed", "OrderBackordered"):
        bus.rule(dt, staff)

    orders_table = CrashingTable(tables[ORDERS_TABLE], rng, P_CRASH_AFTER_WRITE)
    naive_charged: Counter = Counter()
    transact = crashing_transact(make_transact(tables), rng, P_CRASH_AFTER_WRITE)

    # 1. Clients post; a lost reply means the same order is posted again.
    posts, accepted = 0, 0
    for body in orders:
        while True:
            posts += 1
            if guarded:
                resp = accept.accept(body, tables[ORDERS_TABLE], bus.publish, now=1_700_000_000)
            else:
                resp = naive_accept(body, tables[ORDERS_TABLE], bus.publish)
            accepted += resp["statusCode"] == 201
            if rng.random() >= P_CLIENT_LOST_REPLY:
                break
    for _ in range(POISON_MESSAGES):
        payment.send(json.dumps({"detail-type": "OrderPlaced", "detail": {"order_id": "lost"}}))

    # 2. The consumers drain their queues; anything they announce is routed on.
    def charge_handler(records):
        if guarded:
            return charge.charge_batch(records, orders_table, provider, bus.publish)
        return naive_charge(records, orders_table, provider, bus.publish, naive_charged)

    def fulfil_handler(records):
        if guarded:
            return fulfil.fulfil_batch(
                records, orders_table, transact, bus.publish, (ORDERS_TABLE, STOCK_TABLE)
            )
        return naive_fulfil(records, tables[ORDERS_TABLE], tables[STOCK_TABLE], bus.publish)

    invocations = 0
    while payment.pending or fulfilment.pending:
        invocations += run_consumer(payment, charge_handler)
        invocations += run_consumer(fulfilment, fulfil_handler)

    # 3. Count what happened.
    stored = list(tables[ORDERS_TABLE].items.values())
    status = Counter(o["status"] for o in stored)
    charges_per_order = Counter(c["key"] for c in provider.charges) if guarded else naive_charged
    approved = [c for c in provider.charges if c["status"] == "approved"]
    intended_approved = sum(
        ln["qty"] * ln["unit_cents"]
        for o in orders
        for ln in o["lines"]
        if sum(x["qty"] * x["unit_cents"] for x in o["lines"]) % 100 != 99
    )
    reserved_units = {
        sku: sum(
            fulfil.merged_lines(o["lines"]).get(sku, 0) for o in stored if o["status"] == "reserved"
        )
        for sku in SKUS
    }
    taken = {sku: shelf[sku] - tables[STOCK_TABLE].items[sku]["available"] for sku in SKUS}
    unique_staff = {e["detail"]["order_id"] for e in staff}
    return {
        "guarded": guarded,
        "orders intended": n_orders,
        "POST requests": posts,
        "orders stored": len(stored),
        "accepted (201)": accepted,
        "provider charges": len(provider.charges),
        "orders charged twice or more": sum(1 for v in charges_per_order.values() if v > 1),
        "cents charged vs intended (approved orders)": (
            f"{sum(c['amount_cents'] for c in approved):,} / {intended_approved:,}"
        ),
        "status counts": dict(sorted(status.items())),
        "shelf taken vs reserved": {sku: f"{taken[sku]} / {reserved_units[sku]}" for sku in SKUS},
        "oversold SKUs": [sku for sku in SKUS if tables[STOCK_TABLE].items[sku]["available"] < 0],
        "staff notices (unique orders)": f"{len(staff)} ({len(unique_staff)})",
        "events published": dict(sorted(bus.log.items())),
        "consumer invocations": invocations,
        "payment DLQ": len(payment.dlq),
        "fulfilment DLQ": len(fulfilment.dlq),
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--orders", type=int, default=500)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    guarded, naive = run(args.orders, args.seed, True), run(args.orders, args.seed, False)
    print(
        f"{args.orders} orders, seed {args.seed}; lost replies {P_CLIENT_LOST_REPLY:.0%}, "
        f"duplicate deliveries {P_DUPLICATE_DELIVERY:.0%}, lost provider responses "
        f"{P_PROVIDER_LOST_RESPONSE:.0%}, crashes after a write {P_CRASH_AFTER_WRITE:.0%}, "
        f"{POISON_MESSAGES} poison messages. In-memory fakes; nothing here is measured."
    )
    print()
    print("| What happened | guarded (this chapter) | naive (guards removed) |")
    print("|---|---:|---:|")
    for key in guarded:
        if key == "guarded":
            continue
        print(f"| {key} | {guarded[key]} | {naive[key]} |")


if __name__ == "__main__":
    main()
