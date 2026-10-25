"""The handler against a fake table that knows the one condition it uses."""

import json
import uuid

import pytest

import api


class ConditionalCheckFailedException(Exception):
    pass


class FakeTable:
    def __init__(self, fail_reads: bool = False) -> None:
        self.items: dict[str, dict] = {}
        self.fail_reads = fail_reads

    def get_item(self, Key, ConsistentRead=False):
        if self.fail_reads:
            raise RuntimeError("DynamoDB unavailable")
        item = self.items.get(Key["order_id"])
        return {"Item": item} if item is not None else {}

    def put_item(self, Item, ConditionExpression=None):
        exists = Item["order_id"] in self.items
        if ConditionExpression == "attribute_not_exists(order_id)" and exists:
            raise ConditionalCheckFailedException()
        self.items[Item["order_id"]] = dict(Item)


@pytest.fixture
def table():
    t = FakeTable()
    api.set_table(t)
    yield t
    api.set_table(None)


def event(method: str, path: str, body: dict | str | None = None, **path_params):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "pathParameters": path_params,
        "body": json.dumps(body) if isinstance(body, dict) else body,
    }


def order(**overrides):
    return {
        "order_id": str(uuid.uuid4()),
        "farm": "farm-007",
        "lines": [{"sku": "FEED-25KG", "qty": 3, "unit_cents": 50_000}],
        **overrides,
    }


def test_health_reads_the_table_and_names_the_region(table):
    r = api.lambda_handler(event("GET", "/health"))
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["ok"] is True and body["served_by"] == api.REGION and body["role"] == api.ROLE


def test_health_fails_when_the_table_is_unreachable():
    api.set_table(FakeTable(fail_reads=True))
    with pytest.raises(RuntimeError):
        api.lambda_handler(event("GET", "/health"))
    api.set_table(None)


def test_accept_is_a_conditional_put_tagged_with_the_region(table):
    o = order()
    first = api.lambda_handler(event("POST", "/orders", o))
    assert first["statusCode"] == 201
    stored = table.items[o["order_id"]]
    assert stored["status"] == "placed" and stored["accepted_in"] == api.REGION
    assert stored["total_cents"] == 150_000
    again = api.lambda_handler(event("POST", "/orders", o))
    assert again["statusCode"] == 200
    assert json.loads(again["body"])["note"] == "already accepted"
    assert len(table.items) == 1


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "not json",
        {"farm": "f", "lines": []},
        order(order_id="not-a-uuid"),
        order(lines=[{"sku": "X", "qty": 0, "unit_cents": 1}]),
        order(lines=[{"sku": "X", "qty": 1, "unit_cents": -1}]),
    ],
)
def test_bad_orders_are_refused_before_any_write(table, bad):
    r = api.lambda_handler(event("POST", "/orders", bad))
    assert r["statusCode"] == 400 and table.items == {}


def test_get_order_is_404_then_200(table):
    o = order()
    assert api.lambda_handler(event("GET", f"/orders/{o['order_id']}"))["statusCode"] == 404
    api.lambda_handler(event("POST", "/orders", o))
    r = api.lambda_handler(event("GET", f"/orders/{o['order_id']}", order_id=o["order_id"]))
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["order"]["order_id"] == o["order_id"]


def test_unknown_routes_are_404(table):
    assert api.lambda_handler(event("DELETE", "/orders"))["statusCode"] == 404
