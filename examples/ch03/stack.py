"""Chapter 3 reference architecture: batch ML training and inference on a schedule.

Every night a schedule starts one Step Functions execution. It runs the
training task on Fargate (reads the chapter 2 archive, writes a candidate
model and its metrics), asks the gate function whether the candidate beats
the current model on the same hold-out week, runs the scoring task with
whatever model is current, and publishes a summary. Any failure goes to the
same SNS topic, and an alarm fires if a day passes without a successful
execution, so a schedule that silently stops is noticed too.

Read top to bottom; the chapter walks the same order.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sns as sns
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

HERE = Path(__file__).parent

TRAINING_DAYS = 90
SCHEDULE_HOUR_UTC = 2  # 02:00 UTC: the previous day's archive objects are all written by then
TRAIN_TIMEOUT = cdk.Duration.minutes(90)
SCORE_TIMEOUT = cdk.Duration.minutes(30)
TASK_CPU = 1024  # 1 vCPU
TASK_MEMORY_MIB = 4096


class BatchMlStack(cdk.Stack):
    """Scheduler -> Step Functions -> {Fargate train, Lambda gate, Fargate score} -> SNS.

    ``archive_bucket_name`` points at chapter 2's archive bucket. Without it
    the stack creates its own, so the chapter can be deployed on its own and
    fed with the synthetic archive from ``pipeline/synth_archive.py``.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        archive_bucket_name: str | None = None,
        retain_data: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        data_policy = cdk.RemovalPolicy.RETAIN if retain_data else cdk.RemovalPolicy.DESTROY

        # --- Where the data lives ------------------------------------------------
        # The archive is chapter 2's bucket (raw/dt=YYYY-MM-DD/*.gz). Models and
        # predictions go to a second bucket: a different lifetime (candidates
        # expire, the archive never does) and a different writer.
        if archive_bucket_name:
            archive = s3.Bucket.from_bucket_name(self, "Archive", archive_bucket_name)
        else:
            archive = s3.Bucket(
                self,
                "Archive",
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                encryption=s3.BucketEncryption.S3_MANAGED,
                enforce_ssl=True,
                removal_policy=data_policy,
                auto_delete_objects=not retain_data,
            )
        artifacts = s3.Bucket(
            self,
            "Artifacts",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,  # models/current/ keeps every model that was ever live
            removal_policy=data_policy,
            auto_delete_objects=not retain_data,
            lifecycle_rules=[
                s3.LifecycleRule(prefix="models/candidate/", expiration=cdk.Duration.days(30)),
                s3.LifecycleRule(
                    prefix="models/current/", noncurrent_version_expiration=cdk.Duration.days(365)
                ),
            ],
        )
        # One item per device, overwritten each morning: what chapter 1's page
        # reads next to the live status. ``dt`` says which morning it is from,
        # so a stale forecast can be shown as stale rather than as today's.
        predictions = dynamodb.TableV2(
            self,
            "Predictions",
            partition_key=dynamodb.Attribute(name="device", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            removal_policy=data_policy,
        )
        alerts = sns.Topic(self, "Alerts")

        # --- Compute: one container, two commands --------------------------------
        # Fargate needs a VPC. Public subnets with no NAT gateway: the task
        # gets a public IP for the image pull and S3 goes through a gateway
        # endpoint, so the network costs nothing by the hour and the security
        # group allows no inbound traffic at all.
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC)
            ],
        )
        vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)
        vpc.add_gateway_endpoint("DynamoDB", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB)
        cluster = ecs.Cluster(
            self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.DISABLED
        )
        task_logs = logs.LogGroup(
            self,
            "TaskLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        task_def = ecs.FargateTaskDefinition(
            self,
            "PipelineTask",
            cpu=TASK_CPU,
            memory_limit_mib=TASK_MEMORY_MIB,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        container = task_def.add_container(
            "pipeline",
            # pipeline/Dockerfile; built and pushed to ECR by `cdk deploy`.
            image=ecs.ContainerImage.from_asset(
                str(HERE / "pipeline"), platform=ecr_assets.Platform.LINUX_ARM64
            ),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="pipeline", log_group=task_logs),
            environment={
                "ARCHIVE_ROOT": f"s3://{archive.bucket_name}",
                "ARTIFACTS_ROOT": f"s3://{artifacts.bucket_name}",
                "PREDICTIONS_TABLE": predictions.table_name,
                "TRAINING_DAYS": str(TRAINING_DAYS),
            },
        )
        archive.grant_read(task_def.task_role)
        artifacts.grant_read_write(task_def.task_role)
        predictions.grant_write_data(task_def.task_role)

        gate_logs = logs.LogGroup(
            self,
            "GateLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        gate = lambda_.Function(
            self,
            "GateFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="gate.lambda_handler",
            code=lambda_.Code.from_asset(str(HERE / "lambda")),
            memory_size=256,
            timeout=cdk.Duration.seconds(30),
            environment={"ARTIFACTS_BUCKET": artifacts.bucket_name},
            log_group=gate_logs,
        )
        artifacts.grant_read_write(gate)

        # --- The workflow ---------------------------------------------------------
        # The run id is the execution's date: keys in S3 and items in DynamoDB
        # carry it, so a second run on the same day overwrites rather than
        # duplicates, and a human can see which night produced what.
        run_id = sfn.Pass(
            self,
            "RunId",
            parameters={
                "run_id.$": (
                    "States.ArrayGetItem(States.StringSplit($$.Execution.StartTime, 'T'), 0)"
                )
            },
        )

        def run_task(name: str, command: str, timeout: cdk.Duration) -> tasks.EcsRunTask:
            # RUN_JOB: the state waits for the task to stop and fails if the
            # container exits non-zero. Without it the state machine would
            # move on the moment the task was *started*.
            return tasks.EcsRunTask(
                self,
                name,
                integration_pattern=sfn.IntegrationPattern.RUN_JOB,
                cluster=cluster,
                task_definition=task_def,
                launch_target=tasks.EcsFargateLaunchTarget(
                    platform_version=ecs.FargatePlatformVersion.LATEST
                ),
                assign_public_ip=True,  # no NAT: this is how the image pull reaches ECR
                container_overrides=[
                    tasks.ContainerOverride(
                        container_definition=container,
                        command=[command],
                        environment=[
                            tasks.TaskEnvironmentVariable(
                                name="RUN_ID", value=sfn.JsonPath.string_at("$.run_id")
                            )
                        ],
                    )
                ],
                task_timeout=sfn.Timeout.duration(timeout),
                result_path=sfn.JsonPath.DISCARD,  # the task's result is in S3, not in state
            )

        train = run_task("Train", "train", TRAIN_TIMEOUT)
        # Capacity errors are worth one retry; a crashing container is not.
        train.add_retry(
            errors=["ECS.AmazonECSException"], interval=cdk.Duration.minutes(2), max_attempts=2
        )
        gate_step = tasks.LambdaInvoke(
            self,
            "Gate",
            lambda_function=gate,
            payload=sfn.TaskInput.from_object({"run_id": sfn.JsonPath.string_at("$.run_id")}),
            result_selector={"gate.$": "$.Payload"},
            result_path="$.result",
        )
        score = run_task("Score", "score", SCORE_TIMEOUT)
        notify = tasks.SnsPublish(
            self,
            "Notify",
            topic=alerts,
            subject="Nightly forecast: done",
            # SNS wants a string; the gate's result is an object, so stringify it.
            message=sfn.TaskInput.from_text(
                sfn.JsonPath.json_to_string(sfn.JsonPath.object_at("$.result.gate"))
            ),
        )
        no_model = sfn.Fail(
            self,
            "NoModel",
            error="NoModel",
            cause="No candidate was ever promoted; nothing to score with",
        )
        notify_failure = tasks.SnsPublish(
            self,
            "NotifyFailure",
            topic=alerts,
            subject="Nightly forecast: FAILED",
            message=sfn.TaskInput.from_text(
                sfn.JsonPath.json_to_string(sfn.JsonPath.object_at("$.error"))
            ),
        ).next(sfn.Fail(self, "Failed", error="NightlyRunFailed"))

        chain = (
            run_id.next(train)
            .next(gate_step)
            .next(
                sfn.Choice(self, "ModelAvailable?")
                .when(
                    sfn.Condition.boolean_equals("$.result.gate.model_available", True),
                    score.next(notify),
                )
                .otherwise(no_model)
            )
        )
        for step in (train, gate_step, score):
            step.add_catch(notify_failure, errors=["States.ALL"], result_path="$.error")
        machine_logs = logs.LogGroup(
            self,
            "MachineLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        machine = sfn.StateMachine(
            self,
            "Nightly",
            definition_body=sfn.DefinitionBody.from_chainable(chain),
            state_machine_type=sfn.StateMachineType.STANDARD,  # minutes to hours, exactly-once
            timeout=cdk.Duration.hours(3),
            logs=sfn.LogOptions(destination=machine_logs, level=sfn.LogLevel.ERROR),
        )

        # --- The trigger ------------------------------------------------------------
        scheduler.Schedule(
            self,
            "EveryNight",
            schedule=scheduler.ScheduleExpression.cron(
                minute="0", hour=str(SCHEDULE_HOUR_UTC), time_zone=cdk.TimeZone.ETC_UTC
            ),
            target=scheduler_targets.StepFunctionsStartExecution(machine),
            description="Start the nightly train-gate-score run",
        )

        # --- Observability: two alarms ------------------------------------------------
        # "A run failed" is the obvious one. "No run succeeded today" is the
        # one that catches a disabled schedule, a broken trigger or a run
        # that hangs: missing data is treated as breaching, as in chapter 2.
        failed = cw.Alarm(
            self,
            "RunFailedAlarm",
            metric=machine.metric_failed(period=cdk.Duration.hours(1), statistic="Sum"),
            threshold=0,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="A nightly run failed, timed out or was aborted",
        )
        no_success = cw.Alarm(
            self,
            "NoSuccessTodayAlarm",
            metric=machine.metric_succeeded(period=cdk.Duration.days(1), statistic="Sum"),
            threshold=1,
            comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.BREACHING,
            alarm_description="No nightly run succeeded in the last day",
        )
        for alarm in (failed, no_success):
            alarm.add_alarm_action(cw_actions.SnsAction(alerts))

        cdk.CfnOutput(self, "ArchiveBucket", value=archive.bucket_name)
        cdk.CfnOutput(self, "ArtifactsBucket", value=artifacts.bucket_name)
        cdk.CfnOutput(self, "PredictionsTable", value=predictions.table_name)
        cdk.CfnOutput(self, "StateMachineArn", value=machine.state_machine_arn)
        cdk.CfnOutput(self, "AlertsTopic", value=alerts.topic_arn)
