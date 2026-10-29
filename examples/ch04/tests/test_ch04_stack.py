"""Synthesise the stack in-process and check the properties the chapter relies on."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stack import (
    ARCHIVE_RETENTION,
    BATCH_SIZE,
    CONSUMER_TIMEOUT,
    MAX_RECEIVE_COUNT,
    THROTTLE_BURST,
    THROTTLE_RATE,
    OrdersStack,
)


@pytest.fixture(scope="module")
def template() -> Template:
    return Template.from_stack(OrdersStack(cdk.App(), "TestStack"))


def test_two_consumer_queues_each_with_a_dead_letter_queue(template: Template):
    queues = template.find_resources("AWS::SQS::Queue")
    with_redrive = [q for q in queues.values() if "RedrivePolicy" in q["Properties"]]
    assert len(queues) == 5 and len(with_redrive) == 2  # payment, fulfilment + 3 DLQs
    for q in with_redrive:
        assert q["Properties"]["RedrivePolicy"]["maxReceiveCount"] == MAX_RECEIVE_COUNT
        assert q["Properties"]["VisibilityTimeout"] == 6 * CONSUMER_TIMEOUT.to_seconds()
    dlqs = [q for q in queues.values() if "RedrivePolicy" not in q["Properties"]]
    assert all(q["Properties"]["MessageRetentionPeriod"] == 14 * 86_400 for q in dlqs)


def test_consumers_report_batch_item_failures(template: Template):
    mappings = template.find_resources("AWS::Lambda::EventSourceMapping")
    assert len(mappings) == 2
    for m in mappings.values():
        assert m["Properties"]["BatchSize"] == BATCH_SIZE
        assert m["Properties"]["FunctionResponseTypes"] == ["ReportBatchItemFailures"]


def test_rules_route_by_detail_type_and_queue_targets_have_a_dlq(template: Template):
    rules = template.find_resources("AWS::Events::Rule")
    patterns = {tuple(r["Properties"]["EventPattern"]["detail-type"]) for r in rules.values()}
    assert patterns == {("OrderPlaced",), ("OrderPaid",), ("PaymentFailed", "OrderBackordered")}
    for r in rules.values():
        assert r["Properties"]["EventPattern"]["source"] == ["coop.orders"]
        (target,) = r["Properties"]["Targets"]
        if "InputTransformer" not in target:  # the two queue targets
            assert "DeadLetterConfig" in target
            assert target["RetryPolicy"] == {
                "MaximumEventAgeInSeconds": 86_400,
                "MaximumRetryAttempts": 185,
            }


def test_bus_is_archived_for_replay(template: Template):
    template.has_resource_properties(
        "AWS::Events::Archive",
        Match.object_like(
            {
                "RetentionDays": ARCHIVE_RETENTION.to_days(),
                "EventPattern": {"source": ["coop.orders"]},
            }
        ),
    )


def test_every_dead_letter_queue_has_an_alarm_and_each_queue_a_stuck_alarm(template: Template):
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert len(alarms) == 5
    by_metric = {}
    for a in alarms.values():
        by_metric.setdefault(a["Properties"]["MetricName"], []).append(a["Properties"])
    assert len(by_metric["ApproximateNumberOfMessagesVisible"]) == 3
    assert len(by_metric["ApproximateAgeOfOldestMessage"]) == 2
    for a in by_metric["ApproximateNumberOfMessagesVisible"]:
        assert a["Threshold"] == 0 and a["ComparisonOperator"] == "GreaterThanThreshold"
    for a in by_metric["ApproximateAgeOfOldestMessage"]:
        assert a["Threshold"] == 900


def test_only_the_accept_function_is_behind_the_api_and_the_tables_are_recoverable(
    template: Template,
):
    template.resource_count_is("AWS::ApiGatewayV2::Api", 1)
    template.has_resource_properties("AWS::ApiGatewayV2::Route", {"RouteKey": "POST /orders"})
    template.resource_count_is("AWS::Lambda::Function", 3)
    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        Match.object_like(
            {
                "Replicas": [
                    Match.object_like(
                        {"PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True}}
                    )
                ]
            }
        ),
    )


def test_consumers_may_publish_events_but_the_api_function_cannot_touch_stock(template: Template):
    policies = template.find_resources("AWS::IAM::Policy")
    puts = [
        p for p in policies.values() if "events:PutEvents" in str(p["Properties"]["PolicyDocument"])
    ]
    assert len(puts) == 3  # accept, charge, fulfil
    accept = [p for name, p in policies.items() if name.startswith("Accept")]
    assert accept and "Stock" not in str(accept[0]["Properties"]["PolicyDocument"])


def test_chapter_six_review_fixes_stock_has_pitr_and_the_default_stage_throttles(
    template: Template,
):
    tables = template.find_resources("AWS::DynamoDB::GlobalTable")
    assert len(tables) == 2
    for t in tables.values():
        (replica,) = t["Properties"]["Replicas"]
        assert replica["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    (stage,) = template.find_resources("AWS::ApiGatewayV2::Stage").values()
    assert stage["Properties"]["StageName"] == "$default"
    assert stage["Properties"]["DefaultRouteSettings"] == {
        "ThrottlingBurstLimit": THROTTLE_BURST,
        "ThrottlingRateLimit": THROTTLE_RATE,
    }
