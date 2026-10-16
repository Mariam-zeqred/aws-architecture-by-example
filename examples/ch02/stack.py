"""Chapter 2 reference architecture: IoT telemetry at scale.

Devices connect to AWS IoT Core over MQTT with their own X.509 certificate
and publish readings on a Basic Ingest topic. One topic rule fans each
message out to a hot path (Lambda -> two DynamoDB tables: latest status and
30-day history) and a cold path (Firehose -> S3, queried by Athena through
a Glue table). A scheduled Lambda scans the status table for silent devices
and publishes one fleet metric, so there are three alarms in total, however
many devices there are.

Every resource that holds data or costs money by the hour is called out
with a comment that says why it is configured the way it is. Read top to
bottom; the chapter walks the same order.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_athena as athena
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_iot as iot
from aws_cdk import aws_kinesisfirehose as firehose
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

HERE = Path(__file__).parent

RULE_NAME = "FarmTelemetry"  # part of the Basic Ingest topic, so it is fixed here
TOPIC_PREFIX = (
    "farm/telemetry"  # devices publish to $aws/rules/FarmTelemetry/farm/telemetry/<thing>
)
STALE_AFTER_S = 600  # a device that has not reported for 10 minutes is "silent"
HISTORY_DAYS = 30


class TelemetryStack(cdk.Stack):
    """IoT Core -> rule -> {Lambda -> DynamoDB, Firehose -> S3 -> Athena}.

    ``thing_names`` creates registry entries for a few devices so the demo
    has something to attach certificates to; a real fleet uses fleet
    provisioning instead (section 4 of the chapter). ``retain_data`` keeps
    the tables and the archive bucket on ``cdk destroy``.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        thing_names: tuple[str, ...] = ("field-1", "field-2"),
        retain_data: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        data_policy = cdk.RemovalPolicy.RETAIN if retain_data else cdk.RemovalPolicy.DESTROY

        # --- Hot storage: two tables, two questions ----------------------------
        # "What is device X doing now?" is one item per device, overwritten.
        # "What did device X do this month?" is one item per reading, expiring.
        # One table could hold both, but they have different sizes, different
        # TTLs and different readers, and the page from chapter 1 only ever
        # needs the first. On-demand billing is right until the fleet is large
        # and flat; section 5 prices the switch to provisioned capacity.
        status = dynamodb.TableV2(
            self,
            "Status",
            partition_key=dynamodb.Attribute(name="device", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=data_policy,
        )
        history = dynamodb.TableV2(
            self,
            "History",
            partition_key=dynamodb.Attribute(name="device", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="ts", type=dynamodb.AttributeType.NUMBER),
            billing=dynamodb.Billing.on_demand(),
            time_to_live_attribute="expires",  # set by the ingest function: ts + 30 days
            removal_policy=data_policy,
        )

        # --- Rejects: a queue, not /dev/null ------------------------------------
        # A reading that fails validation is not written, but it is not lost
        # either: the function sends it here with a reason, and an alarm fires
        # if the queue grows. A fleet-wide firmware bug shows up as a reason
        # count, not as a silent gap in the data.
        rejects = sqs.Queue(
            self,
            "Rejects",
            retention_period=cdk.Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )

        # --- Hot path: the ingest function -------------------------------------
        ingest_logs = logs.LogGroup(
            self,
            "IngestLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        ingest = lambda_.Function(
            self,
            "Ingest",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="ingest.lambda_handler",
            code=lambda_.Code.from_asset(str(HERE / "lambda")),
            memory_size=256,
            timeout=cdk.Duration.seconds(10),
            environment={
                "STATUS_TABLE": status.table_name,
                "HISTORY_TABLE": history.table_name,
                "REJECTS_URL": rejects.queue_url,
                "HISTORY_DAYS": str(HISTORY_DAYS),
            },
            log_group=ingest_logs,
        )
        status.grant_write_data(ingest)
        history.grant_write_data(ingest)
        rejects.grant_send_messages(ingest)
        # The rules engine invokes the function directly, not through API
        # Gateway: no HTTP, no throttle to tune, one permission statement.
        ingest.add_permission(
            "IotInvoke",
            principal=iam.ServicePrincipal("iot.amazonaws.com"),
            source_arn=self.format_arn(service="iot", resource="rule", resource_name=RULE_NAME),
        )

        # --- Cold path: Firehose -> S3, Glue table, Athena ----------------------
        # The archive keeps every raw message for years, compressed, under a
        # date prefix. Firehose batches messages into one object every five
        # minutes or 64 MB, whichever comes first: small objects are the
        # enemy of both S3 cost and Athena speed.
        archive = s3.Bucket(
            self,
            "Archive",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=data_policy,
            auto_delete_objects=not retain_data,
            lifecycle_rules=[
                s3.LifecycleRule(
                    # Agronomists query the last few months; older data is
                    # read rarely, so it moves to a cheaper class by itself.
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=cdk.Duration.days(90),
                        )
                    ]
                )
            ],
        )
        firehose_role = iam.Role(
            self, "FirehoseRole", assumed_by=iam.ServicePrincipal("firehose.amazonaws.com")
        )
        archive.grant_read_write(firehose_role)
        firehose_logs = logs.LogGroup(
            self,
            "FirehoseLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        firehose_stream_log = firehose_logs.add_stream("S3Delivery")
        firehose_logs.grant_write(firehose_role)
        stream = firehose.CfnDeliveryStream(
            self,
            "Stream",
            delivery_stream_type="DirectPut",
            extended_s3_destination_configuration=firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
                bucket_arn=archive.bucket_arn,
                role_arn=firehose_role.role_arn,
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                    interval_in_seconds=300, size_in_m_bs=64
                ),
                compression_format="GZIP",
                # Hive-style prefix so Athena can prune by day; failed records
                # land under their own prefix instead of vanishing.
                prefix="raw/dt=!{timestamp:yyyy-MM-dd}/",
                error_output_prefix="errors/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/",
                cloud_watch_logging_options=firehose.CfnDeliveryStream.CloudWatchLoggingOptionsProperty(
                    enabled=True,
                    log_group_name=firehose_logs.log_group_name,
                    log_stream_name=firehose_stream_log.log_stream_name,
                ),
            ),
        )
        stream.node.add_dependency(firehose_role)

        database = glue.CfnDatabase(
            self,
            "Database",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(name="farm_telemetry"),
        )
        # One table over the raw prefix, JSON rows, one partition per day.
        # Partition projection means no crawler and no ALTER TABLE per day:
        # Athena computes the dt values from the range in the properties.
        table = glue.CfnTable(
            self,
            "RawTable",
            catalog_id=self.account,
            database_name="farm_telemetry",
            table_input=glue.CfnTable.TableInputProperty(
                name="raw",
                table_type="EXTERNAL_TABLE",
                parameters={
                    "classification": "json",
                    "projection.enabled": "true",
                    "projection.dt.type": "date",
                    "projection.dt.range": "2026-01-01,NOW",
                    "projection.dt.format": "yyyy-MM-dd",
                    "storage.location.template": f"s3://{archive.bucket_name}/raw/dt=${{dt}}/",
                },
                partition_keys=[glue.CfnTable.ColumnProperty(name="dt", type="string")],
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{archive.bucket_name}/raw/",
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.openx.data.jsonserde.JsonSerDe"
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="device", type="string"),
                        glue.CfnTable.ColumnProperty(name="ts", type="bigint"),
                        glue.CfnTable.ColumnProperty(name="received", type="bigint"),
                        glue.CfnTable.ColumnProperty(name="temp", type="double"),
                        glue.CfnTable.ColumnProperty(name="hum", type="double"),
                    ],
                ),
            ),
        )
        table.node.add_dependency(database)
        query_results = s3.Bucket(
            self,
            "QueryResults",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=cdk.Duration.days(7))],
        )
        athena.CfnWorkGroup(
            self,
            "Workgroup",
            name="farm-telemetry",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{query_results.bucket_name}/"
                ),
                enforce_work_group_configuration=True,
                # A fuse: Athena bills per TB scanned, and a query without a
                # dt filter scans the whole archive. 10 GB caps that at 5 cents.
                bytes_scanned_cutoff_per_query=10 * 1024**3,
                publish_cloud_watch_metrics_enabled=True,
            ),
        )

        # --- The rule: one message in, two actions out --------------------------
        rule_role = iam.Role(self, "RuleRole", assumed_by=iam.ServicePrincipal("iot.amazonaws.com"))
        rule_role.add_to_policy(
            iam.PolicyStatement(
                actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
                resources=[stream.attr_arn],
            )
        )
        rejects.grant_send_messages(rule_role)
        rule = iot.CfnTopicRule(
            self,
            "Rule",
            rule_name=RULE_NAME,
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                # Basic Ingest strips the $aws/rules/<name> prefix before the
                # SQL sees the topic, so topic(3) is the thing name and the
                # device cannot claim another identity in its payload.
                sql=(
                    f"SELECT *, topic(3) AS device, timestamp() AS received FROM '{TOPIC_PREFIX}/+'"
                ),
                aws_iot_sql_version="2016-03-23",
                rule_disabled=False,
                actions=[
                    iot.CfnTopicRule.ActionProperty(
                        lambda_=iot.CfnTopicRule.LambdaActionProperty(
                            function_arn=ingest.function_arn
                        )
                    ),
                    iot.CfnTopicRule.ActionProperty(
                        firehose=iot.CfnTopicRule.FirehoseActionProperty(
                            delivery_stream_name=stream.ref,
                            role_arn=rule_role.role_arn,
                            separator="\n",  # one JSON object per line, what the SerDe expects
                        )
                    ),
                ],
                # If an action fails (function throttled, stream full), the
                # message goes to the rejects queue with the error attached
                # instead of being dropped.
                error_action=iot.CfnTopicRule.ActionProperty(
                    sqs=iot.CfnTopicRule.SqsActionProperty(
                        queue_url=rejects.queue_url, role_arn=rule_role.role_arn, use_base64=False
                    )
                ),
            ),
        )

        # --- Identity: one policy for the whole fleet, scoped per thing ---------
        # The policy variables make every certificate able to do exactly one
        # thing: connect as the thing it is attached to and publish readings
        # for that thing. A stolen certificate cannot impersonate a neighbour.
        topic_arn = self.format_arn(
            service="iot",
            resource="topic",
            resource_name=(
                f"$aws/rules/{RULE_NAME}/{TOPIC_PREFIX}/${{iot:Connection.Thing.ThingName}}"
            ),
            arn_format=cdk.ArnFormat.SLASH_RESOURCE_NAME,
        )
        client_arn = self.format_arn(
            service="iot",
            resource="client",
            resource_name="${iot:Connection.Thing.ThingName}",
            arn_format=cdk.ArnFormat.SLASH_RESOURCE_NAME,
        )
        device_policy = iot.CfnPolicy(
            self,
            "DevicePolicy",
            policy_name="farm-monitor",
            policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "iot:Connect",
                        "Resource": client_arn,
                        # Only certificates attached to a registered thing may connect.
                        "Condition": {"Bool": {"iot:Connection.Thing.IsAttached": "true"}},
                    },
                    {"Effect": "Allow", "Action": "iot:Publish", "Resource": topic_arn},
                ],
            },
        )
        device_policy.node.add_dependency(rule)
        for name in thing_names:
            iot.CfnThing(self, f"Thing-{name}", thing_name=name)
        # Certificates are created at deploy time (README), attached to the
        # thing and to this policy; they are never written into a template.

        # --- Observability: three alarms, whatever the fleet size ---------------
        alerts = sns.Topic(self, "Alerts")
        scan_logs = logs.LogGroup(
            self,
            "ScanLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        scan = lambda_.Function(
            self,
            "StaleScan",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="stale_scan.lambda_handler",
            code=lambda_.Code.from_asset(str(HERE / "lambda")),
            memory_size=256,
            timeout=cdk.Duration.minutes(5),  # a 100 000-item scan takes a while
            environment={
                "STATUS_TABLE": status.table_name,
                "STALE_AFTER_S": str(STALE_AFTER_S),
                "METRIC_NAMESPACE": "FarmTelemetry",
            },
            log_group=scan_logs,
        )
        status.grant_read_data(scan)
        scan.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],  # PutMetricData does not support resource-level permissions
                conditions={"StringEquals": {"cloudwatch:namespace": "FarmTelemetry"}},
            )
        )
        events.Rule(
            self,
            "ScanEvery5Min",
            schedule=events.Schedule.rate(cdk.Duration.minutes(5)),
            targets=[targets.LambdaFunction(scan)],
        )
        # One metric for the fleet instead of one alarm per device: at 100 000
        # devices that is $0.40 a month instead of about $22 000 (cost.py).
        stale_metric = cw.Metric(
            namespace="FarmTelemetry",
            metric_name="StaleDevices",
            statistic="Maximum",
            period=cdk.Duration.minutes(5),
        )
        alarms = [
            cw.Alarm(
                self,
                "StaleDevicesAlarm",
                metric=stale_metric,
                threshold=0,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=2,  # two scans, so one late message does not page anyone
                treat_missing_data=cw.TreatMissingData.BREACHING,  # no scan ran = alarm
                alarm_description="At least one device has not reported for 10 minutes",
            ),
            cw.Alarm(
                self,
                "RejectsAlarm",
                metric=rejects.metric_approximate_number_of_messages_visible(
                    period=cdk.Duration.minutes(5), statistic="Maximum"
                ),
                threshold=10,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=1,
                alarm_description="More than 10 rejected or failed messages waiting",
            ),
            cw.Alarm(
                self,
                "IngestErrorsAlarm",
                metric=ingest.metric_errors(period=cdk.Duration.minutes(5)),
                threshold=0,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=1,
                alarm_description="The ingest function raised; its rule messages went to rejects",
            ),
        ]
        for alarm in alarms:
            alarm.add_alarm_action(cw_actions.SnsAction(alerts))

        cdk.CfnOutput(self, "StatusTable", value=status.table_name)
        cdk.CfnOutput(self, "HistoryTable", value=history.table_name)
        cdk.CfnOutput(self, "ArchiveBucket", value=archive.bucket_name)
        cdk.CfnOutput(self, "RejectsQueue", value=rejects.queue_url)
        cdk.CfnOutput(self, "AlertsTopic", value=alerts.topic_arn)
        cdk.CfnOutput(
            self,
            "PublishTopicForField1",
            value=f"$aws/rules/{RULE_NAME}/{TOPIC_PREFIX}/{thing_names[0]}",
        )
