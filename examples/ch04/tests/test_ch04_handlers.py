"""The three functions against the simulator's fakes: every guard, one test each."""

import json
import uuid

import pytest

import accept
import charge
import fulfil
import sim
from orders_common import NEXT, Conflict, advance, event_entry

ORDER_ID = "0f3b2b1e-9c1d-4a6e-8f2a-1c2d3e4f5a6b"


def body(**over):
    b = {
        "order_id": ORDER_ID,
        "farm": "farm-007",
        "lines": [{"sku": "FEED-25KG", "qty": 3, "unit_cents": 50_000}],
    }
    b.update(over)
    return b


def sqs_record(detail_type: str, detail: dict, message_id: str | None = None) -> dict:
    event = {"version": "0", "id": uuid.uuid4().hex, "detail-type": detail_type, "detail": detail}
    return {"messageId": message_id or uuid.uuid4().hex, "body": json.dumps(event)}


@pytest.fixture
def world():
    orders, stock = sim.FakeTable("order_id"), sim.FakeTable("sku")
    stock.items["FEED-25KG"] = {"sku": "FEED-25KG", "available": 10}
    published = []
    tables = {sim.ORDERS_TABLE: orders, sim.STOCK_TABLE: stock}
    return orders, stock, published, sim.make_transact(tables)


# --- accept ------------------------------------------------------------------


def test_first_post_is_201_and_announces(world):
    orders, _, published, _ = world
    r = accept.accept(body(), orders, published.append, now=1)
    assert r["statusCode"] == 201
    assert orders.items[ORDER_ID]["status"] == "placed"
    assert orders.items[ORDER_ID]["total_cents"] == 150_000
    assert [e["DetailType"] for e in published] == ["OrderPlaced"]


def test_repeated_post_with_same_id_stores_once_and_announces_again(world):
    orders, _, published, _ = world
    accept.accept(body(), orders, published.append, now=1)
    r = accept.accept(
        body(lines=[{"sku": "GLOVES-L", "qty": 1, "unit_cents": 1}]), orders, published.append
    )
    assert r["statusCode"] == 200 and json.loads(r["body"])["outcome"].startswith("already")
    assert orders.items[ORDER_ID]["total_cents"] == 150_000  # the first body won
    assert [e["DetailType"] for e in published] == ["OrderPlaced", "OrderPlaced"]


def test_repeated_post_after_payment_does_not_re_announce(world):
    orders, _, published, _ = world
    accept.accept(body(), orders, published.append, now=1)
    advance(orders, ORDER_ID, "placed", "paid", charge_id="ch_1")
    r = accept.accept(body(), orders, published.append)
    assert r["statusCode"] == 200 and json.loads(r["body"])["status"] == "paid"
    assert len(published) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"order_id": "not-a-uuid"},
        {"farm": "Farm 7"},
        {"lines": []},
        {"lines": [{"sku": "x", "qty": 1, "unit_cents": 1}]},
        {"lines": [{"sku": "FEED-25KG", "qty": 0, "unit_cents": 1}]},
        {"lines": [{"sku": "FEED-25KG", "qty": True, "unit_cents": 1}]},
        {"lines": [{"sku": "FEED-25KG", "qty": 1000, "unit_cents": 1_000_000}]},
    ],
)
def test_bad_orders_are_400_and_never_stored(world, bad):
    orders, _, published, _ = world
    r = accept.accept(body(**bad), orders, published.append)
    assert r["statusCode"] == 400 and not orders.items and not published


# --- the status machine -------------------------------------------------------


def test_every_transition_is_forward_only_and_conditional(world):
    orders, _, _, _ = world
    orders.items[ORDER_ID] = {"order_id": ORDER_ID, "status": "placed"}
    advance(orders, ORDER_ID, "placed", "paid", charge_id="ch_1")
    with pytest.raises(Conflict):
        advance(orders, ORDER_ID, "placed", "paid", charge_id="ch_2")  # already moved
    with pytest.raises(ValueError):
        advance(orders, ORDER_ID, "paid", "placed")  # not a transition
    assert orders.items[ORDER_ID] == {"order_id": ORDER_ID, "status": "paid", "charge_id": "ch_1"}
    assert all(set(nxt) <= NEXT.keys() for nxt in NEXT.values())


# --- charge --------------------------------------------------------------------


def test_charge_once_then_duplicates_only_re_announce(world):
    orders, _, published, _ = world
    accept.accept(body(), orders, published.append, now=1)
    provider = charge.StubProvider()
    rec = sqs_record("OrderPlaced", {"order_id": ORDER_ID})
    for _ in range(3):
        out = charge.charge_batch([rec], orders, provider, published.append)
        assert out == {"batchItemFailures": []}
    assert len(provider.charges) == 1
    assert orders.items[ORDER_ID]["status"] == "paid"
    assert [e["DetailType"] for e in published] == ["OrderPlaced"] + ["OrderPaid"] * 3


def test_lost_provider_response_is_retried_with_the_same_key(world):
    orders, _, published, _ = world
    accept.accept(body(), orders, published.append, now=1)
    provider = sim.FakeProvider(sim.random.Random(0), p_lost=0.0)
    first = provider.charge  # charge, then "time out" once

    def flaky(amount_cents, idempotency_key=None):
        result = first(amount_cents, idempotency_key)
        if len(provider.charges) == 1 and not getattr(flaky, "done", False):
            flaky.done = True
            raise charge.ProviderUnavailable("timeout")
        return result

    provider.charge = flaky
    rec = sqs_record("OrderPlaced", {"order_id": ORDER_ID}, "m1")
    assert charge.charge_batch([rec], orders, provider, published.append) == {
        "batchItemFailures": [{"itemIdentifier": "m1"}]
    }
    assert orders.items[ORDER_ID]["status"] == "placed"  # nothing written yet
    assert charge.charge_batch([rec], orders, provider, published.append)["batchItemFailures"] == []
    assert len(provider.charges) == 1 and orders.items[ORDER_ID]["status"] == "paid"


def test_declined_charge_moves_to_payment_failed_and_tells_someone(world):
    orders, _, published, _ = world
    accept.accept(
        body(lines=[{"sku": "FEED-25KG", "qty": 1, "unit_cents": 1_099}]),
        orders,
        published.append,
        now=1,
    )
    out = charge.charge_batch(
        [sqs_record("OrderPlaced", {"order_id": ORDER_ID})],
        orders,
        charge.StubProvider(),
        published.append,
    )
    assert out["batchItemFailures"] == []
    assert orders.items[ORDER_ID]["status"] == "payment_failed"
    assert published[-1]["DetailType"] == "PaymentFailed"


def test_poison_records_fail_individually_and_good_ones_pass(world):
    orders, _, published, _ = world
    accept.accept(body(), orders, published.append, now=1)
    batch = [
        sqs_record("OrderPlaced", {"order_id": "no-such-order"}, "bad1"),
        {"messageId": "bad2", "body": "not json"},
        sqs_record("OrderPlaced", {"order_id": ORDER_ID}, "good"),
        sqs_record("OrderPaid", {"order_id": ORDER_ID}, "bad3"),
    ]
    out = charge.charge_batch(batch, orders, charge.StubProvider(), published.append)
    assert {f["itemIdentifier"] for f in out["batchItemFailures"]} == {"bad1", "bad2", "bad3"}
    assert orders.items[ORDER_ID]["status"] == "paid"


# --- fulfil ----------------------------------------------------------------------


def paid_order(orders, published, qty=3):
    accept.accept(
        body(lines=[{"sku": "FEED-25KG", "qty": qty, "unit_cents": 1}]),
        orders,
        published.append,
        now=1,
    )
    advance(orders, ORDER_ID, "placed", "paid", charge_id="ch_1")


def test_reservation_is_one_transaction_and_a_duplicate_takes_nothing(world):
    orders, stock, published, transact = world
    paid_order(orders, published)
    names = (sim.ORDERS_TABLE, sim.STOCK_TABLE)
    rec = sqs_record("OrderPaid", {"order_id": ORDER_ID})
    for _ in range(2):
        assert (
            fulfil.fulfil_batch([rec], orders, transact, published.append, names)[
                "batchItemFailures"
            ]
            == []
        )
    assert stock.items["FEED-25KG"]["available"] == 7
    assert orders.items[ORDER_ID]["status"] == "reserved"
    assert [e["DetailType"] for e in published][-2:] == ["OrderReserved", "OrderReserved"]


def test_short_stock_backorders_the_paid_order_and_takes_nothing(world):
    orders, stock, published, transact = world
    paid_order(orders, published, qty=11)
    out = fulfil.fulfil_batch(
        [sqs_record("OrderPaid", {"order_id": ORDER_ID})],
        orders,
        transact,
        published.append,
        (sim.ORDERS_TABLE, sim.STOCK_TABLE),
    )
    assert out["batchItemFailures"] == []
    assert stock.items["FEED-25KG"]["available"] == 10
    assert orders.items[ORDER_ID]["status"] == "backordered"
    assert orders.items[ORDER_ID]["short_skus"] == ["FEED-25KG"]
    assert published[-1]["DetailType"] == "OrderBackordered"
    assert json.loads(published[-1]["Detail"])["short_skus"] == ["FEED-25KG"]


def test_same_sku_twice_in_an_order_is_merged_for_the_transaction():
    lines = [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}, {"sku": "A", "qty": 5}]
    assert fulfil.merged_lines(lines) == {"A": 7, "B": 1}
    items = fulfil.reservation({"order_id": "o", "lines": lines}, "orders", "stock")
    assert [i["Update"]["TableName"] for i in items] == ["orders", "stock", "stock"]
    assert items[0]["Update"]["ConditionExpression"] == "#s = :from"
    assert items[1]["Update"]["ExpressionAttributeValues"] == {":q": 7}


def test_event_entry_is_what_put_events_wants():
    e = event_entry("OrderPaid", {"order_id": "o"}, bus_name="bus")
    assert e == {
        "Source": "coop.orders",
        "DetailType": "OrderPaid",
        "Detail": '{"order_id": "o"}',
        "EventBusName": "bus",
    }
