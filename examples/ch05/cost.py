"""Monthly cost model for chapter 5: what each recovery strategy adds.

Usage: ``python cost.py`` prints two tables. The first is the line items of
this chapter's design (a warm standby in eu-west-1 for the systems of
chapters 1 to 4) at two sizes: the cooperative as it is, and a hundred
times the devices and fifty times the orders. The second is the total each
of the four strategies in ``drill.py`` adds to the single-region bill, so
the money can be read beside the drill table's RPO and RTO.

Sizes are derived from the same constants as chapters 2 and 4 (one reading
a minute per device, 120-byte readings compressed 5:1, twelve months of
archive; 700-byte orders kept a year, chapter 4's write counts), restated
here rather than imported so this file stands alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PRICES = json.loads((Path(__file__).parent / "prices.json").read_text())
DAYS = 30
PRIMARY, STANDBY = "us-east-1", "eu-west-1"


@dataclass(frozen=True)
class Scenario:
    name: str
    devices: int
    orders: int
    # chapter 2's archive
    readings_per_device_per_day: int = 1_440
    reading_bytes: int = 120
    gzip_ratio: float = 0.2
    archive_months: int = 12
    firehose_buffer_gb: float = 0.064  # one object per 64 MB or per 5 minutes
    # chapter 4's orders
    order_item_bytes: int = 700
    orders_kept_days: int = 365
    client_retry_rate: float = 0.02
    duplicate_delivery_rate: float = 0.01
    lines_per_order: int = 2
    # this chapter
    backups_retained: int = 35  # daily recovery points kept, each a full copy
    copies_per_month: int = 30
    alarms: int = 5
    replication_rules: int = 2  # archive and site
    metrics_per_rule: int = 4  # S3 replication metrics, billed as custom metrics


SCENARIOS = [
    Scenario("1 000 devices, 2 000 orders", devices=1_000, orders=2_000),
    Scenario("100 000 devices, 100 000 orders", devices=100_000, orders=100_000),
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


def sizes(s: Scenario) -> dict[str, float]:
    """Gigabytes and counts a month, from the chapter 2 and 4 constants."""
    readings = s.devices * s.readings_per_device_per_day * DAYS
    raw_gb = readings * s.reading_bytes / 1e9
    archive_gb_per_month = raw_gb * s.gzip_ratio
    posts = s.orders * (1 + s.client_retry_rate)
    messages = s.orders * (1 + s.duplicate_delivery_rate)
    # one item write per status move and per stock line (chapter 4's counts, as items)
    item_writes = posts + messages + messages * (1 + s.lines_per_order)
    return {
        "archive_gb_per_month": archive_gb_per_month,
        "archive_gb": archive_gb_per_month * s.archive_months,
        "archive_objects_per_month": max(DAYS * 86_400 / 300, raw_gb / s.firehose_buffer_gb),
        "table_gb": s.orders * s.orders_kept_days / DAYS * s.order_item_bytes / 1e9,
        "item_writes": item_writes,
        "replicated_gb": item_writes * s.order_item_bytes / 1e9,
    }


def warm_standby_lines(s: Scenario) -> dict[str, float]:
    """What this chapter's stacks add to the chapters 1-4 bill, by line."""
    p, z = PRICES, sizes(s)
    ddb_p, ddb_s = p["dynamodb"][PRIMARY], p["dynamodb"][STANDBY]
    bk = p["backup"]
    return {
        # Every write is billed as a replicated write in each region. In this
        # offer version us-east-1's rWRU price equals its WRU price, so the
        # primary's own line does not move; the standby's writes are new.
        "DynamoDB replica: replicated writes, storage, PITR (eu-west-1)": (
            z["item_writes"] / 1e6 * ddb_s["replicated_write_request_units_per_million"]
            + z["item_writes"]
            / 1e6
            * (
                ddb_p["replicated_write_request_units_per_million"]
                - ddb_p["write_request_units_per_million"]
            )
            + z["table_gb"] * (ddb_s["storage_per_gb_month"] + ddb_s["pitr_per_gb_month"])
            + z["replicated_gb"] * p["data_transfer"]["us_east_1_to_eu_west_1_per_gb"]
        ),
        "S3 replicas: storage (eu-west-1), replication PUTs, inter-region transfer": (
            z["archive_gb"] * p["s3"]["storage_per_gb_month_first_50tb"]
            + z["archive_objects_per_month"] / 1e3 * p["s3"]["put_per_1k"]
            + z["archive_gb_per_month"] * p["data_transfer"]["us_east_1_to_eu_west_1_per_gb"]
        ),
        "AWS Backup: 35 daily copies in each region + cross-region transfer": (
            z["table_gb"]
            * s.backups_retained
            * (
                bk["dynamodb_warm_storage_per_gb_month"][PRIMARY]
                + bk["dynamodb_warm_storage_per_gb_month"][STANDBY]
            )
            + z["table_gb"] * s.copies_per_month * bk["dynamodb_cross_region_copy_per_gb"]
        ),
        "Route 53: hosted zone, 1 health check, alias queries": (
            p["route53"]["hosted_zone_per_month"]
            + tiered(1, p["route53"]["health_check_aws_endpoint_tiers"])
        ),
        "Standby API + function, idle": 0.0,
        f"{s.alarms} alarms + {s.replication_rules * s.metrics_per_rule} replication metrics": (
            s.alarms * p["cloudwatch"]["alarm_standard_per_month"]
            + s.replication_rules * s.metrics_per_rule * p["cloudwatch"]["custom_metric_per_month"]
        ),
    }


def strategy_totals(s: Scenario) -> dict[str, float]:
    """Added monthly cost of each strategy in drill.py, same order."""
    lines = warm_standby_lines(s)
    backup_line = next(v for k, v in lines.items() if k.startswith("AWS Backup"))
    s3_line = next(v for k, v in lines.items() if k.startswith("S3 replicas"))
    zone = PRICES["route53"]["hosted_zone_per_month"]
    # S3 is replicated in every strategy but the first: AWS Backup for S3
    # costs $0.05 a GB-month against $0.023 for a replica, and a replica with
    # versioning and no delete-marker replication already is the copy.
    warm = sum(lines.values())
    return {
        "one region (chapters 1-4 as they are)": 0.0,
        "backup & restore (daily copies + replicated archive + a zone)": (
            backup_line + s3_line + zone
        ),
        "warm standby (this chapter)": warm,
        "active-active (same resources, both regions writing)": warm,
    }


def markdown_table(scenarios: list[Scenario]) -> str:
    costs = [warm_standby_lines(s) for s in scenarios]
    rows = ["| Line item | " + " | ".join(s.name for s in scenarios) + " |"]
    rows.append("|---|" + "---:|" * len(scenarios))
    for item in costs[0]:
        rows.append(f"| {item} | " + " | ".join(f"${c[item]:,.2f}" for c in costs) + " |")
    rows.append(
        "| **Added / month** | " + " | ".join(f"**${sum(c.values()):,.2f}**" for c in costs) + " |"
    )
    return "\n".join(rows)


def strategies_table(scenarios: list[Scenario]) -> str:
    totals = [strategy_totals(s) for s in scenarios]
    rows = ["| Strategy, added / month | " + " | ".join(s.name for s in scenarios) + " |"]
    rows.append("|---|" + "---:|" * len(scenarios))
    for item in totals[0]:
        rows.append(f"| {item} | " + " | ".join(f"${t[item]:,.2f}" for t in totals) + " |")
    return "\n".join(rows)


if __name__ == "__main__":
    print(markdown_table(SCENARIOS))
    print()
    print(strategies_table(SCENARIOS))
    print()
    z = sizes(SCENARIOS[0])
    print(
        "List prices, us-east-1 and eu-west-1; prices.json has sources, dates and what could "
        f"not be checked. Cooperative sizes: archive {z['archive_gb']:.1f} GB, orders table "
        f"{z['table_gb'] * 1000:.0f} MB, {z['item_writes']:,.0f} item writes a month."
    )
