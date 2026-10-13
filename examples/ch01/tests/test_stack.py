"""Synthesise the stack in-process and check the properties the chapter relies on."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stack import HelloStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    return Template.from_stack(HelloStack(app, "TestStack"))


def test_bucket_is_private_and_tls_only(template: Template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        Match.object_like(
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {"Condition": {"Bool": {"aws:SecureTransport": "false"}}}
                            )
                        ]
                    )
                }
            }
        ),
    )


def test_cloudfront_has_two_origins_and_api_is_never_cached(template: Template):
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "PriceClass": "PriceClass_100",
                    "Origins": [Match.any_value(), Match.any_value()],
                    "CacheBehaviors": [
                        Match.object_like(
                            {
                                "PathPattern": "/api/*",
                                # Managed "CachingDisabled" policy id
                                "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
                            }
                        )
                    ],
                }
            )
        },
    )
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)


def test_table_is_on_demand_with_pitr_and_ttl(template: Template):
    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        Match.object_like(
            {
                "BillingMode": "PAY_PER_REQUEST",
                "TimeToLiveSpecification": {"AttributeName": "expires", "Enabled": True},
                "Replicas": [
                    Match.object_like(
                        {"PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True}}
                    )
                ],
            }
        ),
    )


def test_table_removal_policy_follows_flag():
    # One App per synth: CDK refuses to synthesise the same tree twice.
    t = Template.from_stack(HelloStack(cdk.App(), "Keep", retain_data=True))
    t.has_resource("AWS::DynamoDB::GlobalTable", {"DeletionPolicy": "Retain"})
    t2 = Template.from_stack(HelloStack(cdk.App(), "Drop", retain_data=False))
    t2.has_resource("AWS::DynamoDB::GlobalTable", {"DeletionPolicy": "Delete"})


def test_api_has_three_routes_and_a_throttle(template: Template):
    template.resource_count_is("AWS::ApiGatewayV2::Route", 3)
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        Match.object_like(
            {
                "StageName": "$default",
                "DefaultRouteSettings": {"ThrottlingBurstLimit": 100, "ThrottlingRateLimit": 50},
            }
        ),
    )


def test_handler_is_small_arm_python_with_bounded_logs(template: Template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like(
            {
                "Runtime": "python3.12",
                "Architectures": ["arm64"],
                "MemorySize": 256,
                "Timeout": 10,
                "Handler": "handler.lambda_handler",
            }
        ),
    )
    template.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 30})
