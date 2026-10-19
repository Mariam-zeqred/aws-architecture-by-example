"""Monthly cost model for the chapter 3 architecture.

Usage: ``python cost.py`` prints the chapter's tables: the reference design
at 20, 1 000 and 100 000 devices (with the training task's wall time, which
is the number that actually breaks first), the two changes that make
100 000 fit in a night, and what the same compute would cost on SageMaker
or Lambda. Every assumption is a named field of ``Scenario``; the two
throughput constants were measured on the synthetic archive (README) and
are labelled as such.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

PRICES = json.loads((Path(__file__).parent / "prices.json").read_text())
DAYS = 30
RUNS_PER_MONTH = DAYS  # one execution a night

# Measured here on the synthetic archive (make check prints the live number):
# ~335 000 lines/s parsing gzip JSON lines, ~55 000 rows/s fitting the model.
# A Fargate vCPU is a different machine; these are the right order of magnitude.
LINES_PER_SECOND = 250_000
FIT_ROWS_PER_SECOND = 50_000


@dataclass(frozen=True)
class Scenario:
    name: str
    devices: int
    reading_interval_s: int = 60
    reading_bytes: int = 120  # one archive line
    gzip_ratio: float = 0.2
    training_days: int = 90
    scoring_days: int = 2  # the last 24 complete hours need parts of two days
    feature_source: str = "task"  # "task": parse raw lines; "athena": read hourly rows
    athena_days_scanned: int = 90  # with an appended hourly table, only yesterday: 1
    train_sample_devices: int | None = None  # train on a sample; score everyone
    hourly_row_bytes: int = 40  # one Parquet row of the hourly table
    hourly_rows_per_second: int = 2_000_000  # a columnar load, not json.loads (a guess)
    task_vcpu: int = 1
    task_gb: int = 4
    task_startup_s: int = 60  # pull the image, start Python, load the champion
    model_bytes: int = 420_000  # measured: one joblib file
    prediction_item_bytes: int = 130
    predictions_kept_days: int = 365
    gate_lambda_ms: int = 1_000
    sfn_transitions_per_run: int = 10  # RunId, Train, Gate, Choice, Score, Notify + starts/ends
    archive_objects_per_day: int = 288  # Firehose flushes every 5 minutes (chapter 2)
    log_bytes_per_run: int = 50_000


SCENARIOS = [
    Scenario("20 devices", devices=20),
    Scenario("1 000 devices", devices=1_000),
    Scenario("100 000 devices", devices=100_000),
]
TUNED = [
    replace(
        SCENARIOS[2],
        name="100 000, Athena features + 5 000-device sample",
        feature_source="athena",
        train_sample_devices=5_000,
    ),
    replace(
        SCENARIOS[2],
        name="100 000, + hourly table appended nightly",
        feature_source="athena",
        athena_days_scanned=1,
        train_sample_devices=5_000,
    ),
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


def readings_per_day(s: Scenario) -> int:
    return 86_400 // s.reading_interval_s


def train_seconds(s: Scenario) -> float:
    """Wall time of one training task: startup + reading features + fitting."""
    trained = s.train_sample_devices or s.devices
    if s.feature_source == "task":
        lines = s.devices * readings_per_day(s) * s.training_days  # every device is parsed
        read = lines / LINES_PER_SECOND
    else:
        read = trained * 24 * s.training_days / s.hourly_rows_per_second
    rows = trained * 24 * s.training_days
    return s.task_startup_s + read + rows / FIT_ROWS_PER_SECOND


def score_seconds(s: Scenario) -> float:
    if s.feature_source == "task":
        read = s.devices * readings_per_day(s) * s.scoring_days / LINES_PER_SECOND
    else:
        read = s.devices * 24 * s.scoring_days / s.hourly_rows_per_second
    return s.task_startup_s + read + s.devices / 200_000  # predicting is fast


def fargate_cost(seconds: float, vcpu: int, gb: int) -> float:
    fg = PRICES["fargate"]
    hours = seconds / 3600
    return hours * (vcpu * fg["vcpu_hour_arm"] + gb * fg["gb_hour_arm"])


def monthly_cost(s: Scenario) -> dict[str, float]:
    p = PRICES
    runs = RUNS_PER_MONTH
    raw_gb_per_day = s.devices * readings_per_day(s) * s.reading_bytes * s.gzip_ratio / 1e9

    compute = runs * (
        fargate_cost(train_seconds(s), s.task_vcpu, s.task_gb)
        + fargate_cost(score_seconds(s), s.task_vcpu, s.task_gb)
    )
    if s.feature_source == "athena":
        # Athena bills the compressed bytes it scans; the hourly table it
        # writes is tiny next to the raw archive it reads.
        scanned_tb = (s.athena_days_scanned + s.scoring_days) * raw_gb_per_day / 1e3
        athena = runs * scanned_tb * p["athena"]["per_tb_scanned"]
        objects_read = runs * (s.train_sample_devices or s.devices) * 24 * s.training_days / 1e6
    else:
        athena = 0.0
        objects_read = runs * s.archive_objects_per_day * (s.training_days + s.scoring_days)
    sfn = runs * s.sfn_transitions_per_run * p["step_functions"]["standard_per_transition"]
    gate = (
        runs / 1e6 * p["lambda"]["requests_per_million"]
        + runs * (s.gate_lambda_ms / 1000) * (256 / 1024) * p["lambda"]["gb_second_arm_tier1"]
    )
    # Predictions: one write per device per night; items kept a year in S3.
    pred_writes = (
        runs * s.devices / 1e6 * p["dynamodb_on_demand"]["write_request_units_per_million"]
    )
    pred_storage = (
        s.devices * s.prediction_item_bytes / 1e9 * p["dynamodb_on_demand"]["storage_per_gb_month"]
    )
    s3_gb = (
        30 * s.model_bytes  # a month of candidates, then they expire
        + 12 * s.model_bytes  # noncurrent versions of models/current
        + s.devices * s.prediction_item_bytes * s.predictions_kept_days
    ) / 1e9
    s3 = (
        s3_gb * p["s3"]["storage_per_gb_month_first_50tb"]
        + runs * 10 / 1e3 * p["s3"]["put_per_1k"]
        + objects_read / 1e4 * p["s3"]["get_per_10k"]
    )
    ecr = 0.5 * p["ecr"]["storage_per_gb_month"]  # a half-gigabyte image
    logs_gb = runs * s.log_bytes_per_run / 1e9
    watch = (
        2 * p["cloudwatch"]["alarm_standard_per_month"]
        + logs_gb
        * (p["cloudwatch"]["logs_ingested_per_gb"] + p["cloudwatch"]["logs_storage_per_gb_month"])
        + tiered(runs, p["eventbridge_scheduler"]["invocations_tiers"])
        + tiered(runs, p["sns"]["requests_tiers"])
        + tiered(runs, p["sns"]["email_tiers"])
    )
    return {
        "Fargate tasks (train + score)": compute,
        "Athena feature aggregation": athena,
        "Step Functions transitions": sfn,
        "Gate Lambda": gate,
        "DynamoDB predictions (writes + storage)": pred_writes + pred_storage,
        "S3 models + predictions + archive GETs": s3,
        "ECR image storage": ecr,
        "Scheduler, 2 alarms, SNS, logs": watch,
    }


def compute_alternatives(s: Scenario) -> dict[str, float]:
    """The compute line if the same minutes ran elsewhere.

    Lambda is capped at 15 minutes per invocation, so it is only an option
    when the training task fits; SageMaker is priced on ml.m5.xlarge
    (4 vCPU, 16 GiB) against Fargate at the same size, because its smallest
    instances are already bigger than this task.
    """
    seconds = train_seconds(s) + score_seconds(s)
    sm = PRICES["sagemaker"]
    out = {
        "Fargate, 1 vCPU / 4 GB (this chapter)": RUNS_PER_MONTH * fargate_cost(seconds, 1, 4),
        "Fargate, 4 vCPU / 16 GB": RUNS_PER_MONTH * fargate_cost(seconds, 4, 16),
        "SageMaker training + transform, ml.m5.xlarge": RUNS_PER_MONTH
        * seconds
        / 3600
        * sm["train_ml_m5_xlarge_hour"],
    }
    if train_seconds(s) <= 900 and score_seconds(s) <= 900:
        lam = PRICES["lambda"]
        out["Lambda, 4 GB (fits in 15 min)"] = RUNS_PER_MONTH * (
            2 * lam["requests_per_million"] / 1e6 + seconds * 4 * lam["gb_second_arm_tier1"]
        )
    else:
        out["Lambda, 4 GB (fits in 15 min)"] = math.nan
    return out


def _hms(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"
    return f"{seconds // 60} min {seconds % 60:02d} s"


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
        "| training task wall time / night | "
        + " | ".join(_hms(train_seconds(s)) for s in scenarios)
        + " |"
    )
    rows.append(
        "| scoring task wall time / night | "
        + " | ".join(_hms(score_seconds(s)) for s in scenarios)
        + " |"
    )
    return "\n".join(rows)


def alternatives_table(scenarios: list[Scenario]) -> str:
    alts = [compute_alternatives(s) for s in scenarios]
    rows = ["| Compute for the same minutes | " + " | ".join(s.name for s in scenarios) + " |"]
    rows.append("|---|" + "---:|" * len(scenarios))
    for item in alts[0]:
        cells = ["n/a" if math.isnan(a[item]) else f"${a[item]:,.2f}" for a in alts]
        rows.append(f"| {item} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


if __name__ == "__main__":
    print(markdown_table(SCENARIOS))
    print()
    print(markdown_table([SCENARIOS[2], *TUNED]))
    print()
    print(alternatives_table([*SCENARIOS, TUNED[1]]))
    print()
    print(
        "List prices, us-east-1; prices.json has sources, dates and tiers. "
        f"Throughput constants: {LINES_PER_SECOND:,} lines/s, {FIT_ROWS_PER_SECOND:,} rows/s."
    )
