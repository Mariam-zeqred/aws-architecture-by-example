"""Synthesise chapters 1 to 5 in their production flavour and review them.

The slow test of the chapter (about twenty seconds): it is the state the
chapter's section 4 tables describe, so the counts here are the chapter's.
"""

from pathlib import Path

import pytest

import review


@pytest.fixture(scope="module")
def templates(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("templates")
    review.synth(root)
    return root


def test_six_production_stacks_synthesise_with_data_retained(templates: Path):
    stacks = review.load_stacks(templates)
    assert [s.name for s in stacks] == [
        "Ch01HelloStack",
        "Ch02TelemetryStack",
        "Ch03BatchMlStack",
        "Ch04OrdersStack",
        "Ch05PrimaryStack",
        "Ch05StandbyStack",
    ]
    for s in stacks:
        for k, r in s.of_type("AWS::DynamoDB::GlobalTable").items():
            assert r["DeletionPolicy"] == "Retain", (s.name, k)


def test_the_review_is_clean_with_the_repos_acceptances(templates: Path):
    report = review.review(review.load_stacks(templates), review.load_accepted())
    assert report.open_findings == []
    assert report.unused_acceptances == []
    counts = review.summary(report)
    assert counts["fail"] == 0
    assert counts["accepted findings"] == 29
    assert counts["pass"] == 102 and counts["accepted"] == 21 and counts["n/a"] == 33


def test_without_the_acceptances_the_same_twenty_nine_findings_are_open(templates: Path):
    report = review.review(review.load_stacks(templates), [])
    assert len(report.open_findings) == 29
    by_rule = {}
    for c, _f in report.open_findings:
        by_rule[c.rule.id] = by_rule.get(c.rule.id, 0) + 1
    assert by_rule == {
        "SEC-04": 4,
        "REL-01": 2,
        "REL-02": 2,
        "REL-05": 3,
        "COST-02": 3,
        "COST-03": 2,
        "COST-04": 2,
        "COST-06": 8,
        "OPS-03": 3,
    }


def test_the_fixes_the_review_asked_for_are_in_the_templates(templates: Path):
    stacks = {s.name: s for s in review.load_stacks(templates)}
    stock = stacks["Ch04OrdersStack"].props("Stock38709CBC")
    assert (
        stock["Replicas"][0]["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"]
        is True
    )
    for name in ("Ch04OrdersStack", "Ch05PrimaryStack", "Ch05StandbyStack"):
        (stage,) = stacks[name].of_type("AWS::ApiGatewayV2::Stage").values()
        assert stage["Properties"]["DefaultRouteSettings"]["ThrottlingRateLimit"] == 50
    for name in ("Ch05PrimaryStack", "Ch05StandbyStack"):
        for b in stacks[name].of_type("AWS::S3::Bucket").values():
            assert "BucketEncryption" in b["Properties"]
    for s in stacks.values():
        for k, fn in s.of_type("AWS::Lambda::Function").items():
            if not k.startswith("Custom"):
                assert review.tags_of(fn).get("project") == "aws-architecture-by-example", (
                    s.name,
                    k,
                )


def test_the_idle_floor_of_the_five_designs(templates: Path):
    stacks = review.load_stacks(templates)
    floor = sum(sum(review.idle_floor(s).values()) for s in stacks)
    assert floor == pytest.approx(3.90)
