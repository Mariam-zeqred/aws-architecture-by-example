"""Chapter 6: the five chapters' cost models added up, as one system.

Each chapter priced its own design at its own sizes. ``python rollup.py``
loads the five ``cost.py`` files as they are, picks the scenario of each
that describes the same cooperative, and prints:

* the bill per chapter and in total, at the cooperative's size today and at
  a hundred times the devices;
* the largest line items across all five chapters at each size, with their
  share of the whole, which is where a cost review starts;
* the same total with the three levers the chapters already priced
  (chapter 1's page weight, chapter 2's batching and provisioned capacity,
  chapter 3's features built by Athena), one lever at a time and together.

Two lines are counted once: chapter 1 priced its own readings table (20
devices posting through the API) and a hosted zone, and chapters 2 and 5
replaced both, so they are dropped from chapter 1 here. Every number is
list price from the chapters' ``prices.json`` files, on the dates recorded
there; none is a measurement.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).parent
EXAMPLES = HERE.parent


def load_cost_module(chapter: str) -> ModuleType:
    """Import examples/<chapter>/cost.py under its own name, whatever ``cost`` is loaded."""
    name = f"{chapter}_cost"
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / chapter / "cost.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Part:
    """One chapter's contribution: which function prices it and which lines to drop."""

    chapter: str
    title: str
    function: str = "monthly_cost"
    drop: tuple[str, ...] = ()  # line items another chapter already counts


PARTS = [
    Part(
        "ch01",
        "1 site + API",
        drop=("DynamoDB writes", "DynamoDB storage + PITR", "Route 53 hosted zone"),
    ),
    Part("ch02", "2 telemetry"),
    Part("ch03", "3 nightly ML"),
    Part("ch04", "4 orders"),
    Part("ch05", "5 standby region", function="warm_standby_lines"),
]

# Which of each chapter's scenarios is "the cooperative today" and which is
# "a hundred times the devices" (chapter 4 and 5: fifty times the orders).
TODAY = {"ch01": 0, "ch02": 1, "ch03": 1, "ch04": 0, "ch05": 0}
SCALE = {"ch01": 1, "ch02": 2, "ch03": 2, "ch04": 1, "ch05": 1}

MODULES = {p.chapter: load_cost_module(p.chapter) for p in PARTS}


def scenario(chapter: str, which: dict[str, int]):
    return MODULES[chapter].SCENARIOS[which[chapter]]


def lines_for(part: Part, s) -> dict[str, float]:
    lines = getattr(MODULES[part.chapter], part.function)(s)
    return {k: v for k, v in lines.items() if k not in part.drop}


def levers(which: dict[str, int]) -> dict[str, dict[str, object]]:
    """The three levers the chapters priced, as overrides of the base scenario at one size."""
    s1, s2, s3 = (scenario(ch, which) for ch in ("ch01", "ch02", "ch03"))
    return {
        "chapter 1: a 300 kB page instead of 1.2 MB": {
            "ch01": replace(s1, bytes_per_visit=300_000)
        },
        "chapter 2: five readings a message + provisioned capacity": {
            "ch02": replace(s2, readings_per_message=5, provisioned_ddb=True)
        },
        "chapter 3: features from an hourly table Athena appends nightly": {
            "ch03": replace(
                s3,
                feature_source="athena",
                athena_days_scanned=1,
                train_sample_devices=min(5_000, s3.devices),
            )
        },
    }


def bill(which: dict[str, int], overrides: dict | None = None) -> dict[str, dict[str, float]]:
    """Line items per chapter for one size, with optional scenario overrides."""
    overrides = overrides or {}
    out = {}
    for part in PARTS:
        s = overrides.get(part.chapter) or scenario(part.chapter, which)
        out[part.title] = lines_for(part, s)
    return out


def total(b: dict[str, dict[str, float]]) -> float:
    return sum(sum(lines.values()) for lines in b.values())


def chapters_table() -> str:
    today, scale = bill(TODAY), bill(SCALE)
    rows = [
        "| Chapter | 1 000 devices, 2 000 orders, 2 000 visits a day | "
        "100 000 devices, 100 000 orders, 200 000 visits a day |",
        "|---|---:|---:|",
    ]
    for part in PARTS:
        rows.append(
            f"| {part.title} | ${sum(today[part.title].values()):,.2f} | "
            f"${sum(scale[part.title].values()):,.2f} |"
        )
    rows.append(
        f"| **Whole system / month** | **${total(today):,.2f}** | **${total(scale):,.2f}** |"
    )
    return "\n".join(rows)


def largest_lines(
    b: dict[str, dict[str, float]], n: int = 8
) -> list[tuple[str, str, float, float]]:
    """(chapter, line, dollars, share) for the n largest lines of a bill."""
    whole = total(b)
    flat = [(ch, line, v) for ch, lines in b.items() for line, v in lines.items()]
    flat.sort(key=lambda t: -t[2])
    return [(ch, line, v, v / whole) for ch, line, v in flat[:n]]


def largest_table(which: dict[str, int], title: str, n: int = 8) -> str:
    b = bill(which)
    rows = [f"| Largest lines, {title} | $ / month | Share |", "|---|---:|---:|"]
    for ch, line, v, share in largest_lines(b, n):
        rows.append(f"| {ch}: {line} | ${v:,.2f} | {share:.0%} |")
    rows.append(
        f"| the other {sum(len(v) for v in b.values()) - n} lines | "
        f"${total(b) - sum(v for _, _, v, _ in largest_lines(b, n)):,.2f} | |"
    )
    return "\n".join(rows)


def levers_table(which: dict[str, int], title: str) -> str:
    base = total(bill(which))
    rows = [
        f"| {title} | Whole system / month | Saved |",
        "|---|---:|---:|",
        f"| as the chapters stand | ${base:,.2f} | |",
    ]
    all_overrides: dict = {}
    for name, overrides in levers(which).items():
        t = total(bill(which, overrides))
        rows.append(f"| {name} | ${t:,.2f} | ${base - t:,.2f} |")
        all_overrides.update(overrides)
    t = total(bill(which, all_overrides))
    rows.append(f"| **all three** | **${t:,.2f}** | **${base - t:,.2f}** |")
    return "\n".join(rows)


if __name__ == "__main__":
    print(chapters_table())
    print()
    print(largest_table(TODAY, "cooperative today"))
    print()
    print(largest_table(SCALE, "100 000 devices"))
    print()
    print(levers_table(TODAY, "Cooperative today"))
    print()
    print(levers_table(SCALE, "At 100 000 devices"))
    print()
    print(
        "List prices from each chapter's prices.json on the dates recorded there; chapter 1's "
        "readings table and hosted zone are dropped because chapters 2 and 5 count them."
    )
