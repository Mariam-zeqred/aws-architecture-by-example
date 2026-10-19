import math

import cost


def test_twenty_devices_cost_about_a_dollar():
    total = sum(cost.monthly_cost(cost.SCENARIOS[0]).values())
    assert 0.3 < total < 3


def test_reference_design_does_not_fit_a_night_at_100_000_devices():
    assert cost.train_seconds(cost.SCENARIOS[1]) < 30 * 60
    assert cost.train_seconds(cost.SCENARIOS[2]) > 8 * 3600


def test_tuned_designs_fit_a_night_and_incremental_is_cheapest():
    full, incremental = cost.TUNED
    assert cost.train_seconds(full) < 15 * 60 and cost.train_seconds(incremental) < 15 * 60
    assert cost.monthly_cost(full)["Athena feature aggregation"] > 10
    assert sum(cost.monthly_cost(incremental).values()) < sum(cost.monthly_cost(full).values())
    assert cost.monthly_cost(cost.SCENARIOS[2])["Athena feature aggregation"] == 0


def test_lambda_is_an_option_only_when_the_task_fits_in_15_minutes():
    small = cost.compute_alternatives(cost.SCENARIOS[0])
    big = cost.compute_alternatives(cost.SCENARIOS[2])
    assert not math.isnan(small["Lambda, 4 GB (fits in 15 min)"])
    assert math.isnan(big["Lambda, 4 GB (fits in 15 min)"])
    assert small["Fargate, 1 vCPU / 4 GB (this chapter)"] < small["Fargate, 4 vCPU / 16 GB"]


def test_tables_have_totals_and_wall_times():
    md = cost.markdown_table(cost.SCENARIOS)
    assert "| **Total / month** |" in md and "training task wall time / night" in md
    assert "SageMaker" in cost.alternatives_table(cost.SCENARIOS)
