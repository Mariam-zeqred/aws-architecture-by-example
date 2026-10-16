"""The ingest function against fake tables and a fake queue, no AWS needed."""

import json
from decimal import Decimal

import pytest

import ingest


class ConditionalCheckFailedException(Exception):
    pass


class FakeTable:
    """Enough of a boto3 Table to exercise put_item with the status condition."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.puts = 0

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        self.puts += 1
        current = self.items.get(Item["device"])
        if ConditionExpression and current is not None:
            if not current["ts"] < ExpressionAttributeValues[":ts"]:
                raise ConditionalCheckFailedException()
        key = Item["device"] if ConditionExpression else f"{Item['device']}#{Item['ts']}"
        self.items[key] = Item


@pytest.fixture
def world():
    status, history, rejects = FakeTable(), FakeTable(), []
    return status, history, rejects


def run(world, message, **kw):
    status, history, rejects = world
    return ingest.ingest(message, status, history, rejects.append, **kw)


RECEIVED_MS = 1_760_000_000_000  # 2025-10-09T08:53:20Z in ms


def good(**over):
    msg = {
        "device": "field-7",
        "temp": 22.456,
        "hum": 55,
        "ts": 1_759_999_990,
        "received": RECEIVED_MS,
    }
    msg.update(over)
    return msg


def test_valid_reading_writes_history_and_status(world):
    status, history, rejects = world
    result = run(world, good())
    assert result["outcome"] == "written"
    assert rejects == []
    item = history.items["field-7#1759999990"]
    assert item["temp"] == Decimal("22.46") and item["hum"] == Decimal("55.00")
    assert item["expires"] == 1_759_999_990 + 30 * 86_400
    assert "clock" not in item  # the device clock was plausible, so it was kept
    assert status.items["field-7"]["ts"] == 1_759_999_990


def test_older_reading_does_not_roll_status_back(world):
    status, history, _ = world
    run(world, good(ts=1_759_999_990))
    result = run(world, good(ts=1_759_999_900, received=RECEIVED_MS - 90_000, temp=30))
    assert result["outcome"].startswith("written, status kept")
    assert status.items["field-7"]["temp"] == Decimal("22.46")  # unchanged
    assert len(history.items) == 2  # but the history has both


@pytest.mark.parametrize(
    "message, reason",
    [
        (good(hum="55%"), "hum must be a number"),
        (good(temp=120), "temp out of sensor range"),
        (good(device="Field 7"), "device id missing or malformed"),
        ({k: v for k, v in good().items() if k != "received"}, "received (broker timestamp)"),
        (good(hum=True), "hum must be a number"),
    ],
)
def test_bad_readings_go_to_the_queue_and_nowhere_else(world, message, reason):
    status, history, rejects = world
    result = run(world, message)
    assert result["outcome"] == "rejected"
    assert status.puts == 0 and history.puts == 0
    assert len(rejects) == 1
    body = json.loads(rejects[0])
    assert body["reason"].startswith(reason)
    assert body["message"] == message


def test_wrong_device_clock_falls_back_to_broker_time(world):
    _, history, _ = world
    far_future = good(ts=RECEIVED_MS // 1000 + 3_600)  # an hour ahead of the broker
    run(world, far_future)
    (item,) = history.items.values()
    assert item["ts"] == RECEIVED_MS // 1000 and item["clock"] == "server"


def test_missing_device_clock_is_allowed(world):
    _, history, _ = world
    msg = good()
    del msg["ts"]
    run(world, msg)
    (item,) = history.items.values()
    assert item["ts"] == RECEIVED_MS // 1000 and item["clock"] == "server"


def test_history_days_is_configurable(world):
    _, history, _ = world
    run(world, good(), history_days=7)
    (item,) = history.items.values()
    assert item["expires"] - item["ts"] == 7 * 86_400
