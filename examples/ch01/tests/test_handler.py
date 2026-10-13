"""The API logic, run against an in-memory table (no AWS, no network)."""

import json
from decimal import Decimal

import pytest

import handler


class FakeTable:
    """Just enough of boto3's Table for the handler: put_item and query."""

    def __init__(self):
        self.items: list[dict] = []

    def put_item(self, Item):
        self.items.append(Item)

    def query(self, KeyConditionExpression, ScanIndexForward, Limit):
        device = KeyConditionExpression._values[1]  # Key("device").eq(x) -> x
        rows = sorted(
            (i for i in self.items if i["device"] == device),
            key=lambda i: i["ts"],
            reverse=not ScanIndexForward,
        )
        return {"Items": rows[:Limit]}


def event(method, path, body=None, params=None, query=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "pathParameters": params or {},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
    }


def test_health():
    res = handler.handle(event("GET", "/api/health"), table=FakeTable())
    assert res["statusCode"] == 200
    assert json.loads(res["body"]) == {"ok": True}


def test_post_then_get_newest_first():
    table = FakeTable()
    for ts, temp in [(100, 21.5), (200, 22.0), (300, 22.4)]:
        body = {"device": "field-1", "temp": temp, "hum": 55, "ts": ts}
        res = handler.handle(event("POST", "/api/readings", body), table=table)
        assert res["statusCode"] == 201, res["body"]
    res = handler.handle(
        event("GET", "/api/readings/field-1", params={"device": "field-1"}, query={"limit": "2"}),
        table=table,
    )
    data = json.loads(res["body"])
    assert [r["ts"] for r in data["readings"]] == [300, 200]
    assert data["readings"][0]["temp"] == 22.4  # Decimal rendered as a plain number


def test_ttl_is_set_30_days_after_reading():
    table = FakeTable()
    handler.handle(
        event("POST", "/api/readings", {"device": "a1", "temp": 20, "hum": 50, "ts": 1000}),
        table=table,
    )
    assert table.items[0]["expires"] == 1000 + 30 * 86400
    assert isinstance(table.items[0]["temp"], Decimal)


@pytest.mark.parametrize(
    "body, message",
    [
        ({"temp": 20, "hum": 50}, "device must be"),
        ({"device": "Field 1", "temp": 20, "hum": 50}, "device must be"),
        ({"device": "f1", "hum": 50}, "temp is required"),
        ({"device": "f1", "temp": "warm", "hum": 50}, "temp must be a number"),
        ({"device": "f1", "temp": 120, "hum": 50}, "temp must be between"),
        ({"device": "f1", "temp": 20, "hum": 101}, "hum must be between"),
    ],
)
def test_post_rejects_bad_input(body, message):
    res = handler.handle(event("POST", "/api/readings", body), table=FakeTable())
    assert res["statusCode"] == 400
    assert message in json.loads(res["body"])["error"]


def test_bad_json_and_bad_limit_and_unknown_route():
    table = FakeTable()
    bad = event("POST", "/api/readings")
    bad["body"] = "{not json"
    assert handler.handle(bad, table=table)["statusCode"] == 400
    res = handler.handle(
        event("GET", "/api/readings/f1", params={"device": "f1"}, query={"limit": "ten"}),
        table=table,
    )
    assert res["statusCode"] == 400
    assert handler.handle(event("GET", "/api/nothing"), table=table)["statusCode"] == 404


def test_limit_is_clamped():
    table = FakeTable()
    for ts in range(600):
        table.put_item({"device": "f1", "ts": ts, "temp": Decimal(1), "hum": Decimal(1)})
    res = handler.handle(
        event("GET", "/api/readings/f1", params={"device": "f1"}, query={"limit": "9999"}),
        table=table,
    )
    assert len(json.loads(res["body"])["readings"]) == handler.MAX_LIMIT
