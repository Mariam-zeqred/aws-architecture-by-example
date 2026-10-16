import cost


def test_tiered_pricing_crosses_bands():
    tiers = [[10, 1.0], [20, 0.5], [None, 0.1]]
    assert cost.tiered(5, tiers) == 5.0
    assert cost.tiered(15, tiers) == 10 + 2.5
    assert cost.tiered(30, tiers) == 10 + 5 + 1.0


def test_twenty_devices_cost_a_few_dollars():
    total = sum(cost.monthly_cost(cost.SCENARIOS[0]).values())
    assert 1 < total < 10


def test_cost_per_device_is_cents_at_every_scale():
    for s in cost.SCENARIOS:
        per_device = sum(cost.monthly_cost(s).values()) / s.devices
        assert 0.01 < per_device < 0.20


def test_dynamodb_writes_dominate_at_scale():
    items = cost.monthly_cost(cost.SCENARIOS[2])
    writes = items["DynamoDB writes (on-demand)"]
    assert writes == max(items.values())
    assert writes > 0.4 * sum(items.values())


def test_basic_ingest_removes_the_messaging_line():
    s = cost.SCENARIOS[2]
    with_broker = cost.replace(s, basic_ingest=False)
    assert cost.monthly_cost(s)["IoT Core messaging"] == 0
    assert cost.monthly_cost(with_broker)["IoT Core messaging"] > 3_000


def test_batching_and_provisioned_capacity_cut_the_bill_by_more_than_half():
    base = sum(cost.monthly_cost(cost.SCENARIOS[2]).values())
    tuned = sum(cost.monthly_cost(cost.TUNED[1]).values())
    assert tuned < 0.3 * base


def test_per_device_alarms_would_exceed_the_whole_design_at_scale():
    s = cost.SCENARIOS[2]
    assert cost.per_device_alarms_cost(s.devices) > sum(cost.monthly_cost(s).values())


def test_markdown_tables_have_totals():
    md = cost.markdown_table(cost.SCENARIOS)
    assert "| **Total / month** |" in md
    assert md.splitlines()[-1].startswith("| per device / month")
    assert "one alarm + one metric per device" in cost.alarms_table(cost.SCENARIOS)
