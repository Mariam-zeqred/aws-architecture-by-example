"""The stale-device scan against a paginating fake table."""

import stale_scan


class PagedTable:
    def __init__(self, items, page_size=2):
        self.items, self.page_size, self.scans = items, page_size, 0

    def scan(self, ProjectionExpression=None, ExclusiveStartKey=None):
        self.scans += 1
        start = ExclusiveStartKey or 0
        page = self.items[start : start + self.page_size]
        out = {"Items": page}
        if start + self.page_size < len(self.items):
            out["LastEvaluatedKey"] = start + self.page_size
        return out


class FakeCloudWatch:
    def __init__(self):
        self.calls = []

    def put_metric_data(self, **kwargs):
        self.calls.append(kwargs)


NOW = 1_760_000_000


def test_counts_only_devices_past_the_cutoff_and_pages_through():
    table = PagedTable(
        [
            {"device": "field-1", "received": (NOW - 30) * 1000},
            {"device": "field-2", "received": (NOW - 599) * 1000},  # just inside 10 min
            {"device": "field-3", "received": (NOW - 601) * 1000},  # just outside
            {"device": "field-4", "received": (NOW - 86_400) * 1000},
            {"device": "field-5", "received": (NOW - 5) * 1000},
        ]
    )
    stale = stale_scan.find_stale(table, NOW, 600)
    assert [d["device"] for d in stale] == ["field-3", "field-4"]
    assert stale[1]["silent_for_s"] == 86_400
    assert table.scans == 3  # 5 items, 2 per page


def test_publishes_one_metric_with_no_per_device_dimension():
    table = PagedTable([{"device": "field-1", "received": (NOW - 7_200) * 1000}])
    cw = FakeCloudWatch()
    result = stale_scan.run(table, cw, NOW, 600, "FarmTelemetry")
    assert result == {"stale": 1, "devices": ["field-1"]}
    (call,) = cw.calls
    assert call["Namespace"] == "FarmTelemetry"
    (datum,) = call["MetricData"]
    assert datum == {"MetricName": "StaleDevices", "Value": 1, "Unit": "Count"}
    assert "Dimensions" not in datum
