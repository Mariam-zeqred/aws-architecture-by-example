"""Synthesise both regions in-process and check what the chapter relies on."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stack import (
    BACKUP_RETENTION,
    HEALTH_FAILURES,
    HEALTH_INTERVAL,
    PRIMARY_REGION,
    STANDBY_REGION,
    TABLE_NAME,
    THROTTLE_BURST,
    THROTTLE_RATE,
    RegionStack,
)

ZONE = {"zone_name": "coop.example", "hosted_zone_id": "Z0000000000000000000A"}


def synth(role: str, **kwargs) -> Template:
    region = PRIMARY_REGION if role == "primary" else STANDBY_REGION
    stack = RegionStack(
        cdk.App(),
        f"Test{role.title()}",
        role=role,
        env=cdk.Environment(account="123456789012", region=region),
        **ZONE,
        **kwargs,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def primary() -> Template:
    return synth("primary")


@pytest.fixture(scope="module")
def standby() -> Template:
    return synth("standby")


def test_role_must_be_primary_or_standby():
    with pytest.raises(ValueError):
        RegionStack(cdk.App(), "Bad", role="tertiary", **ZONE)


def test_primary_creates_the_global_table_with_a_protected_replica(primary: Template):
    primary.resource_count_is("AWS::DynamoDB::GlobalTable", 1)
    table = next(iter(primary.find_resources("AWS::DynamoDB::GlobalTable").values()))
    assert table["Properties"]["TableName"] == TABLE_NAME
    assert table["DeletionPolicy"] == "Retain"
    replicas = {r["Region"]: r for r in table["Properties"]["Replicas"]}
    assert set(replicas) == {PRIMARY_REGION, STANDBY_REGION}
    for r in replicas.values():
        assert r["DeletionProtectionEnabled"] is True
        assert r["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True


def test_standby_only_names_the_table_and_may_read_and_write_its_replica(standby: Template):
    standby.resource_count_is("AWS::DynamoDB::GlobalTable", 0)
    standby.resource_count_is("AWS::DynamoDB::Table", 0)
    fn = next(iter(standby.find_resources("AWS::Lambda::Function").values()))
    assert fn["Properties"]["Environment"]["Variables"] == {
        "ORDERS_TABLE": TABLE_NAME,
        "REGION_ROLE": "standby",
    }
    policy = next(iter(standby.find_resources("AWS::IAM::Policy").values()))
    arns = str(policy["Properties"]["PolicyDocument"]["Statement"][0]["Resource"])
    assert f":dynamodb:{STANDBY_REGION}:" in arns and f"table/{TABLE_NAME}" in arns


def test_primary_buckets_replicate_to_the_standby_names_without_deletes(primary: Template):
    buckets = primary.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 2
    for b in buckets.values():
        props = b["Properties"]
        assert props["VersioningConfiguration"] == {"Status": "Enabled"}
        (rule,) = props["ReplicationConfiguration"]["Rules"]
        assert rule["DeleteMarkerReplication"] == {"Status": "Disabled"}
        assert rule["Destination"]["Metrics"]["Status"] == "Enabled"
        parts = rule["Destination"]["Bucket"]["Fn::Join"][1]
        dest = "".join(x if isinstance(x, str) else "ACCOUNT" for x in parts)
        assert dest.endswith(f"-ACCOUNT-{STANDBY_REGION}")
        assert rule["Id"].endswith(f"-to-{STANDBY_REGION}")
    roles = [
        r for k, r in primary.find_resources("AWS::IAM::Role").items() if "ReplicationRole" in k
    ]
    assert len(roles) == 2 and all("RoleName" not in r["Properties"] for r in roles)


def test_standby_buckets_are_versioned_destinations_readable_by_this_accounts_cloudfront(
    standby: Template,
):
    buckets = standby.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 2
    for b in buckets.values():
        assert b["Properties"]["VersioningConfiguration"] == {"Status": "Enabled"}
        assert "ReplicationConfiguration" not in b["Properties"]
    policies = standby.find_resources("AWS::S3::BucketPolicy")
    statements = [
        s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    cloudfront = [
        s for s in statements if s.get("Principal") == {"Service": "cloudfront.amazonaws.com"}
    ]
    assert len(cloudfront) == 1
    assert "distribution/*" in str(cloudfront[0]["Condition"]["ArnLike"]["AWS:SourceArn"])


def test_daily_backup_is_copied_to_the_standby_vault_and_kept_35_days(primary: Template):
    plan = next(iter(primary.find_resources("AWS::Backup::BackupPlan").values()))
    (rule,) = plan["Properties"]["BackupPlan"]["BackupPlanRule"]
    assert rule["ScheduleExpression"] == "cron(0 3 * * ? *)"
    assert rule["Lifecycle"]["DeleteAfterDays"] == BACKUP_RETENTION.to_days()
    (copy,) = rule["CopyActions"]
    assert copy["Lifecycle"]["DeleteAfterDays"] == BACKUP_RETENTION.to_days()
    assert f"arn:aws:backup:{STANDBY_REGION}:" in str(copy["DestinationBackupVaultArn"])
    primary.resource_count_is("AWS::Backup::BackupSelection", 1)
    primary.resource_count_is("AWS::Backup::BackupVault", 1)


def test_standby_has_a_vault_and_no_plan(standby: Template):
    standby.resource_count_is("AWS::Backup::BackupVault", 1)
    standby.resource_count_is("AWS::Backup::BackupPlan", 0)
    vault = next(iter(standby.find_resources("AWS::Backup::BackupVault").values()))
    assert vault["Properties"]["BackupVaultName"] == f"coop-vault-{STANDBY_REGION}"


def test_health_check_reads_the_whole_path_from_outside(primary: Template):
    hc = next(iter(primary.find_resources("AWS::Route53::HealthCheck").values()))
    cfg = hc["Properties"]["HealthCheckConfig"]
    assert cfg["Type"] == "HTTPS" and cfg["ResourcePath"] == "/health"
    assert cfg["RequestInterval"] == HEALTH_INTERVAL.to_seconds()
    assert cfg["FailureThreshold"] == HEALTH_FAILURES
    assert cfg["EnableSNI"] is True and cfg.get("MeasureLatency") is False  # no paid options


def test_one_record_name_two_failover_answers(primary: Template, standby: Template):
    p = next(iter(primary.find_resources("AWS::Route53::RecordSet").values()))["Properties"]
    s = next(iter(standby.find_resources("AWS::Route53::RecordSet").values()))["Properties"]
    assert p["Name"] == s["Name"] == "api.coop.example"
    assert p["Failover"] == "PRIMARY" and s["Failover"] == "SECONDARY"
    assert "HealthCheckId" in p and "HealthCheckId" not in s
    assert p["SetIdentifier"] != s["SetIdentifier"]
    for r in (p, s):
        assert r["Type"] == "A" and r["AliasTarget"]["EvaluateTargetHealth"] is True
        assert "TTL" not in r  # alias records carry the target's TTL
    for t in (primary, standby):
        t.resource_count_is("AWS::CertificateManager::Certificate", 1)
        t.resource_count_is("AWS::ApiGatewayV2::DomainName", 1)
        t.has_resource_properties("AWS::ApiGatewayV2::Route", {"RouteKey": "GET /health"})
        t.has_resource_properties("AWS::ApiGatewayV2::Route", {"RouteKey": "POST /orders"})


def test_site_falls_back_to_the_standby_bucket(primary: Template):
    dist = next(iter(primary.find_resources("AWS::CloudFront::Distribution").values()))
    cfg = dist["Properties"]["DistributionConfig"]
    (group,) = cfg["OriginGroups"]["Items"]
    assert group["Members"]["Quantity"] == 2
    assert set(group["FailoverCriteria"]["StatusCodes"]["Items"]) >= {500, 502, 503, 504}
    origins = [str(o["DomainName"]) for o in cfg["Origins"]]
    assert any(f"-{STANDBY_REGION}.s3.{STANDBY_REGION}.amazonaws.com" in o for o in origins)
    primary.resource_count_is("AWS::CloudFront::OriginAccessControl", 2)


def test_every_copy_has_an_alarm_and_backup_failures_are_routed(primary: Template):
    alarms = primary.find_resources("AWS::CloudWatch::Alarm")
    metrics = sorted(a["Properties"]["MetricName"] for a in alarms.values())
    assert metrics == [
        "HealthCheckStatus",
        "NumberOfBackupJobsCompleted",
        "OperationsPendingReplication",
        "OperationsPendingReplication",
        "ReplicationLatency",
    ]
    silence = next(
        a["Properties"]
        for a in alarms.values()
        if a["Properties"]["MetricName"] == "NumberOfBackupJobsCompleted"
    )
    assert silence["TreatMissingData"] == "breaching" and silence["Period"] == 86_400
    primary.has_resource_properties(
        "AWS::Events::Rule",
        Match.object_like(
            {
                "EventPattern": {
                    "source": ["aws.backup"],
                    "detail-type": ["Backup Job State Change", "Copy Job State Change"],
                    "detail": {"state": ["FAILED", "ABORTED", "EXPIRED"]},
                }
            }
        ),
    )


def test_standby_is_quiet(standby: Template):
    standby.resource_count_is("AWS::CloudWatch::Alarm", 0)
    standby.resource_count_is("AWS::Route53::HealthCheck", 0)
    standby.resource_count_is("AWS::CloudFront::Distribution", 0)


def test_resource_counts_match_the_chapter(primary: Template, standby: Template):
    assert len(primary.to_json()["Resources"]) == 42
    assert len(standby.to_json()["Resources"]) == 23


def test_protect_data_off_makes_everything_destroyable():
    t = synth("primary", protect_data=False)
    table = next(iter(t.find_resources("AWS::DynamoDB::GlobalTable").values()))
    assert table["DeletionPolicy"] == "Delete"
    assert all(not r["DeletionProtectionEnabled"] for r in table["Properties"]["Replicas"])
    buckets = t.find_resources("AWS::S3::Bucket").values()
    assert all(b["DeletionPolicy"] == "Delete" for b in buckets)


@pytest.mark.parametrize("role", ["primary", "standby"])
def test_chapter_six_review_fixes_buckets_say_their_encryption_and_the_stage_throttles(role):
    t = synth(role)
    for b in t.find_resources("AWS::S3::Bucket").values():
        (rule,) = b["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert rule["ServerSideEncryptionByDefault"] == {"SSEAlgorithm": "AES256"}
    (stage,) = t.find_resources("AWS::ApiGatewayV2::Stage").values()
    assert stage["Properties"]["DefaultRouteSettings"] == {
        "ThrottlingBurstLimit": THROTTLE_BURST,
        "ThrottlingRateLimit": THROTTLE_RATE,
    }
