"""Chapter 1 reference architecture: static site + API + database.

One CloudFront distribution in front of two origins: an S3 bucket for the
static files and an HTTP API for ``/api/*``. The API is a single Lambda
function that reads and writes one DynamoDB table.

Every resource that holds data or costs money by the hour is called out
with a comment that says why it is configured the way it is. The stack is
meant to be read top to bottom.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

HERE = Path(__file__).parent


class HelloStack(cdk.Stack):
    """Static site + API + DB. ``retain_data`` keeps the table on ``cdk destroy``."""

    def __init__(
        self, scope: Construct, construct_id: str, *, retain_data: bool = False, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        data_policy = cdk.RemovalPolicy.RETAIN if retain_data else cdk.RemovalPolicy.DESTROY

        # --- Database -------------------------------------------------------
        # On-demand billing: no capacity planning, you pay per request. Right
        # for anything whose traffic you cannot predict, which is every new
        # product. Switch to provisioned capacity only once a month of
        # CloudWatch metrics shows a flat, predictable load.
        table = dynamodb.TableV2(
            self,
            "Readings",
            partition_key=dynamodb.Attribute(name="device", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="ts", type=dynamodb.AttributeType.NUMBER),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            time_to_live_attribute="expires",  # readings fall out after 30 days (set by the API)
            removal_policy=data_policy,
        )

        # --- API: one Lambda behind an HTTP API ------------------------------
        # 256 MB is the smallest size at which a Python handler that imports
        # boto3 starts quickly; ARM is ~20 % cheaper per GB-second than x86.
        log_group = logs.LogGroup(
            self,
            "ApiLogs",
            retention=logs.RetentionDays.ONE_MONTH,  # default is "forever", which is a bill
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        handler = lambda_.Function(
            self,
            "ApiHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(HERE / "lambda")),
            memory_size=256,
            timeout=cdk.Duration.seconds(10),  # the API will give up at 30 s anyway
            environment={"TABLE_NAME": table.table_name},
            log_group=log_group,
        )
        table.grant_read_write_data(handler)  # least privilege: this table, nothing else

        api = apigw.HttpApi(
            self,
            "Api",
            create_default_stage=False,  # we create it ourselves to set throttling
        )
        api_integration = integrations.HttpLambdaIntegration("ApiIntegration", handler)
        api.add_routes(
            path="/api/health", methods=[apigw.HttpMethod.GET], integration=api_integration
        )
        api.add_routes(
            path="/api/readings/{device}",
            methods=[apigw.HttpMethod.GET],
            integration=api_integration,
        )
        api.add_routes(
            path="/api/readings", methods=[apigw.HttpMethod.POST], integration=api_integration
        )
        apigw.HttpStage(
            self,
            "ApiStage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            # A throttle is the cheapest protection you will ever buy: a bug in
            # the front end or a script hammering the API costs at most this.
            throttle=apigw.ThrottleSettings(rate_limit=50, burst_limit=100),
        )

        # --- Static site: private bucket, CloudFront is the only reader -------
        site_bucket = s3.Bucket(
            self,
            "Site",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,  # the site is rebuilt from git; it is not data
        )

        api_domain = f"{api.api_id}.execute-api.{self.region}.{self.url_suffix}"
        distribution = cloudfront.Distribution(
            self,
            "Cdn",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(api_domain),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    # Never cache API responses by accident, and forward the
                    # query string (``?limit=``) but not the Host header, which
                    # API Gateway would reject.
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                )
            },
            # NA + Europe edges only: cheapest class, and the brief's users are
            # in one country. Use PRICE_CLASS_ALL when latency elsewhere matters.
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            # No ``minimum_protocol_version`` here: with the default
            # *.cloudfront.net certificate the TLS policy is fixed (TLS 1.0+)
            # and CDK warns that the setting is ignored. Exercise 4 adds a
            # custom domain + ACM certificate, which is what unlocks TLS 1.2-only.
            comment="ch01 static site + API",
        )

        # Upload ./site on every deploy and invalidate the cache, so a deploy
        # is one command and nobody edits the bucket by hand.
        s3deploy.BucketDeployment(
            self,
            "SiteDeploy",
            sources=[s3deploy.Source.asset(str(HERE / "site"))],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        cdk.CfnOutput(self, "SiteUrl", value=f"https://{distribution.distribution_domain_name}")
        cdk.CfnOutput(self, "ApiUrl", value=f"https://{api_domain}/api/health")
        cdk.CfnOutput(self, "TableName", value=table.table_name)
