import cost


def test_the_standby_costs_the_cooperative_a_few_dollars_mostly_for_watching_the_copies():
    lines = cost.warm_standby_lines(cost.SCENARIOS[0])
    total = sum(lines.values())
    assert 2 < total < 8
    watching = next(v for k, v in lines.items() if "alarms" in k)
    assert watching / total > 0.5
    assert lines["Standby API + function, idle"] == 0.0


def test_at_scale_the_archive_replica_dominates():
    lines = cost.warm_standby_lines(cost.SCENARIOS[1])
    s3 = next(v for k, v in lines.items() if k.startswith("S3 replicas"))
    assert s3 / sum(lines.values()) > 0.6


def test_backup_and_restore_is_cheaper_and_active_active_is_not():
    for s in cost.SCENARIOS:
        t = cost.strategy_totals(s)
        values = list(t.values())
        assert values[0] == 0.0
        assert values[1] < values[2]
        assert values[2] == values[3]
        assert values[2] - values[1] < 5  # the standby itself is cheap; the archive copy is not


def test_tables_have_totals():
    md = cost.markdown_table(cost.SCENARIOS)
    assert "| **Added / month** |" in md
    assert "active-active" in cost.strategies_table(cost.SCENARIOS)
