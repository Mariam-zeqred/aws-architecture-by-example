"""Synthesise the stack in-process and check the properties the chapter relies on."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stack import SCHEDULE_HOUR_UTC, TASK_CPU, TASK_MEMORY_MIB, BatchMlStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    return Template.from_stack(BatchMlStack(app, "TestStack"))


def definition(template: Template) -> str:
    (machine,) = template.find_resources("AWS::StepFunctions::StateMachine").values()
    return json.dumps(machine["Properties"]["DefinitionString"])


def test_run_id_is_the_execution_date(template: Template):
    assert "States.ArrayGetItem(States.StringSplit($$.Execution.StartTime, 'T'), 0)" in definition(
        template
    )


def test_tasks_run_synchronously_with_timeouts_and_commands(template: Template):
    d = definition(template)
    assert d.count("ecs:runTask.sync") == 2
    assert '\\"Command\\":[\\"train\\"]' in d and '\\"Command\\":[\\"score\\"]' in d
    assert '\\"TimeoutSeconds\\":5400' in d and '\\"TimeoutSeconds\\":1800' in d
    assert '\\"Name\\":\\"RUN_ID\\",\\"Value.$\\":\\"$.run_id\\"' in d


def test_every_task_is_caught_and_the_choice_guards_scoring(template: Template):
    d = definition(template)
    assert d.count('\\"Next\\":\\"NotifyFailure\\"') == 3  # Train, Gate, Score
    assert '\\"Variable\\":\\"$.result.gate.model_available\\",\\"BooleanEquals\\":true' in d
    assert '\\"Default\\":\\"NoModel\\"' in d
    assert "States.JsonToString($.result.gate)" in d  # SNS wants a string


def test_schedule_fires_once_a_night_in_utc(template: Template):
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        Match.object_like(
            {
                "ScheduleExpression": f"cron(0 {SCHEDULE_HOUR_UTC} * * ? *)",
                "ScheduleExpressionTimezone": "Etc/UTC",
                "FlexibleTimeWindow": {"Mode": "OFF"},
            }
        ),
    )


def test_no_success_today_alarm_treats_missing_data_as_breaching(template: Template):
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "MetricName": "ExecutionsSucceeded",
                "Period": 86400,
                "Threshold": 1,
                "ComparisonOperator": "LessThanThreshold",
                "TreatMissingData": "breaching",
            }
        ),
    )
    template.resource_count_is("AWS::CloudWatch::Alarm", 2)


def test_task_is_arm_and_the_network_has_no_nat_gateway(template: Template):
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        Match.object_like(
            {
                "Cpu": str(TASK_CPU),
                "Memory": str(TASK_MEMORY_MIB),
                "RequiresCompatibilities": ["FARGATE"],
                "RuntimePlatform": {"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"},
            }
        ),
    )
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::EC2::VPCEndpoint", 2)
    for sg in template.find_resources("AWS::EC2::SecurityGroup").values():
        assert "SecurityGroupIngress" not in sg["Properties"]


def test_candidates_expire_and_current_models_are_versioned(template: Template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like(
            {
                "VersioningConfiguration": {"Status": "Enabled"},
                "LifecycleConfiguration": {
                    "Rules": Match.array_with(
                        [Match.object_like({"Prefix": "models/candidate/", "ExpirationInDays": 30})]
                    )
                },
            }
        ),
    )


def test_pointing_at_chapter_2_archive_creates_one_bucket_fewer():
    own = Template.from_stack(BatchMlStack(cdk.App(), "Own"))
    shared = Template.from_stack(
        BatchMlStack(cdk.App(), "Shared", archive_bucket_name="farm-archive-from-chapter-2")
    )
    assert len(own.find_resources("AWS::S3::Bucket")) == 2
    assert len(shared.find_resources("AWS::S3::Bucket")) == 1
    (task_def,) = shared.find_resources("AWS::ECS::TaskDefinition").values()
    env = task_def["Properties"]["ContainerDefinitions"][0]["Environment"]
    assert {"Name": "ARCHIVE_ROOT", "Value": "s3://farm-archive-from-chapter-2"} in env
