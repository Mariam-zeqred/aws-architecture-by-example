"""Chapter 4 reference architecture: event-driven order processing.

``POST /orders`` -> accept function -> orders table + ``OrderPlaced`` on an
EventBridge bus. Rules route each event to an SQS queue owned by one
consumer: ``OrderPlaced`` to the payment queue (charge function),
``OrderPaid`` to the fulfilment queue (fulfil function), and the two
"a person must look" events to an SNS topic. Every queue has a dead-letter
queue and a redrive policy; every rule target has a dead-letter queue for
deliveries EventBridge itself could not make; every dead-letter queue has
an alarm; the bus has an archive so events can be replayed after a fix.

Read top to bottom; the chapter walks the same order.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

HERE = Path(__file__).parent
SOURCE = "coop.orders"  # the ``source`` field of every event; must match orders_common.SOURCE

MAX_RECEIVE_COUNT = 5  # deliveries before dead-lettering; Lambda docs say at least 5
BATCH_SIZE = 10
CONSUMER_TIMEOUT = cdk.Duration.seconds(30)
DLQ_RETENTION = cdk.Duration.days(14)  # the SQS maximum: time for a person to look
ARCHIVE_RETENTION = cdk.Duration.days(30)
STUCK_AFTER = cdk.Duration.minutes(15)  # oldest message older than this: consumer is stuck


class OrdersStack(cdk.Stack):
    """HTTP API -> accept -> EventBridge bus -> {SQS -> charge, SQS -> fulfil, SNS}."""

    def __init__(
        self, scope: Construct, construct_id: str, *, retain_data: bool = False, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        data_policy = cdk.RemovalPolicy.RETAIN if retain_data else cdk.RemovalPolicy.DESTROY

        # --- State ----------------------------------------------------------------
        # One item per order. ``status`` only moves forward (orders_common.NEXT),
        # and every move is a conditional update: the item is the idempotency
        # record for every step, so there is no second table to keep in step.
        orders = dynamodb.TableV2(
            self,
            "Orders",
            partition_key=dynamodb.Attribute(name="order_id", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=data_policy,
        )
        # One item per SKU with ``available``; decremented inside a transaction
        # with the order's status move, so it can never be taken twice or
        # taken without the order moving.
        stock = dynamodb.TableV2(
            self,
            "Stock",
            partition_key=dynamodb.Attribute(name="sku", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            removal_policy=data_policy,
        )
        alerts = sns.Topic(self, "Alerts")

        # --- The bus ----------------------------------------------------------------
        bus = events.EventBus(self, "Bus")
        # Every event the shop ever published, for 30 days, replayable into
        # the same rules: the recovery tool for "the consumer had a bug".
        bus.archive(
            "Archive",
            event_pattern=events.EventPattern(source=[SOURCE]),
            retention=ARCHIVE_RETENTION,
        )
        # Where EventBridge puts an event it could not deliver to a target
        # (the queue's policy was wrong, the target was deleted). Different
        # from the consumers' DLQs: nothing ever *received* these.
        rule_dlq = sqs.Queue(
            self,
            "RuleDeadLetters",
            retention_period=DLQ_RETENTION,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )

        def function(name: str, handler: str, **env: str) -> lambda_.Function:
            fn = lambda_.Function(
                self,
                name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                handler=handler,
                code=lambda_.Code.from_asset(str(HERE / "lambda")),
                memory_size=256,
                timeout=CONSUMER_TIMEOUT,
                environment={"BUS_NAME": bus.event_bus_name, **env},
                log_group=logs.LogGroup(
                    self,
                    f"{name}Logs",
                    retention=logs.RetentionDays.ONE_MONTH,
                    removal_policy=cdk.RemovalPolicy.DESTROY,
                ),
            )
            bus.grant_put_events_to(fn)
            return fn

        def queue(name: str) -> tuple[sqs.Queue, sqs.Queue]:
            """A consumer's queue and its dead-letter queue."""
            dlq = sqs.Queue(
                self,
                f"{name}DeadLetters",
                retention_period=DLQ_RETENTION,
                encryption=sqs.QueueEncryption.SQS_MANAGED,
                enforce_ssl=True,
            )
            main = sqs.Queue(
                self,
                f"{name}Queue",
                # Lambda's rule of thumb: at least six times the function timeout,
                # so a slow invocation cannot let the same message out twice.
                visibility_timeout=cdk.Duration.seconds(CONSUMER_TIMEOUT.to_seconds() * 6),
                retention_period=cdk.Duration.days(4),
                dead_letter_queue=sqs.DeadLetterQueue(
                    max_receive_count=MAX_RECEIVE_COUNT, queue=dlq
                ),
                encryption=sqs.QueueEncryption.SQS_MANAGED,
                enforce_ssl=True,
            )
            return main, dlq

        def route(name: str, detail_types: list[str], target: events.IRuleTarget) -> events.Rule:
            return events.Rule(
                self,
                name,
                event_bus=bus,
                event_pattern=events.EventPattern(source=[SOURCE], detail_type=detail_types),
                targets=[target],
            )

        def to_queue(q: sqs.Queue) -> targets.SqsQueue:
            # EventBridge retries a failed delivery for up to 24 hours, then
            # sends the event to the rule DLQ instead of dropping it.
            return targets.SqsQueue(
                q,
                dead_letter_queue=rule_dlq,
                max_event_age=cdk.Duration.hours(24),
                retry_attempts=185,
            )

        # --- Accept: the only writer of new orders --------------------------------
        accept = function("Accept", "accept.lambda_handler", ORDERS_TABLE=orders.table_name)
        orders.grant_read_write_data(accept)
        api = apigw.HttpApi(self, "Api", description="Chapter 4: POST /orders")
        api.add_routes(
            path="/orders",
            methods=[apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("AcceptIntegration", accept),
        )

        # --- Charge: OrderPlaced -> payment queue -> charge function -----------------
        payment, payment_dlq = queue("Payment")
        route("OrderPlacedToPayment", ["OrderPlaced"], to_queue(payment))
        charge = function("Charge", "charge.lambda_handler", ORDERS_TABLE=orders.table_name)
        orders.grant_read_write_data(charge)
        charge.add_event_source(
            sources.SqsEventSource(
                payment,
                batch_size=BATCH_SIZE,
                # The function returns the ids that failed; the rest are
                # deleted. Without this one bad record would redeliver ten.
                report_batch_item_failures=True,
            )
        )

        # --- Fulfil: OrderPaid -> fulfilment queue -> fulfil function ----------------
        fulfilment, fulfilment_dlq = queue("Fulfilment")
        route("OrderPaidToFulfilment", ["OrderPaid"], to_queue(fulfilment))
        fulfil = function(
            "Fulfil",
            "fulfil.lambda_handler",
            ORDERS_TABLE=orders.table_name,
            STOCK_TABLE=stock.table_name,
        )
        orders.grant_read_write_data(fulfil)
        stock.grant_read_write_data(fulfil)
        fulfil.add_event_source(
            sources.SqsEventSource(
                fulfilment, batch_size=BATCH_SIZE, report_batch_item_failures=True
            )
        )

        # --- A person must look: two events straight to email ------------------------
        route(
            "NeedsAPerson",
            ["PaymentFailed", "OrderBackordered"],
            targets.SnsTopic(
                alerts,
                message=events.RuleTargetInput.from_text(
                    f"{events.EventField.from_path('$.detail-type')}: order "
                    f"{events.EventField.from_path('$.detail.order_id')}"
                ),
            ),
        )

        # --- Observability: a dead-letter queue nobody watches is a bin ---------------
        for name, dlq in (
            ("PaymentDlq", payment_dlq),
            ("FulfilmentDlq", fulfilment_dlq),
            ("RuleDlq", rule_dlq),
        ):
            cw.Alarm(
                self,
                f"{name}NotEmpty",
                metric=dlq.metric_approximate_number_of_messages_visible(
                    period=cdk.Duration.minutes(5), statistic="Maximum"
                ),
                threshold=0,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=1,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                alarm_description=f"{name}: a message nobody could process is waiting",
            ).add_alarm_action(cw_actions.SnsAction(alerts))
        # A consumer that is disabled, throttled or always failing shows up as
        # the oldest message in its queue getting older, with nothing in the DLQ.
        for name, q in (("Payment", payment), ("Fulfilment", fulfilment)):
            cw.Alarm(
                self,
                f"{name}Stuck",
                metric=q.metric_approximate_age_of_oldest_message(
                    period=cdk.Duration.minutes(5), statistic="Maximum"
                ),
                threshold=STUCK_AFTER.to_seconds(),
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=1,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                alarm_description=f"{name} queue: the oldest message is not being consumed",
            ).add_alarm_action(cw_actions.SnsAction(alerts))

        cdk.CfnOutput(self, "ApiUrl", value=api.url or "")
        cdk.CfnOutput(self, "OrdersTable", value=orders.table_name)
        cdk.CfnOutput(self, "StockTable", value=stock.table_name)
        cdk.CfnOutput(self, "BusName", value=bus.event_bus_name)
        cdk.CfnOutput(self, "PaymentDlqUrl", value=payment_dlq.queue_url)
        cdk.CfnOutput(self, "FulfilmentDlqUrl", value=fulfilment_dlq.queue_url)
        cdk.CfnOutput(self, "RuleDlqUrl", value=rule_dlq.queue_url)
        cdk.CfnOutput(self, "AlertsTopic", value=alerts.topic_arn)
