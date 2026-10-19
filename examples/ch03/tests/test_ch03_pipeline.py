"""The pipeline end to end on a synthetic archive in a temporary directory."""

import gzip
import json
from datetime import date
from decimal import Decimal

import numpy as np
import pytest

import cli
import features
import model as model_mod
from archive import day_range, iter_readings, parse_line
from storage import Store
from synth_archive import write_synthetic_archive

END = date(2026, 10, 18)


@pytest.fixture(scope="module")
def archive(tmp_path_factory) -> Store:
    store = Store(str(tmp_path_factory.mktemp("archive")))
    counts = write_synthetic_archive(store, devices=10, days=21, end=END)
    assert counts["lines"] == 10 * 1440 * 21
    return store


def test_archive_has_chapter_2_shape(archive: Store):
    keys = list(archive.list("raw/dt=2026-10-18/"))
    assert len(keys) == 4 and all(k.endswith(".gz") for k in keys)
    first = gzip.decompress(archive.get(keys[0])).decode().splitlines()[0]
    obj = json.loads(first)
    assert set(obj) == {"temp", "hum", "ts", "device", "received"}
    assert obj["device"] == "field-1" and obj["received"] > obj["ts"] * 1000


def test_parse_line_skips_malformed_lines_and_counts_them(archive: Store):
    assert parse_line('{"temp": "warm", "hum": 1, "ts": 1, "device": "x"}') is None
    assert parse_line("not json") is None
    stats: dict[str, int] = {}
    n = sum(1 for _ in iter_readings(archive, [END], stats))
    assert n == stats["lines"] == 10 * 1440 and stats["skipped"] == 0


def test_hourly_table_and_examples(archive: Store):
    table = features.hourly_table(iter_readings(archive, day_range(END, 3)))
    hours = table["field-1"]
    assert len(hours) == 72 and all(h.count == 60 for h in hours.values())
    ex = features.build_examples(table)
    # 72 hours: 24 lags before, 24 target hours after -> 72 - 24 - 24 + 1 windows... per device
    assert len(ex) == 10 * (72 - features.LAGS - features.HORIZON + 1)
    assert ex.X.shape == (len(ex), features.N_FEATURES)
    assert np.all(np.isfinite(ex.y))
    # Remove one hour in the middle: every window touching it disappears.
    del hours[min(hours) + 36]
    fewer = features.build_examples(table)
    assert len(fewer) < len(ex)


def test_baseline_is_the_highest_lag_and_split_is_by_time(archive: Store):
    table = features.hourly_table(iter_readings(archive, day_range(END, 10)))
    ex = features.build_examples(table)
    assert np.allclose(features.yesterday_baseline(ex.X), ex.X[:, : features.LAGS].max(axis=1))
    train, test = model_mod.time_split(ex, holdout_hours=48)
    assert ex.hours[train].max() < ex.hours[test].min()


def test_train_gate_score_round_trip(archive: Store, tmp_path):
    artifacts = Store(str(tmp_path / "artifacts"))
    result = cli.train(archive, artifacts, "2026-10-19", days=21)
    assert result["devices"] == 10 and result["lines"] == 10 * 1440 * 21
    assert result["holdout_rows"] == 10 * 7 * 24
    assert 0 < result["mae"] < result["baseline_mae"]  # on this synthetic data; see README
    assert result["champion_mae"] is None
    assert artifacts.exists("models/candidate/2026-10-19/model.joblib")

    import gate

    decision = gate.gate(gate.LocalStore(artifacts.root), "2026-10-19")
    assert decision["promoted"] and decision["model_available"]

    class FakeTable:
        items: list[dict] = []

        def put_item(self, Item):  # noqa: N803
            self.items.append(Item)

    table = FakeTable()
    summary = cli.score(archive, artifacts, "2026-10-19", cli._FloatSafeWriter(table))
    assert summary["devices_scored"] == 10
    assert len(table.items) == 10
    assert isinstance(table.items[0]["predicted_max_24h"], Decimal)
    lines = artifacts.get("predictions/dt=2026-10-19/predictions.jsonl").decode().splitlines()
    assert len(lines) == 10
    first = json.loads(lines[0])
    assert first["dt"] == "2026-10-19" and first["as_of"].startswith("2026-10-19T00:00")

    # A second night: the champion is scored on the same hold-out as the candidate.
    again = cli.train(archive, artifacts, "2026-10-19", days=14)
    assert again["champion_mae"] is not None
    assert abs(again["champion_mae"] - result["mae"]) < 1e-6  # same model, same hold-out week


def test_store_s3_branch_with_a_fake_client():
    class FakeS3:
        def __init__(self):
            self.objects = {}

        class exceptions:  # noqa: N801
            class ClientError(Exception):
                pass

        def put_object(self, Bucket, Key, Body):  # noqa: N803
            self.objects[(Bucket, Key)] = Body

        def get_object(self, Bucket, Key):  # noqa: N803
            import io

            return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

        def head_object(self, Bucket, Key):  # noqa: N803
            if (Bucket, Key) not in self.objects:
                raise self.exceptions.ClientError()

        def get_paginator(self, name):
            objects = self.objects

            class P:
                def paginate(self, Bucket, Prefix):  # noqa: N803
                    keys = [k for (b, k) in objects if b == Bucket and k.startswith(Prefix)]
                    yield {"Contents": [{"Key": k} for k in sorted(keys, reverse=True)]}

            return P()

    store = Store("s3://bucket/farm/", client=FakeS3())
    store.put("raw/dt=2026-10-18/a.gz", b"1")
    store.put("raw/dt=2026-10-18/b.gz", b"2")
    assert store.is_s3 and store.prefix == "farm"
    assert store.exists("raw/dt=2026-10-18/a.gz") and not store.exists("nope")
    assert list(store.list("raw/dt=2026-10-18/")) == [
        "raw/dt=2026-10-18/a.gz",
        "raw/dt=2026-10-18/b.gz",
    ]
    assert store.get("raw/dt=2026-10-18/b.gz") == b"2"
