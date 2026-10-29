"""Chapter 5 reference architecture: the same stack in two regions.

One class, ``RegionStack``, is instantiated twice: once as the *primary*
(us-east-1, where chapters 1 to 4 run) and once as the *standby*
(eu-west-1). Both regions get the same API and function, the same two
buckets, a backup vault and the DNS record for ``api.<zone>``; what
differs is which side of each copy they are on:

* the orders table is a DynamoDB global table created by the primary with
  a replica in the standby; the standby only reads it by name;
* the archive and site buckets replicate from the primary to the standby;
* a daily backup is taken in the primary and copied into the standby's
  vault; point-in-time recovery is on in both regions;
* Route 53 answers ``api.<zone>`` with the primary while a health check
  from outside both regions says it is healthy, and with the standby
  otherwise; CloudFront falls back to the standby's site bucket itself.

Deploy the standby first (it owns the destinations), then the primary.
Read top to bottom; the chapter walks the same resources.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_backup as backup
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from constructs import Construct

HERE = Path(__file__).parent

PRIMARY_REGION = "us-east-1"
STANDBY_REGION = "eu-west-1"
TABLE_NAME = "coop-orders"  # a global table has the same name in every region

# Failover timing. A Route 53 health checker asks every HEALTH_INTERVAL from
# several AWS regions; the endpoint is unhealthy after HEALTH_FAILURES
# consecutive failures; resolvers keep the answer for DNS_TTL. Worst case to
# a client with a well-behaved resolver: 90 s + 60 s.
HEALTH_INTERVAL = cdk.Duration.seconds(30)
HEALTH_FAILURES = 3
DNS_TTL = cdk.Duration.seconds(60)

# Backups: one a day, kept 35 days (the PITR window), copied to the standby.
BACKUP_HOUR_UTC = 3
BACKUP_RETENTION = cdk.Duration.days(35)
# Global tables usually replicate in about a second; alarm when a change has
# not reached the other region for two minutes.
REPLICATION_LAG_ALARM = cdk.Duration.minutes(2)
# The API's throttle (chapter 6's review, SEC-07): the same as chapter 1's.
THROTTLE_RATE, THROTTLE_BURST = 50, 100


class RegionStack(cdk.Stack):
    """API + function + buckets + vault + DNS record, as primary or standby."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        role: str,
        zone_name: str,
        hosted_zone_id: str,
        name_prefix: str = "coop",
        protect_data: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if role not in ("primary", "standby"):
            raise ValueError("role must be 'primary' or 'standby'")
        primary = role == "primary"
        region = cdk.Stack.of(self).region
        data_policy = cdk.RemovalPolicy.RETAIN if protect_data else cdk.RemovalPolicy.DESTROY
        account = cdk.Aws.ACCOUNT_ID

        def bucket_name(kind: str, in_region: str) -> str:
            # Deterministic names, so each region can address the other's
            # buckets without a cross-region CloudFormation reference.
            return f"{name_prefix}-{kind}-{account}-{in_region}"

        alerts = sns.Topic(self, "Alerts")

        # --- Buckets: versioned everywhere; the primary replicates to the standby ----
        def replicated_bucket(kind: str) -> s3.Bucket:
            rules = None
            if primary:
                destination = s3.Bucket.from_bucket_attributes(
                    self,
                    f"{kind.title()}Replica",
                    bucket_name=bucket_name(kind, STANDBY_REGION),
                    region=STANDBY_REGION,
                )
                rules = [
                    s3.ReplicationRule(
                        destination=destination,
                        id=f"{kind}-to-{STANDBY_REGION}",
                        # A delete in the primary is NOT replicated: an emptied
                        # bucket here stays full there. Versioning keeps every
                        # overwritten object on both sides.
                        delete_marker_replication=False,
                        # Publishes the replication metrics the alarms below watch.
                        metrics=s3.ReplicationTimeValue.FIFTEEN_MINUTES,
                    )
                ]
            return s3.Bucket(
                self,
                kind.title(),
                bucket_name=bucket_name(kind, region),
                versioned=True,  # required for replication; also the undo button
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                # Chapter 6's review (SEC-03): S3 encrypts by default since 2023,
                # but a template that says so is one a reviewer can check.
                encryption=s3.BucketEncryption.S3_MANAGED,
                enforce_ssl=True,
                removal_policy=data_policy,
                lifecycle_rules=[
                    s3.LifecycleRule(noncurrent_version_expiration=cdk.Duration.days(90))
                ],
                replication_rules=rules,
            )

        archive = replicated_bucket("archive")
        site = replicated_bucket("site")
        if primary:
            # The L2 names every replication role "CDKReplicationRole"; two
            # replicated buckets in one stack would collide. Let CloudFormation name them.
            for b in (archive, site):
                role_cfn = b.node.find_child("ReplicationRole").node.default_child
                role_cfn.add_property_deletion_override("RoleName")

        # --- The orders table: a global table, created once -------------------------
        if primary:
            orders: dynamodb.ITableV2 = dynamodb.TableV2(
                self,
                "Orders",
                table_name=TABLE_NAME,
                partition_key=dynamodb.Attribute(
                    name="order_id", type=dynamodb.AttributeType.STRING
                ),
                billing=dynamodb.Billing.on_demand(),
                point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                ),
                deletion_protection=protect_data,
                removal_policy=data_policy,
                replicas=[
                    dynamodb.ReplicaTableProps(
                        region=STANDBY_REGION,
                        point_in_time_recovery_specification=(
                            dynamodb.PointInTimeRecoverySpecification(
                                point_in_time_recovery_enabled=True
                            )
                        ),
                        deletion_protection=protect_data,
                    )
                ],
            )
        else:
            # The replica is created by the primary's stack; here it is only a name.
            orders = dynamodb.TableV2.from_table_name(self, "Orders", TABLE_NAME)

        # --- The API: identical in both regions ---------------------------------------
        fn = lambda_.Function(
            self,
            "Api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="api.lambda_handler",
            code=lambda_.Code.from_asset(str(HERE / "lambda")),
            memory_size=256,
            timeout=cdk.Duration.seconds(10),
            environment={"ORDERS_TABLE": TABLE_NAME, "REGION_ROLE": role},
            log_group=logs.LogGroup(
                self,
                "ApiLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        orders.grant_read_write_data(fn)
        api = apigw.HttpApi(self, "HttpApi", description=f"Chapter 5: {role} API in {region}")
        # Chapter 6's review (SEC-07): the $default stage the L2 creates has no
        # throttle; set one on the underlying CfnStage, keeping ``api.url``.
        api.default_stage.node.default_child.default_route_settings = (
            apigw.CfnStage.RouteSettingsProperty(
                throttling_rate_limit=THROTTLE_RATE, throttling_burst_limit=THROTTLE_BURST
            )
        )
        integration = integrations.HttpLambdaIntegration("ApiIntegration", fn)
        api.add_routes(path="/health", methods=[apigw.HttpMethod.GET], integration=integration)
        api.add_routes(
            path="/orders/{order_id}", methods=[apigw.HttpMethod.GET], integration=integration
        )
        api.add_routes(path="/orders", methods=[apigw.HttpMethod.POST], integration=integration)

        # --- One hostname, two regions ------------------------------------------------
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self, "Zone", hosted_zone_id=hosted_zone_id, zone_name=zone_name
        )
        api_host = f"api.{zone_name}"
        # A regional certificate in each region (API Gateway needs it where it
        # terminates TLS); both validate through the same zone.
        certificate = acm.Certificate(
            self,
            "ApiCertificate",
            domain_name=api_host,
            validation=acm.CertificateValidation.from_dns(zone),
        )
        domain = apigw.DomainName(self, "ApiDomain", domain_name=api_host, certificate=certificate)
        apigw.ApiMapping(self, "ApiMapping", api=api, domain_name=domain)

        health_check = None
        if primary:
            # Checked from outside the region, against the whole path: the
            # handler reads the table, so a region whose DynamoDB is broken
            # also fails over.
            health_check = route53.HealthCheck(
                self,
                "ApiHealth",
                type=route53.HealthCheckType.HTTPS,
                fqdn=f"{api.api_id}.execute-api.{region}.amazonaws.com",
                resource_path="/health",
                port=443,
                request_interval=HEALTH_INTERVAL,
                failure_threshold=HEALTH_FAILURES,
            )
        # One record name, two answers. Alias records have no TTL of their own:
        # resolvers get the target's, 60 s for API Gateway (DNS_TTL documents
        # it for the drill arithmetic). The L2 alias target cannot set
        # EvaluateTargetHealth, which failover aliases require, hence the L1.
        route53.CfnRecordSet(
            self,
            "ApiRecord",
            hosted_zone_id=hosted_zone_id,
            name=api_host,
            type="A",
            alias_target=route53.CfnRecordSet.AliasTargetProperty(
                dns_name=domain.regional_domain_name,
                hosted_zone_id=domain.regional_hosted_zone_id,
                evaluate_target_health=True,
            ),
            failover="PRIMARY" if primary else "SECONDARY",
            set_identifier=f"{role}-{region}",
            health_check_id=health_check.health_check_id if health_check else None,
        )

        # --- Backups: the thing replication cannot do -----------------------------------
        vault = backup.BackupVault(
            self,
            "Vault",
            backup_vault_name=f"{name_prefix}-vault-{region}",
            removal_policy=data_policy,
        )
        if primary:
            standby_vault = backup.BackupVault.from_backup_vault_arn(
                self,
                "StandbyVault",
                f"arn:aws:backup:{STANDBY_REGION}:{account}:backup-vault:"
                f"{name_prefix}-vault-{STANDBY_REGION}",
            )
            plan = backup.BackupPlan(
                self,
                "DailyPlan",
                backup_plan_name=f"{name_prefix}-daily",
                backup_vault=vault,
                backup_plan_rules=[
                    backup.BackupPlanRule(
                        rule_name="daily-copied-to-standby",
                        schedule_expression=events.Schedule.cron(
                            hour=str(BACKUP_HOUR_UTC), minute="0"
                        ),
                        delete_after=BACKUP_RETENTION,
                        copy_actions=[
                            backup.BackupPlanCopyActionProps(
                                destination_backup_vault=standby_vault,
                                delete_after=BACKUP_RETENTION,
                            )
                        ],
                    )
                ],
            )
            plan.add_selection(
                "Orders", resources=[backup.BackupResource.from_dynamo_db_table(orders)]
            )

        # --- The site: CloudFront is already global; only the origin needed a twin ----
        if primary:
            fallback_site = s3.Bucket.from_bucket_attributes(
                self,
                "SiteFallback",
                bucket_name=bucket_name("site", STANDBY_REGION),
                region=STANDBY_REGION,
            )
            cloudfront.Distribution(
                self,
                "Distribution",
                default_root_object="index.html",
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origins.OriginGroup(
                        primary_origin=origins.S3BucketOrigin.with_origin_access_control(site),
                        fallback_origin=origins.S3BucketOrigin.with_origin_access_control(
                            fallback_site
                        ),
                        fallback_status_codes=[500, 502, 503, 504, 403, 404],
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                ),
                comment="Chapter 5: site with origin failover to the standby bucket",
            )
        else:
            # The distribution does not exist when the standby deploys, so the
            # standby's site bucket admits any distribution of this account.
            site.add_to_resource_policy(
                iam.PolicyStatement(
                    actions=["s3:GetObject"],
                    resources=[site.arn_for_objects("*")],
                    principals=[iam.ServicePrincipal("cloudfront.amazonaws.com")],
                    conditions={
                        "ArnLike": {
                            "AWS:SourceArn": f"arn:aws:cloudfront::{account}:distribution/*"
                        }
                    },
                )
            )

        # --- Alarms: every copy has a way of silently stopping -------------------------
        if primary:
            # Route 53 publishes health-check metrics in us-east-1 only, which
            # is where the primary lives in this course.
            cw.Alarm(
                self,
                "PrimaryUnhealthy",
                metric=cw.Metric(
                    namespace="AWS/Route53",
                    metric_name="HealthCheckStatus",
                    dimensions_map={"HealthCheckId": health_check.health_check_id},
                    period=cdk.Duration.minutes(1),
                    statistic="Minimum",
                ),
                threshold=1,
                comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                evaluation_periods=1,
                treat_missing_data=cw.TreatMissingData.BREACHING,
                alarm_description="Route 53 sees the primary API as unhealthy: DNS has failed over",
            ).add_alarm_action(cw_actions.SnsAction(alerts))
            cw.Alarm(
                self,
                "ReplicationLagging",
                metric=cw.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ReplicationLatency",
                    dimensions_map={"TableName": TABLE_NAME, "ReceivingRegion": STANDBY_REGION},
                    period=cdk.Duration.minutes(1),
                    statistic="Maximum",
                ),
                threshold=REPLICATION_LAG_ALARM.to_milliseconds(),
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=3,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                alarm_description="Orders written here are not reaching the standby",
            ).add_alarm_action(cw_actions.SnsAction(alerts))
            for kind, bucket in (("archive", archive), ("site", site)):
                cw.Alarm(
                    self,
                    f"{kind.title()}ReplicationPending",
                    metric=cw.Metric(
                        namespace="AWS/S3",
                        metric_name="OperationsPendingReplication",
                        dimensions_map={
                            "SourceBucket": bucket.bucket_name,
                            "DestinationBucket": bucket_name(kind, STANDBY_REGION),
                            "RuleId": f"{kind}-to-{STANDBY_REGION}",
                        },
                        period=cdk.Duration.minutes(5),
                        statistic="Maximum",
                    ),
                    threshold=1000,
                    comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                    evaluation_periods=3,
                    treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                    alarm_description=f"{kind} objects are piling up unreplicated",
                ).add_alarm_action(cw_actions.SnsAction(alerts))
            # Chapter 3's rule, again: notice silence. A day with no completed
            # backup is an alarm whether or not a job reported failure.
            cw.Alarm(
                self,
                "NoBackupToday",
                metric=cw.Metric(
                    namespace="AWS/Backup",
                    metric_name="NumberOfBackupJobsCompleted",
                    dimensions_map={"BackupVaultName": vault.backup_vault_name},
                    period=cdk.Duration.days(1),
                    statistic="Sum",
                ),
                threshold=1,
                comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                evaluation_periods=1,
                treat_missing_data=cw.TreatMissingData.BREACHING,
                alarm_description="No backup of the orders table completed in the last day",
            ).add_alarm_action(cw_actions.SnsAction(alerts))
            events.Rule(
                self,
                "BackupOrCopyFailed",
                event_pattern=events.EventPattern(
                    source=["aws.backup"],
                    detail_type=["Backup Job State Change", "Copy Job State Change"],
                    detail={"state": ["FAILED", "ABORTED", "EXPIRED"]},
                ),
                targets=[targets.SnsTopic(alerts)],
            )

        cdk.CfnOutput(self, "Role", value=role)
        cdk.CfnOutput(self, "ApiUrl", value=api.url or "")
        cdk.CfnOutput(self, "ApiHostname", value=api_host)
        cdk.CfnOutput(self, "ArchiveBucket", value=archive.bucket_name)
        cdk.CfnOutput(self, "SiteBucket", value=site.bucket_name)
        cdk.CfnOutput(self, "VaultName", value=vault.backup_vault_name)
        cdk.CfnOutput(self, "AlertsTopic", value=alerts.topic_arn)
