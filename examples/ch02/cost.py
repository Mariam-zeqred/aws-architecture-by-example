"""Monthly cost model for the chapter 2 architecture.

Usage: ``python cost.py`` prints the chapter's tables: the reference design
at 20, 1 000 and 100 000 devices, the same fleet with the two changes that
section 5 argues for, and what per-device alarms would have cost instead.
Every assumption is a named field of ``Scenario``; change it and re-run.
Prices come from ``prices.json`` (list prices; tiers only where noted there).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

PRICES = json.loads((Path(__file__).parent / "prices.json").read_text())
DAYS = 30
HOURS = 24 * DAYS
MINUTES = 60 * HOURS


@dataclass(frozen=True)
class Scenario:
    name: str
    devices: int
    reading_interval_s: int = 60  # one reading a minute, always connected
    readings_per_message: int = 1  # >1 means the firmware batches readings
    reading_bytes: int = 120  # one reading as JSON on the wire
    item_bytes: int = 160  # one history item (the reading plus received, expires)
    history_days: int = 30
    archive_months: int = 12  # how much raw history is sitting in S3
    gzip_ratio: float = 0.2  # JSON telemetry compresses about 5:1
    lambda_ms: int = 80  # two DynamoDB writes from a warm function
    lambda_mb: int = 256
    reject_fraction: float = 0.001  # one reading in a thousand fails validation
    scan_interval_s: int = 300  # the stale-device scan
    status_item_bytes: int = 150
    athena_queries_per_month: int = 100
    athena_days_scanned_per_query: int = 1
    provisioned_ddb: bool = False  # False: on-demand; True: provisioned WCU, 20 % headroom
    basic_ingest: bool = True  # devices publish to $aws/rules/<rule>/..., no messaging fee


SCENARIOS = [
    Scenario("20 devices", devices=20),
    Scenario("1 000 devices", devices=1_000),
    Scenario("100 000 devices", devices=100_000),
]
TUNED = [
    replace(SCENARIOS[2], name="100 000, batched x5", readings_per_message=5),
    replace(
        SCENARIOS[2],
        name="100 000, batched x5 + provisioned",
        readings_per_message=5,
        provisioned_ddb=True,
    ),
]


def tiered(quantity: float, tiers: list[list]) -> float:
    """Price ``quantity`` units across ``[[upper_bound_or_None, unit_price], ...]``."""
    total, lower = 0.0, 0.0
    for upper, price in tiers:
        band = quantity - lower if upper is None else min(quantity, upper) - lower
        if band <= 0:
            break
        total += band * price
        lower = upper
    return total


def monthly_cost(s: Scenario) -> dict[str, float]:
    """Return cost per line item in USD for one month."""
    p = PRICES
    iot, lam, ddb, prov = (
        p["iot_core"],
        p["lambda"],
        p["dynamodb_on_demand"],
        p["dynamodb_provisioned"],
    )
    fh, s3, cw = p["firehose"], p["s3"], p["cloudwatch"]

    readings = s.devices * (86_400 // s.reading_interval_s) * DAYS
    messages = math.ceil(readings / s.readings_per_message)
    message_bytes = s.reading_bytes * s.readings_per_message
    # IoT bills a message per 5 KB increment; a batch of 5 x 120 B is still one.
    message_units = messages * math.ceil(message_bytes / iot["message_billing_increment_bytes"])
    messaging = (
        0.0 if s.basic_ingest else tiered(message_units, iot["messages_per_million_tiers"]) / 1e6
    )

    # Hot path. The function is invoked once per message and writes one
    # history item per reading plus one status item per message.
    gb_seconds = messages * (s.lambda_ms / 1000) * (s.lambda_mb / 1024)
    writes = readings + messages  # each item is < 1 KB, so 1 WRU each
    if s.provisioned_ddb:
        wcu = math.ceil(writes / (DAYS * 86_400) * 1.2)  # steady rate + 20 % headroom
        ddb_writes = wcu * HOURS * prov["write_capacity_unit_hour"]
    else:
        ddb_writes = writes / 1e6 * ddb["write_request_units_per_million"]
    history_gb = readings * (s.history_days / DAYS) * s.item_bytes / 1e9
    status_gb = s.devices * s.status_item_bytes / 1e9
    ddb_storage = (history_gb + status_gb) * ddb["storage_per_gb_month"] + status_gb * ddb[
        "pitr_per_gb_month"
    ]

    # Cold path. Firehose rounds every record up to 5 KB, so a 120 B reading
    # is billed as 5 120 B: the single biggest reason to batch on the device.
    record_billed = (
        math.ceil(message_bytes / fh["record_billing_increment_bytes"])
        * fh["record_billing_increment_bytes"]
    )
    firehose = messages * record_billed / 1e9 * fh["ingested_per_gb_first_500tb"]
    raw_gb_per_month = readings * s.reading_bytes / 1e9 * s.gzip_ratio
    archive_gb = raw_gb_per_month * s.archive_months
    # Firehose flushes every 300 s or 64 MB: objects per month is the larger count.
    flushes = max(DAYS * 86_400 / 300, raw_gb_per_month / s.gzip_ratio / 0.064)
    s3_cost = archive_gb * s3["storage_per_gb_month_first_50tb"] + flushes / 1e3 * s3["put_per_1k"]
    scanned_tb = (
        s.athena_queries_per_month
        * s.athena_days_scanned_per_query
        * (raw_gb_per_month / DAYS)
        / 1e3
    )
    athena = scanned_tb * p["athena"]["per_tb_scanned"]

    # Watchdog: a scan every 5 min reads the whole status table once.
    scans = DAYS * 86_400 / s.scan_interval_s
    rru_per_scan = math.ceil(s.devices * s.status_item_bytes / 4_096) / 2  # eventually consistent
    scan_reads = scans * max(rru_per_scan, 0.5) / 1e6 * ddb["read_request_units_per_million"]
    scan_lambda = (
        scans / 1e6 * lam["requests_per_million"]
        + scans * max(1.0, s.devices / 20_000) * (s.lambda_mb / 1024) * lam["gb_second_arm_tier1"]
    )
    rejects = readings * s.reject_fraction / 1e6 * p["sqs"]["standard_requests_per_million"]
    watch = (
        3 * cw["alarm_standard_per_month"]
        + tiered(1, cw["custom_metric_per_month_tiers"])
        + scan_reads
        + scan_lambda
        + rejects
    )
    # The ingest function logs one 150 B line per message.
    logs_gb = messages * 150 / 1e9
    logs_cost = logs_gb * cw["logs_ingested_per_gb"] + logs_gb * cw["logs_storage_per_gb_month"]

    return {
        "IoT Core connection minutes": s.devices
        * MINUTES
        / 1e6
        * iot["connection_minutes_per_million"],
        "IoT Core messaging": messaging,
        "IoT Core rules triggered": messages / 1e6 * iot["rules_triggered_per_million"],
        "IoT Core actions (Lambda + Firehose)": 2
        * messages
        / 1e6
        * iot["actions_executed_per_million"],
        "Lambda requests": messages / 1e6 * lam["requests_per_million"],
        "Lambda duration (ARM)": gb_seconds * lam["gb_second_arm_tier1"],
        "DynamoDB writes" + (" (provisioned)" if s.provisioned_ddb else " (on-demand)"): ddb_writes,
        "DynamoDB storage + status PITR": ddb_storage,
        "Firehose ingestion (5 KB minimum)": firehose,
        "S3 archive (12 months) + puts": s3_cost,
        "Athena queries": athena,
        "Watchdog: 3 alarms, 1 metric, scans, rejects": watch,
        "CloudWatch Logs": logs_cost,
    }


def per_device_alarms_cost(devices: int) -> float:
    """What the obvious design, one alarm and one metric per device, costs instead."""
    cw = PRICES["cloudwatch"]
    return devices * cw["alarm_standard_per_month"] + tiered(
        devices, cw["custom_metric_per_month_tiers"]
    )


def usage_summary(s: Scenario) -> dict[str, str]:
    readings = s.devices * (86_400 // s.reading_interval_s) * DAYS
    messages = math.ceil(readings / s.readings_per_message)
    return {
        "readings / month": f"{readings:,}",
        "messages / month": f"{messages:,}",
        "raw JSON": f"{readings * s.reading_bytes / 1e9:,.1f} GB",
        "history items live": f"{s.devices * (86_400 // s.reading_interval_s) * s.history_days:,}",
    }


def markdown_table(scenarios: list[Scenario]) -> str:
    costs = [monthly_cost(s) for s in scenarios]
    # Line items differ in one label (on-demand vs provisioned); merge them.
    items: list[str] = []
    for c in costs:
        for k in c:
            key = k.split(" (")[0] if k.startswith("DynamoDB writes") else k
            if key not in items:
                items.append(key)

    def lookup(c: dict[str, float], key: str) -> float:
        for k, v in c.items():
            if k == key or (key == "DynamoDB writes" and k.startswith(key)):
                return v
        return 0.0

    rows = ["| Line item | " + " | ".join(s.name for s in scenarios) + " |"]
    rows.append("|---|" + "---:|" * len(scenarios))
    for item in items:
        rows.append(f"| {item} | " + " | ".join(f"${lookup(c, item):,.2f}" for c in costs) + " |")
    rows.append(
        "| **Total / month** | " + " | ".join(f"**${sum(c.values()):,.2f}**" for c in costs) + " |"
    )
    rows.append(
        "| per device / month | "
        + " | ".join(
            f"${sum(c.values()) / s.devices:,.4f}" for c, s in zip(costs, scenarios, strict=True)
        )
        + " |"
    )
    return "\n".join(rows)


def alarms_table(scenarios: list[Scenario]) -> str:
    rows = ["| Watchdog design | " + " | ".join(s.name for s in scenarios) + " |"]
    rows.append("|---|" + "---:|" * len(scenarios))
    rows.append(
        "| one alarm + one metric per device | "
        + " | ".join(f"${per_device_alarms_cost(s.devices):,.2f}" for s in scenarios)
        + " |"
    )
    cw = PRICES["cloudwatch"]
    fleet = 3 * cw["alarm_standard_per_month"] + tiered(1, cw["custom_metric_per_month_tiers"])
    rows.append(
        "| this chapter: 3 alarms + 1 fleet metric (scans priced above) | "
        + " | ".join(f"${fleet:,.2f}" for _ in scenarios)
        + " |"
    )
    return "\n".join(rows)


if __name__ == "__main__":
    for s in SCENARIOS + TUNED:
        print(f"{s.name}: " + ", ".join(f"{k} {v}" for k, v in usage_summary(s).items()))
    print()
    print(markdown_table(SCENARIOS))
    print()
    print(markdown_table([SCENARIOS[2], *TUNED]))
    print()
    print(alarms_table(SCENARIOS))
    print()
    print("List prices, us-east-1, no free tier; prices.json has sources, dates and tiers.")
