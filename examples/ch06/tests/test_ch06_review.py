"""The rules on small hand-made templates, the acceptance logic, and the idle floor."""

import json
from pathlib import Path

import pytest

import review
from review import Acceptance, Finding, Stack

RULE = {r.id: r for r in review.RULES}


def stack(resources: dict, description: str = "a stack", name: str = "TestStack") -> Stack:
    return Stack(name, "chNN", {"Description": description, "Resources": resources})


def bucket(**props) -> dict:
    base = {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        },
        "BucketEncryption": {"ServerSideEncryptionConfiguration": []},
        "LifecycleConfiguration": {"Rules": []},
        "Tags": [{"Key": "project", "Value": "x"}],
    }
    return {"Type": "AWS::S3::Bucket", "DeletionPolicy": "Retain", "Properties": {**base, **props}}


def tls_policy(target: str, kind: str = "AWS::S3::BucketPolicy") -> dict:
    ref = (
        {"Bucket": {"Ref": target}} if kind.startswith("AWS::S3") else {"Queues": [{"Ref": target}]}
    )
    return {
        "Type": kind,
        "Properties": {
            **ref,
            "PolicyDocument": {
                "Statement": [
                    {
                        "Effect": "Deny",
                        "Action": "s3:*",
                        "Resource": "*",
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    }
                ]
            },
        },
    }


def function(**props) -> dict:
    base = {
        "Runtime": "python3.12",
        "Architectures": ["arm64"],
        "Timeout": 10,
        "LoggingConfig": {"LogGroup": {"Ref": "Logs"}},
        "Environment": {"Variables": {"TABLE_NAME": {"Ref": "T"}}},
        "Tags": [{"Key": "project", "Value": "x"}],
    }
    return {"Type": "AWS::Lambda::Function", "Properties": {**base, **props}}


# --- the rules, one at a time ------------------------------------------------------


def test_rules_are_not_applicable_to_an_empty_stack_except_the_stack_wide_ones():
    s = stack({})
    for rule in review.RULES:
        result = rule.check(s)
        if rule.id in ("REL-05", "OPS-01", "OPS-03", "COST-05"):
            assert result is not None, rule.id
        else:
            assert result is None, rule.id


def test_bucket_rules_pass_a_well_configured_bucket_and_fail_a_bare_one():
    good = stack({"B": bucket(), "P": tls_policy("B")})
    bare = stack({"B": {"Type": "AWS::S3::Bucket", "Properties": {}}})
    for rule_id in ("SEC-01", "SEC-02", "SEC-03", "COST-04", "REL-02", "COST-06"):
        assert RULE[rule_id].check(good) == [], rule_id
        assert len(RULE[rule_id].check(bare)) == 1, rule_id


def test_wildcards_are_findings_only_when_allowed_without_a_condition():
    def policy(statement):
        return {
            "Type": "AWS::IAM::Policy",
            "Properties": {"PolicyDocument": {"Statement": [statement]}},
        }

    allow_star = stack(
        {"P": policy({"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"})}
    )
    with_condition = stack(
        {
            "P": policy(
                {
                    "Effect": "Allow",
                    "Action": "cloudwatch:PutMetricData",
                    "Resource": "*",
                    "Condition": {"StringEquals": {"cloudwatch:namespace": "X"}},
                }
            )
        }
    )
    deny_star = stack({"P": policy({"Effect": "Deny", "Action": "s3:*", "Resource": "*"})})
    service_star = stack({"P": policy({"Effect": "Allow", "Action": "s3:*", "Resource": "arn:x"})})
    inline_on_role = stack(
        {
            "R": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "Policies": [
                        {
                            "PolicyDocument": {
                                "Statement": [
                                    {"Effect": "Allow", "Action": "*", "Resource": "arn:x"}
                                ]
                            }
                        }
                    ]
                },
            }
        }
    )
    assert len(RULE["SEC-04"].check(allow_star)) == 1
    assert RULE["SEC-04"].check(with_condition) == []
    assert RULE["SEC-04"].check(deny_star) == []
    assert len(RULE["SEC-04"].check(service_star)) == 1
    assert RULE["SEC-04"].check(inline_on_role) == [Finding("R", "Allow * without a Condition")]


def test_queue_rule_wants_encryption_and_a_tls_policy():
    good = stack(
        {
            "Q": {"Type": "AWS::SQS::Queue", "Properties": {"SqsManagedSseEnabled": True}},
            "P": tls_policy("Q", "AWS::SQS::QueuePolicy"),
        }
    )
    bare = stack({"Q": {"Type": "AWS::SQS::Queue", "Properties": {}}})
    assert RULE["SEC-05"].check(good) == []
    assert len(RULE["SEC-05"].check(bare)) == 2


def test_device_policy_must_name_the_thing_and_check_attachment():
    def iot(statements):
        return stack(
            {
                "P": {
                    "Type": "AWS::IoT::Policy",
                    "Properties": {"PolicyDocument": {"Statement": statements}},
                }
            }
        )

    named = iot(
        [
            {
                "Effect": "Allow",
                "Action": "iot:Connect",
                "Resource": "arn:aws:iot:r:a:client/${iot:Connection.Thing.ThingName}",
                "Condition": {"Bool": {"iot:Connection.Thing.IsAttached": "true"}},
            },
            {
                "Effect": "Allow",
                "Action": "iot:Publish",
                "Resource": "arn:aws:iot:r:a:topic/x/${iot:Connection.Thing.ThingName}",
            },
        ]
    )
    star = iot([{"Effect": "Allow", "Action": "iot:*", "Resource": "*"}])
    unattached = iot(
        [
            {
                "Effect": "Allow",
                "Action": "iot:Connect",
                "Resource": "arn:x/${iot:Connection.Thing.ThingName}",
            }
        ]
    )
    assert RULE["SEC-06"].check(named) == []
    assert len(RULE["SEC-06"].check(star)) == 1
    assert len(RULE["SEC-06"].check(unattached)) == 1


def test_api_stage_throttle_and_cloudfront_https():
    throttled = stack(
        {
            "S": {
                "Type": "AWS::ApiGatewayV2::Stage",
                "Properties": {
                    "DefaultRouteSettings": {"ThrottlingRateLimit": 50, "ThrottlingBurstLimit": 100}
                },
            }
        }
    )
    default = stack(
        {"S": {"Type": "AWS::ApiGatewayV2::Stage", "Properties": {"StageName": "$default"}}}
    )
    assert RULE["SEC-07"].check(throttled) == []
    assert len(RULE["SEC-07"].check(default)) == 1

    def dist(viewer, origin):
        return stack(
            {
                "D": {
                    "Type": "AWS::CloudFront::Distribution",
                    "Properties": {
                        "DistributionConfig": {
                            "DefaultCacheBehavior": {"ViewerProtocolPolicy": viewer},
                            "Origins": [
                                {
                                    "Id": "api",
                                    "CustomOriginConfig": {"OriginProtocolPolicy": origin},
                                }
                            ],
                        }
                    },
                }
            }
        )

    assert RULE["SEC-08"].check(dist("redirect-to-https", "https-only")) == []
    assert len(RULE["SEC-08"].check(dist("allow-all", "http-only"))) == 2


def test_function_rules():
    good = stack({"F": function()})
    assert all(
        RULE[r].check(good) == []
        for r in ("SEC-09", "SEC-10", "COST-02", "COST-03", "PERF-01", "COST-06")
    )
    old = stack({"F": function(Runtime="python3.8")})
    assert RULE["SEC-09"].check(old) == [Finding("F", "runtime python3.8")]
    leaky = stack({"F": function(Environment={"Variables": {"API_KEY": "abc"}})})
    assert len(RULE["SEC-10"].check(leaky)) == 1
    bare = stack({"F": {"Type": "AWS::Lambda::Function", "Properties": {"Runtime": "python3.12"}}})
    assert len(RULE["COST-02"].check(bare)) == 1
    assert len(RULE["COST-03"].check(bare)) == 1
    assert len(RULE["PERF-01"].check(bare)) == 1


def test_table_rules_read_replicas_and_deletion_policy():
    def table(pitr: bool, policy: str = "Retain"):
        return {
            "Type": "AWS::DynamoDB::GlobalTable",
            "DeletionPolicy": policy,
            "Properties": {
                "Replicas": [
                    {
                        "Region": "us-east-1",
                        "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": pitr},
                        "Tags": [{"Key": "project", "Value": "x"}],
                    }
                ]
            },
        }

    assert RULE["REL-01"].check(stack({"T": table(True)})) == []
    assert len(RULE["REL-01"].check(stack({"T": table(False)}))) == 1
    assert len(RULE["REL-02"].check(stack({"T": table(True, "Delete")}))) == 1
    assert RULE["COST-06"].check(stack({"T": table(True)})) == []


def test_queue_sinks_are_not_work_queues():
    s = stack(
        {
            "Work": {
                "Type": "AWS::SQS::Queue",
                "Properties": {
                    "RedrivePolicy": {"deadLetterTargetArn": {"Fn::GetAtt": ["Dlq", "Arn"]}}
                },
            },
            "Dlq": {"Type": "AWS::SQS::Queue", "Properties": {}},
            "Rejects": {"Type": "AWS::SQS::Queue", "Properties": {}},
            "Rule": {
                "Type": "AWS::IoT::TopicRule",
                "Properties": {
                    "TopicRulePayload": {"ErrorAction": {"Sqs": {"QueueUrl": {"Ref": "Rejects"}}}}
                },
            },
            "Orphan": {"Type": "AWS::SQS::Queue", "Properties": {}},
        }
    )
    assert RULE["REL-03"].check(s) == [Finding("Orphan", "a work queue with no RedrivePolicy")]


def test_event_source_mappings_count_only_sqs_sources():
    s = stack(
        {
            "Q": {"Type": "AWS::SQS::Queue", "Properties": {}},
            "M": {
                "Type": "AWS::Lambda::EventSourceMapping",
                "Properties": {
                    "EventSourceArn": {"Fn::GetAtt": ["Q", "Arn"]},
                    "FunctionResponseTypes": [],
                },
            },
            "Stream": {
                "Type": "AWS::Lambda::EventSourceMapping",
                "Properties": {"EventSourceArn": "arn:kinesis"},
            },
        }
    )
    assert RULE["REL-06"].check(s) == [
        Finding("M", "FunctionResponseTypes lacks ReportBatchItemFailures")
    ]
    assert RULE["REL-06"].check(stack({"Stream": s.resources["Stream"]})) is None


def test_alarm_rules_and_silence():
    alarm = {
        "Type": "AWS::CloudWatch::Alarm",
        "Properties": {
            "AlarmActions": [{"Ref": "T"}],
            "AlarmDescription": "x",
            "TreatMissingData": "breaching",
        },
    }
    topic = {"Type": "AWS::SNS::Topic", "Properties": {"Tags": [{"Key": "project", "Value": "x"}]}}
    watched = stack({"A": alarm, "T": topic})
    assert RULE["REL-04"].check(watched) == []
    assert RULE["REL-05"].check(watched) == []
    assert RULE["OPS-02"].check(watched) == []
    assert RULE["OPS-03"].check(watched) == []
    quiet = stack(
        {
            "A": {
                "Type": "AWS::CloudWatch::Alarm",
                "Properties": {"TreatMissingData": "notBreaching"},
            }
        }
    )
    assert len(RULE["REL-04"].check(quiet)) == 1
    assert len(RULE["REL-05"].check(quiet)) == 1  # no topic
    assert len(RULE["OPS-02"].check(quiet)) == 1
    assert len(RULE["OPS-03"].check(quiet)) == 1
    checked = stack({"H": {"Type": "AWS::Route53::HealthCheck", "Properties": {}}})
    assert RULE["OPS-03"].check(checked) == []


def test_idle_billing_rule_knows_gateway_endpoints_are_free():
    s = stack(
        {
            "G": {"Type": "AWS::EC2::VPCEndpoint", "Properties": {"VpcEndpointType": "Gateway"}},
            "I": {"Type": "AWS::EC2::VPCEndpoint", "Properties": {"VpcEndpointType": "Interface"}},
            "N": {"Type": "AWS::EC2::NatGateway", "Properties": {}},
            "L": {"Type": "AWS::Logs::LogGroup", "Properties": {"RetentionInDays": 30}},
        }
    )
    found = {f.resource for f in RULE["COST-05"].check(s)}
    assert found == {"I", "N"}
    assert RULE["COST-01"].check(s) == []
    assert RULE["OPS-01"].check(stack({}, description="")) == [
        Finding("TestStack", "no stack Description")
    ]


# --- acceptances -----------------------------------------------------------------------


def test_an_acceptance_turns_a_finding_into_acc_and_an_unused_one_is_reported():
    s = stack({"B": {"Type": "AWS::S3::Bucket", "Properties": {}}})
    rules = [RULE["SEC-03"]]
    open_report = review.review([s], [], rules)
    assert [c.status for c in open_report.cells] == ["FAIL 1"]
    accepted = [Acceptance("SEC-03", "Test*", "B*", "because")]
    acc_report = review.review([s], accepted, rules)
    assert [c.status for c in acc_report.cells] == ["acc 1"]
    assert acc_report.open_findings == [] and acc_report.unused_acceptances == []
    stale = [Acceptance("SEC-03", "Other*", "*", "because")]
    stale_report = review.review([s], stale, rules)
    assert len(stale_report.open_findings) == 1 and stale_report.unused_acceptances == stale


def test_an_acceptance_without_a_reason_is_refused(tmp_path: Path):
    p = tmp_path / "accepted.json"
    p.write_text(json.dumps({"accepted": [{"rule": "SEC-03", "resource": "*", "reason": " "}]}))
    with pytest.raises(ValueError):
        review.load_accepted(p)


def test_the_repos_accepted_file_loads_and_every_entry_has_a_reason():
    entries = review.load_accepted()
    assert entries and all(a.reason for a in entries)
    assert all(a.rule in RULE for a in entries)


# --- the idle floor --------------------------------------------------------------------


def test_idle_floor_counts_alarms_checks_and_replication_metrics():
    s = stack(
        {
            "A1": {"Type": "AWS::CloudWatch::Alarm", "Properties": {}},
            "A2": {"Type": "AWS::CloudWatch::Alarm", "Properties": {}},
            "H": {"Type": "AWS::Route53::HealthCheck", "Properties": {}},
            "B": bucket(
                ReplicationConfiguration={
                    "Rules": [{"Destination": {"Metrics": {"Status": "Enabled"}}}]
                }
            ),
        }
    )
    floor = review.idle_floor(s)
    assert floor["2 alarms"] == pytest.approx(0.20)
    assert floor["1 health checks"] == 0.0
    assert floor["4 replication metrics"] == pytest.approx(1.20)
    assert "0 hosted zones" not in floor


def test_summary_and_tables_render(tmp_path: Path):
    s = stack({"B": bucket(), "P": tls_policy("B")})
    report = review.review([s], [])
    md = review.matrix_table(report)
    assert md.count("\n") == len(review.RULES) + 1
    assert "| SEC-01 | Security |" in md
    counts = review.summary(report)
    assert counts["cells"] == len(review.RULES)
    assert counts["pass"] + counts["accepted"] + counts["fail"] + counts["n/a"] == counts["cells"]
    assert "| **All five designs** |" in review.idle_table([s])
