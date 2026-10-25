"""The drill table's shape and the conflict drill's numbers."""

import drill
from drill import DISASTERS, STRATEGIES, FakeGlobalTable, Params, reserve_from_regions


def minutes(text: str) -> float:
    """Parse the first duration in a drill cell back to minutes."""
    first = text.split(";")[0].split(",")[0].strip().split(" ")
    value, unit = float(first[0]), first[1]
    return value * {"s": 1 / 60, "min": 1, "h": 60}[unit]


def test_every_cell_is_filled():
    for d in DISASTERS:
        for s in STRATEGIES:
            o = drill.drill(d, s)
            assert o.lost and o.down


def test_region_outage_warm_standby_is_minutes_backup_restore_is_an_hour_one_region_is_the_outage():
    p = Params()
    one = drill.drill(DISASTERS[0], "one region", p)
    br = drill.drill(DISASTERS[0], "backup & restore", p)
    warm = drill.drill(DISASTERS[0], "warm standby", p)
    assert minutes(one.down) == p.outage_hours * 60
    assert 60 <= minutes(br.down) <= 90 and "24 h" in br.lost
    assert minutes(warm.down) == (p.health_check_s + p.dns_ttl_s) / 60
    assert "1 s" in warm.lost


def test_deleting_items_is_the_same_recovery_under_every_strategy():
    cells = {drill.drill(DISASTERS[1], s).down for s in STRATEGIES}
    assert len(cells) == 1  # replication does not help; PITR does, everywhere
    assert "reached the other region" in drill.drill(DISASTERS[1], "warm standby").note


def test_corruption_is_never_downtime_and_never_fixed_by_a_replica():
    for s in STRATEGIES:
        o = drill.drill(DISASTERS[2], s)
        assert o.down == "none" and "wrong" in o.lost


def test_leaked_credentials_lose_everything_in_one_region_only():
    assert drill.drill(DISASTERS[3], "one region").lost == "everything"
    assert "24 h" in drill.drill(DISASTERS[3], "backup & restore").lost


def test_restore_time_grows_with_the_table_and_table_appears_in_the_output():
    small, big = Params(table_gb=0.02), Params(table_gb=100)
    assert drill.restore_minutes(big) > drill.restore_minutes(small) + 400
    md = drill.drill_table()
    assert md.count("\n") == len(DISASTERS) + 1 and "warm standby" in md


def test_last_writer_wins_converges_and_counts_the_conflict():
    t = FakeGlobalTable(["a", "b"], lag_s=1.0)
    t.put("a", "k", {"v": 1}, now=0.0)
    t.put("b", "k", {"v": 2}, now=0.5)
    t.settle(until=10.0)
    assert t.read("a", "k") == t.read("b", "k") == {"v": 2}
    assert t.conflicts == 2  # seen once from each side


def test_a_counter_is_safe_in_one_region_and_not_across_two():
    one = reserve_from_regions([("us-east-1", 6), ("us-east-1", 6)])
    assert one.reserved == 6 and one.backordered == 1 and one.forgotten == 0 and one.oversold == 0
    both = reserve_from_regions([("us-east-1", 6), ("eu-west-1", 6)])
    assert both.reserved == 12 and both.backordered == 0
    assert both.stock_end == {"us-east-1": 4, "eu-west-1": 4}
    assert both.forgotten == 6 and both.oversold == 2


def test_conflict_report_has_the_two_columns():
    md = drill.conflict_report()
    assert "| units promised beyond the shelf | 2 | 0 |" in md
