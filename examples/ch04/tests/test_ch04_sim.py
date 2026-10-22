"""The simulator: the guarded pipeline is exact, the naive one is not, the DLQ fills as designed."""

import sim


def test_guarded_pipeline_charges_each_order_exactly_once():
    r = sim.run(500, seed=3, guarded=True)
    assert r["orders stored"] == 500 == r["accepted (201)"]
    assert r["POST requests"] > 500  # some clients retried
    assert r["provider charges"] == 500
    assert r["orders charged twice or more"] == 0
    charged, intended = r["cents charged vs intended (approved orders)"].split(" / ")
    assert charged == intended
    assert r["oversold SKUs"] == []
    assert all(a == b for a, b in (v.split(" / ") for v in r["shelf taken vs reserved"].values()))
    assert r["payment DLQ"] == sim.POISON_MESSAGES and r["fulfilment DLQ"] == 0
    assert set(r["status counts"]) <= {"reserved", "backordered", "payment_failed"}


def test_naive_pipeline_double_charges_and_oversells():
    r = sim.run(500, seed=3, guarded=False)
    assert r["orders stored"] > 500
    assert r["orders charged twice or more"] > 0
    charged, intended = r["cents charged vs intended (approved orders)"].split(" / ")
    assert int(charged.replace(",", "")) > int(intended.replace(",", ""))
    assert r["oversold SKUs"]  # the scarce SKU went negative
    taken_vs_reserved = [v.split(" / ") for v in r["shelf taken vs reserved"].values()]
    assert any(int(a) > int(b) for a, b in taken_vs_reserved)  # stock left the shelf for no order


def test_poison_message_is_dead_lettered_after_max_receive_count():
    q = sim.FakeQueue("q", sim.random.Random(0), p_duplicate=0.0)
    q.send("not json")
    calls = []

    def handler(records):
        calls.append(len(records))
        return {"batchItemFailures": [{"itemIdentifier": r["messageId"]} for r in records]}

    assert sim.run_consumer(q, handler) == sim.MAX_RECEIVE_COUNT
    assert len(q.dlq) == 1 and q.dlq[0]["n"] == sim.MAX_RECEIVE_COUNT


def test_crash_fails_the_whole_batch_and_redelivers_everything():
    q = sim.FakeQueue("q", sim.random.Random(0), p_duplicate=0.0)
    for i in range(4):
        q.send(str(i))
    seen = []

    def handler(records):
        seen.append([r["body"] for r in records])
        if len(seen) == 1:
            raise sim.Crashed("boom")
        return {"batchItemFailures": []}

    sim.run_consumer(q, handler)
    assert seen[0] == ["0", "1", "2", "3"] and sorted(seen[1]) == ["0", "1", "2", "3"]
    assert not q.dlq


def test_main_prints_the_table(capsys):
    sim.main(["--orders", "40", "--seed", "2"])
    out = capsys.readouterr().out
    assert "| orders charged twice or more | 0 |" in out
    assert "naive (guards removed)" in out
