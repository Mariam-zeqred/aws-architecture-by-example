import cost


def test_small_scenario_is_under_fifteen_dollars():
    total = sum(cost.monthly_cost(cost.SCENARIOS[0]).values())
    assert 1 < total < 15


def test_transfer_dominates_when_busy():
    items = cost.monthly_cost(cost.SCENARIOS[1])
    assert items["CloudFront data transfer"] > 0.5 * sum(items.values())


def test_cost_scales_roughly_linearly_with_visits():
    small, busy = (sum(cost.monthly_cost(s).values()) for s in cost.SCENARIOS)
    ratio = cost.SCENARIOS[1].visits_per_day / cost.SCENARIOS[0].visits_per_day
    assert busy < small * ratio  # fixed items (zone, device writes) do not scale


def test_markdown_table_has_a_total_row():
    md = cost.markdown_table(cost.SCENARIOS)
    assert md.splitlines()[-1].startswith("| **Total / month**")
