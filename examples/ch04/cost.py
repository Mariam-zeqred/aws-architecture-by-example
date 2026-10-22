"""Monthly cost model for the chapter 4 architecture.

Usage: ``python cost.py`` prints the chapter's two tables: the reference
design at 2 000, 100 000 and 10 000 000 orders a month, and what the same
orders would cost through other plumbing (SQS FIFO, Step Functions Standard
and Express). Every assumption is a named field of ``Scenario``. The one
that matters at small volume, how often Lambda's pollers ask an empty
queue for messages, is a documented minimum, not a measurement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PRICES = json.loads((Path(__file__).parent / "prices.json").read_text())
DAYS = 30
SECONDS_PER_MONTH = DAYS * 86_400


@dataclass(frozen=True)
class Scenario:
    name: str
    orders: int
    client_retry_rate: float = 0.02  # POSTs repeated with the same order_id
    duplicate_delivery_rate: float = 0.01  # SQS standard hands a message out twice
    failed_or_short_rate: float = 0.05  # orders a person is emailed about
    lines_per_order: int = 2  # SKUs touched by the fulfilment transaction
    # Lambda
    accept_ms: int = 50
    charge_ms: int = 300  # one HTTPS call to the provider
    fulfil_ms: int = 100
    memory_mb: int = 256
    avg_batch: int = 1  # messages per consumer invocation (1 when traffic is sparse)
    # SQS: Lambda keeps at least this many long polls (20 s) open per queue.
    # Documented minimum for standard queues; assumed to run all month.
    idle_pollers_per_queue: int = 5
    long_poll_s: int = 20
    consumer_queues: int = 2
    # Events: OrderPlaced, OrderPaid|PaymentFailed, OrderReserved|OrderBackordered,
    # plus the re-announcements duplicates cause.
    event_bytes: int = 600
    # Storage
    order_item_bytes: int = 700
    orders_kept_days: int = 365
    log_bytes_per_order: int = 400
    alarms: int = 5


SCENARIOS = [
    Scenario("2 000 orders", orders=2_000),
    Scenario("100 000 orders", orders=100_000, avg_batch=2),
    Scenario("10 000 000 orders", orders=10_000_000, avg_batch=10),
]


def tiered(quantity: float, tiers: list[list]) -> float:
    total, lower = 0.0, 0.0
    for upper, price in tiers:
        band = quantity - lower if upper is None else min(quantity, upper) - lower
        if band <= 0:
            break
        total += band * price
        lower = upper
    return total


def lambda_cost(invocations: float, ms: int, memory_mb: int) -> float:
    lam = PRICES["lambda"]
    gb_s = invocations * ms / 1000 * memory_mb / 1024
    return invocations / 1e6 * lam["requests_per_million"] + gb_s * lam["gb_second_arm_tier1"]


def counts(s: Scenario) -> dict[str, float]:
    """How many of everything one month produces; the cost lines are priced from these."""
    posts = s.orders * (1 + s.client_retry_rate)
    messages = s.orders * (1 + s.duplicate_delivery_rate)  # per consumer queue
    events = (
        posts  # OrderPlaced, once per accepted POST (a repeat re-announces)
        + messages  # OrderPaid or PaymentFailed, plus re-announcements
        + messages  # OrderReserved or OrderBackordered
    )
    invocations = messages / s.avg_batch
    idle_polls = s.consumer_queues * s.idle_pollers_per_queue * SECONDS_PER_MONTH / s.long_poll_s
    sqs_requests = (
        2 * s.orders  # EventBridge SendMessage into the two queues
        + 2 * invocations  # a ReceiveMessage and a DeleteMessageBatch per invocation
        + idle_polls  # empty receives
    )
    ddb = PRICES["dynamodb_on_demand"]
    writes = (
        posts  # the conditional put (a refused one still costs a unit)
        + messages  # placed -> paid
        + messages * (1 + s.lines_per_order) * ddb["transactional_write_units_per_item"]
    )
    reads = 0.5 * (2 * messages + s.orders * s.client_retry_rate)  # eventually consistent gets
    return {
        "posts": posts,
        "messages": messages,
        "events": events,
        "invocations": invocations,
        "sqs_requests": sqs_requests,
        "idle_polls": idle_polls,
        "write_units": writes,
        "read_units": reads,
        "emails": s.orders * s.failed_or_short_rate,
    }


def monthly_cost(s: Scenario) -> dict[str, float]:
    p, c = PRICES, counts(s)
    ddb = p["dynamodb_on_demand"]
    stored_gb = s.orders * s.orders_kept_days / DAYS * s.order_item_bytes / 1e9
    logs_gb = s.orders * s.log_bytes_per_order / 1e9
    return {
        "API Gateway (HTTP API)": c["posts"]
        / 1e6
        * p["api_gateway"]["http_api_per_million_first_300m"],
        "Lambda (accept + charge + fulfil)": (
            lambda_cost(c["posts"], s.accept_ms, s.memory_mb)
            + lambda_cost(c["invocations"], s.charge_ms * s.avg_batch, s.memory_mb)
            + lambda_cost(c["invocations"], s.fulfil_ms * s.avg_batch, s.memory_mb)
        ),
        "EventBridge events + 30-day archive": (
            c["events"] / 1e6 * p["eventbridge"]["custom_events_per_million"]
            + c["events"] * s.event_bytes / 1e9 * p["eventbridge"]["archive_per_gb"]
        ),
        "SQS requests (incl. empty receives)": tiered(
            c["sqs_requests"], p["sqs"]["standard_requests_tiers"]
        ),
        "DynamoDB orders + stock (writes, reads, storage, PITR)": (
            c["write_units"] / 1e6 * ddb["write_request_units_per_million"]
            + c["read_units"] / 1e6 * ddb["read_request_units_per_million"]
            + stored_gb * (ddb["storage_per_gb_month"] + ddb["pitr_per_gb_month"])
        ),
        "SNS emails to staff": tiered(c["emails"], p["sns"]["email_tiers"])
        + tiered(c["emails"], p["sns"]["requests_tiers"]),
        f"{s.alarms} alarms + logs": (
            s.alarms * p["cloudwatch"]["alarm_standard_per_month"]
            + logs_gb
            * (
                p["cloudwatch"]["logs_ingested_per_gb"]
                + p["cloudwatch"]["logs_storage_per_gb_month"]
            )
        ),
    }


def plumbing_alternatives(s: Scenario) -> dict[str, float]:
    """The messaging line only, if the same orders flowed through other plumbing.

    Everything else (API, functions, tables, alarms) is the same in every
    row. Step Functions prices one execution per order with eight state
    transitions (accept, charge, choice, reserve, choice, notify, plus
    start and end); Express adds its duration at 64 MB for one second.
    """
    p, c = PRICES, counts(s)
    sf = p["step_functions"]
    transitions_per_order = 8
    return {
        "EventBridge + SQS standard (this chapter)": (
            c["events"] / 1e6 * p["eventbridge"]["custom_events_per_million"]
            + tiered(c["sqs_requests"], p["sqs"]["standard_requests_tiers"])
        ),
        "EventBridge + SQS FIFO (ordered per order id)": (
            c["events"] / 1e6 * p["eventbridge"]["custom_events_per_million"]
            + tiered(c["sqs_requests"], p["sqs"]["fifo_requests_tiers"])
        ),
        "Step Functions Standard, one execution per order": (
            s.orders * transitions_per_order * sf["standard_per_transition"]
        ),
        "Step Functions Express, one execution per order": (
            s.orders / 1e6 * sf["express_per_million_requests"]
            + s.orders * 1.0 * (64 / 1024) * sf["express_gb_second_tier1"]
        ),
    }


def markdown_table(scenarios: list[Scenario]) -> str:
    costs = [monthly_cost(s) for s in scenarios]
    rows = ["| Line item | " + " | ".join(s.name for s in scenarios) + " |"]
    rows.append("|---|" + "---:|" * len(scenarios))
    for item in costs[0]:
        rows.append(f"| {item} | " + " | ".join(f"${c[item]:,.2f}" for c in costs) + " |")
    rows.append(
        "| **Total / month** | " + " | ".join(f"**${sum(c.values()):,.2f}**" for c in costs) + " |"
    )
    rows.append(
        "| per order | "
        + " | ".join(
            f"${sum(c.values()) / s.orders:,.5f}" for c, s in zip(costs, scenarios, strict=True)
        )
        + " |"
    )
    return "\n".join(rows)


def alternatives_table(scenarios: list[Scenario]) -> str:
    alts = [plumbing_alternatives(s) for s in scenarios]
    rows = ["| Messaging line only | " + " | ".join(s.name for s in scenarios) + " |"]
    rows.append("|---|" + "---:|" * len(scenarios))
    for item in alts[0]:
        rows.append(f"| {item} | " + " | ".join(f"${a[item]:,.2f}" for a in alts) + " |")
    return "\n".join(rows)


if __name__ == "__main__":
    print(markdown_table(SCENARIOS))
    print()
    print(alternatives_table(SCENARIOS))
    print()
    c = counts(SCENARIOS[0])
    print(
        "List prices, us-east-1; prices.json has sources, dates and tiers. "
        f"Idle polling assumed: {int(c['idle_polls']):,} empty receives a month "
        f"({SCENARIOS[0].consumer_queues} queues x {SCENARIOS[0].idle_pollers_per_queue} pollers)."
    )
