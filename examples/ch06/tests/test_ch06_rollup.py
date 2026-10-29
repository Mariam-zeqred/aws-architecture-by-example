"""The five cost models added up, the de-duplication and the levers."""

import pytest

import rollup


def test_every_chapter_contributes_and_the_total_is_their_sum():
    for which in (rollup.TODAY, rollup.SCALE):
        b = rollup.bill(which)
        assert set(b) == {p.title for p in rollup.PARTS}
        assert all(sum(lines.values()) > 0 for lines in b.values())
        assert rollup.total(b) == pytest.approx(sum(sum(v.values()) for v in b.values()))


def test_chapter_one_drops_the_lines_chapters_two_and_five_count():
    part = next(p for p in rollup.PARTS if p.chapter == "ch01")
    lines = rollup.lines_for(part, rollup.scenario("ch01", rollup.TODAY))
    assert "DynamoDB writes" not in lines and "Route 53 hosted zone" not in lines
    assert "DynamoDB reads" in lines and "CloudFront data transfer" in lines
    raw = rollup.MODULES["ch01"].monthly_cost(rollup.scenario("ch01", rollup.TODAY))
    assert all(k in raw for k in part.drop)  # the dropped names still exist in chapter 1


def test_the_whole_system_is_about_a_hundred_dollars_today_and_chapter_two_is_most_of_it():
    b = rollup.bill(rollup.TODAY)
    whole = rollup.total(b)
    assert 80 < whole < 200
    assert sum(b["2 telemetry"].values()) / whole > 0.8
    top = rollup.largest_lines(b, 1)[0]
    assert top[0] == "2 telemetry" and top[1].startswith("DynamoDB writes")


def test_each_chapter_total_matches_what_its_own_cost_module_prints():
    # chapter 5's warm-standby lines are taken whole; chapters 2-4 too
    for ch, fn in (
        ("ch02", "monthly_cost"),
        ("ch03", "monthly_cost"),
        ("ch04", "monthly_cost"),
        ("ch05", "warm_standby_lines"),
    ):
        part = next(p for p in rollup.PARTS if p.chapter == ch)
        s = rollup.scenario(ch, rollup.SCALE)
        assert sum(rollup.lines_for(part, s).values()) == pytest.approx(
            sum(getattr(rollup.MODULES[ch], fn)(s).values())
        )


def test_the_levers_save_money_and_chapter_two_s_is_the_big_one():
    for which in (rollup.TODAY, rollup.SCALE):
        base = rollup.total(rollup.bill(which))
        savings = {
            name: base - rollup.total(rollup.bill(which, o))
            for name, o in rollup.levers(which).items()
        }
        assert all(v > 0 for v in savings.values())
        biggest = max(savings, key=savings.get)
        assert biggest.startswith("chapter 2")
        assert savings[biggest] / base > 0.6


def test_tables_render():
    assert "| **Whole system / month** |" in rollup.chapters_table()
    assert "| Largest lines, cooperative today |" in rollup.largest_table(
        rollup.TODAY, "cooperative today"
    )
    assert "| **all three** |" in rollup.levers_table(rollup.SCALE, "At 100 000 devices")
