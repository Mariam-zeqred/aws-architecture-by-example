"""Synthesise the stack in-process and check the properties the chapter relies on."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stack import RULE_NAME, TelemetryStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    return Template.from_stack(TelemetryStack(app, "TestStack"))


def test_device_policy_is_scoped_to_the_connecting_thing(template: Template):
    policies = template.find_resources("AWS::IoT::Policy")
    (policy,) = policies.values()
    doc = json.dumps(policy["Properties"]["PolicyDocument"])
    assert "${iot:Connection.Thing.ThingName}" in doc
    assert f"$aws/rules/{RULE_NAME}/farm/telemetry/" in doc  # Basic Ingest topic
    assert '"iot:Connection.Thing.IsAttached": "true"' in doc
    for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
        assert statement["Action"] in ("iot:Connect", "iot:Publish")
        assert statement["Resource"] != "*"
    assert "iot:Subscribe" not in doc and "iot:Receive" not in doc


def test_rule_fans_out_to_lambda_and_firehose_with_an_error_action(template: Template):
    template.resource_count_is("AWS::IoT::TopicRule", 1)
    template.has_resource_properties(
        "AWS::IoT::TopicRule",
        {
            "RuleName": RULE_NAME,
            "TopicRulePayload": Match.object_like(
                {
                    "Sql": Match.string_like_regexp(
                        r"SELECT \*, topic\(3\) AS device, timestamp\(\) AS received "
                        r"FROM 'farm/telemetry/\+'"
                    ),
                    "Actions": [
                        Match.object_like({"Lambda": Match.any_value()}),
                        Match.object_like({"Firehose": Match.object_like({"Separator": "\n"})}),
                    ],
                    "ErrorAction": Match.object_like({"Sqs": Match.any_value()}),
                }
            ),
        },
    )
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        Match.object_like({"Principal": "iot.amazonaws.com"}),
    )


def test_history_expires_and_status_has_pitr(template: Template):
    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        Match.object_like(
            {
                "KeySchema": [
                    {"AttributeName": "device", "KeyType": "HASH"},
                    {"AttributeName": "ts", "KeyType": "RANGE"},
                ],
                "TimeToLiveSpecification": {"AttributeName": "expires", "Enabled": True},
                "BillingMode": "PAY_PER_REQUEST",
            }
        ),
    )
    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        Match.object_like(
            {
                "KeySchema": [{"AttributeName": "device", "KeyType": "HASH"}],
                "Replicas": [
                    Match.object_like(
                        {"PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True}}
                    )
                ],
            }
        ),
    )


def test_firehose_batches_compresses_and_partitions_by_day(template: Template):
    template.has_resource_properties(
        "AWS::KinesisFirehose::DeliveryStream",
        {
            "DeliveryStreamType": "DirectPut",
            "ExtendedS3DestinationConfiguration": Match.object_like(
                {
                    "BufferingHints": {"IntervalInSeconds": 300, "SizeInMBs": 64},
                    "CompressionFormat": "GZIP",
                    "Prefix": "raw/dt=!{timestamp:yyyy-MM-dd}/",
                    "ErrorOutputPrefix": Match.string_like_regexp(r"^errors/"),
                }
            ),
        },
    )
    template.has_resource_properties(
        "AWS::Glue::Table",
        Match.object_like(
            {
                "TableInput": Match.object_like(
                    {
                        "Parameters": Match.object_like({"projection.enabled": "true"}),
                        "PartitionKeys": [{"Name": "dt", "Type": "string"}],
                    }
                )
            }
        ),
    )
    template.has_resource_properties(
        "AWS::Athena::WorkGroup",
        Match.object_like(
            {
                "WorkGroupConfiguration": Match.object_like(
                    {"BytesScannedCutoffPerQuery": 10 * 1024**3}
                )
            }
        ),
    )


def test_buckets_are_private_and_the_archive_tiers_down(template: Template):
    for bucket in template.find_resources("AWS::S3::Bucket").values():
        assert bucket["Properties"]["PublicAccessBlockConfiguration"]["BlockPublicPolicy"] is True
    template.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like(
            {
                "LifecycleConfiguration": {
                    "Rules": [
                        Match.object_like(
                            {
                                "Transitions": [
                                    {"StorageClass": "STANDARD_IA", "TransitionInDays": 90}
                                ]
                            }
                        )
                    ]
                }
            }
        ),
    )


def test_three_alarms_however_many_things(template: Template):
    app = cdk.App()
    big = Template.from_stack(
        TelemetryStack(app, "Big", thing_names=tuple(f"field-{i}" for i in range(50)))
    )
    for t in (template, big):
        t.resource_count_is("AWS::CloudWatch::Alarm", 3)
        t.resource_count_is("AWS::SNS::Topic", 1)
    big.resource_count_is("AWS::IoT::Thing", 50)
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "MetricName": "StaleDevices",
                "Namespace": "FarmTelemetry",
                "TreatMissingData": "breaching",
                "EvaluationPeriods": 2,
            }
        ),
    )
    template.has_resource_properties(
        "AWS::Events::Rule", Match.object_like({"ScheduleExpression": "rate(5 minutes)"})
    )


def test_every_log_group_expires(template: Template):
    groups = template.find_resources("AWS::Logs::LogGroup")
    assert len(groups) == 3
    for group in groups.values():
        assert group["Properties"]["RetentionInDays"] == 30


def test_retain_flag_keeps_data_resources():
    app = cdk.App()
    t = Template.from_stack(TelemetryStack(app, "Keep", retain_data=True))
    tables = t.find_resources("AWS::DynamoDB::GlobalTable")
    assert all(r["DeletionPolicy"] == "Retain" for r in tables.values())
    assert any(
        r["DeletionPolicy"] == "Retain" for r in t.find_resources("AWS::S3::Bucket").values()
    )
