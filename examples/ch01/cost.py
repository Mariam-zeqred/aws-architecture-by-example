"""Monthly cost model for the chapter 1 architecture.

Usage: ``python cost.py`` prints a Markdown table for the two scenarios in the
chapter. Every assumption is a named constant below; change it and re-run.
Prices come from ``prices.json`` (first tier, no free-tier allowance).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

PRICES = json.loads((Path(__file__).parent / "prices.json").read_text())
DAYS = 30


@dataclass(frozen=True)
class Scenario:
    name: str
    visits_per_day: int
    objects_per_visit: int = 12  # html + css + js + a few images
    bytes_per_visit: int = 1_200_000  # 1.2 MB of static files per visit
    api_calls_per_visit: int = 3
    api_response_bytes: int = 4_000  # 20 readings, as JSON
    lambda_ms: int = 120  # typical warm duration of the handler
    lambda_mb: int = 256
    devices: int = 20
    reading_interval_s: int = 60
    reading_bytes: int = 120  # one DynamoDB item
    retention_days: int = 30
    site_gb: float = 0.01  # the static files in S3


SCENARIOS = [
    Scenario("small", visits_per_day=2_000),
    Scenario("busy", visits_per_day=200_000),
]


def monthly_cost(s: Scenario) -> dict[str, float]:
    """Return cost per line item in USD for one month."""
    p = PRICES
    cf, ddb, lam, apig = p["cloudfront"], p["dynamodb_on_demand"], p["lambda"], p["api_gateway"]
    visits = s.visits_per_day * DAYS
    cf_requests = visits * (s.objects_per_visit + s.api_calls_per_visit)
    cf_gb = visits * (s.bytes_per_visit + s.api_calls_per_visit * s.api_response_bytes) / 1e9
    api_calls = visits * s.api_calls_per_visit
    gb_seconds = api_calls * (s.lambda_ms / 1000) * (s.lambda_mb / 1024)
    writes = s.devices * (86_400 // s.reading_interval_s) * DAYS
    # One eventually-consistent query returning <= 8 KB costs 1 read request unit.
    rru_per_call = math.ceil(s.api_response_bytes / 4_096) / 2
    rrus = api_calls * max(rru_per_call, 0.5)
    table_gb = writes * (s.retention_days / DAYS) * s.reading_bytes / 1e9

    return {
        "CloudFront data transfer": cf_gb * cf["transfer_out_per_gb_first_10tb_us_eu"],
        "CloudFront requests": cf_requests / 10_000 * cf["https_requests_per_10k_us"],
        "API Gateway (HTTP API)": api_calls / 1e6 * apig["http_api_per_million_first_300m"],
        "Lambda requests": api_calls / 1e6 * lam["requests_per_million"],
        "Lambda duration (ARM)": gb_seconds * lam["gb_second_arm_tier1"],
        "DynamoDB writes": writes / 1e6 * ddb["write_request_units_per_million"],
        "DynamoDB reads": rrus / 1e6 * ddb["read_request_units_per_million"],
        "DynamoDB storage + PITR": (
            max(table_gb - 25, 0) * ddb["storage_per_gb_month_after_25gb_free"]
            + table_gb * ddb["pitr_per_gb_month"]
        ),
        "S3 storage": s.site_gb * p["s3"]["storage_per_gb_month_first_50tb"],
        "Route 53 hosted zone": p["route53"]["hosted_zone_per_month"],
    }


def usage_summary(s: Scenario) -> dict[str, str]:
    visits = s.visits_per_day * DAYS
    return {
        "visits / month": f"{visits:,}",
        "CloudFront requests": f"{visits * (s.objects_per_visit + s.api_calls_per_visit):,}",
        "data out": f"{visits * s.bytes_per_visit / 1e12:.2f} TB",
        "API calls": f"{visits * s.api_calls_per_visit:,}",
        "device writes": f"{s.devices * (86_400 // s.reading_interval_s) * DAYS:,}",
    }


def markdown_table(scenarios: list[Scenario]) -> str:
    costs = [monthly_cost(s) for s in scenarios]
    head = "| Line item | " + " | ".join(f"{s.name} ({s.visits_per_day:,}/day)" for s in scenarios)
    rows = [head + " |", "|---|" + "---:|" * len(scenarios)]
    for item in costs[0]:
        rows.append(f"| {item} | " + " | ".join(f"${c[item]:,.2f}" for c in costs) + " |")
    totals = " | ".join(f"**${sum(c.values()):,.2f}**" for c in costs)
    rows.append(f"| **Total / month** | {totals} |")
    return "\n".join(rows)


if __name__ == "__main__":
    for s in SCENARIOS:
        print(f"{s.name}: " + ", ".join(f"{k} {v}" for k, v in usage_summary(s).items()))
    print()
    print(markdown_table(SCENARIOS))
    print()
    print("List prices, us-east-1, first tier, no free tier; prices.json has source and date.")
