import cost


def test_small_shop_pays_mostly_for_listening():
    c = cost.monthly_cost(cost.SCENARIOS[0])
    total = sum(c.values())
    assert 0.3 < total < 3
    fixed = c["5 alarms + logs"] + c["SQS requests (incl. empty receives)"]
    assert fixed / total > 0.8


def test_per_order_cost_falls_then_flattens():
    per = [sum(cost.monthly_cost(s).values()) / s.orders for s in cost.SCENARIOS]
    assert per[0] > 10 * per[1]
    assert per[1] < 0.0001 and abs(per[1] - per[2]) < 0.00002


def test_standard_workflows_cost_two_orders_of_magnitude_more_at_scale():
    alts = cost.plumbing_alternatives(cost.SCENARIOS[2])
    this = alts["EventBridge + SQS standard (this chapter)"]
    assert alts["Step Functions Standard, one execution per order"] > 40 * this
    assert alts["EventBridge + SQS FIFO (ordered per order id)"] > this
    assert alts["Step Functions Express, one execution per order"] < this


def test_tables_have_totals():
    md = cost.markdown_table(cost.SCENARIOS)
    assert "| **Total / month** |" in md and "| per order |" in md
    assert "Step Functions" in cost.alternatives_table(cost.SCENARIOS)
