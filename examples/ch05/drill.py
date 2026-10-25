"""Chapter 5's two drills, in memory.

``python drill.py`` prints the section 4 table: four disasters against four
strategies, each cell the writes lost and the time the cooperative cannot
take orders. It is arithmetic over the named parameters below, not a
measurement: the outage length, how long a person takes to notice and act,
how long a restore takes. Change them and the table changes; the *shape* of
the table is the lesson, and it does not.

``python drill.py conflict`` runs the second drill: a fake global table with
two regions and last-writer-wins replication. The same ten units of stock
are reserved from both regions at once, and the chapter 4 condition that
protects a counter inside one region does not protect it across two.
"""

from __future__ import annotations

import heapq
import sys
from dataclasses import dataclass, field

# --- Parameters: every number the drill table is built from ----------------------
# Illustrative. None of these is a measurement of AWS or of the cooperative.


@dataclass(frozen=True)
class Params:
    outage_hours: float = 6.0  # how long the primary region is unavailable
    replication_lag_s: float = 1.0  # global tables: usually about a second
    health_check_s: float = 90.0  # 3 failures x 30 s interval (stack.py)
    dns_ttl_s: float = 60.0  # API Gateway alias TTL (stack.py DNS_TTL)
    person_notices_min: float = 15.0  # alarm -> a person at a keyboard
    person_decides_min: float = 15.0  # ... -> decides to move the write region
    deploy_from_code_min: float = 25.0  # cdk deploy of a region from scratch
    restore_base_min: float = 20.0  # DynamoDB restore: fixed part
    restore_per_gb_min: float = 5.0  # ... plus this per GB (assumed, see TODO)
    repoint_min: float = 10.0  # change a table name in the stack and deploy
    backup_every_h: float = 24.0  # the daily plan in stack.py
    deletion_noticed_min: float = 20.0  # a script empties the table; alarm -> person
    corruption_noticed_h: float = 5.0  # a bad deploy writes wrong statuses
    table_gb: float = 0.02  # the cooperative's orders table (chapter 4 sizes)


DEFAULTS = Params()
STRATEGIES = ["one region", "backup & restore", "warm standby", "active-active"]
DISASTERS = [
    "region unavailable (6 h)",
    "script deletes every order",
    "bad deploy corrupts statuses (5 h)",
    "primary credentials leaked, everything there deleted",
]


@dataclass(frozen=True)
class Outcome:
    lost: str  # writes that are gone for good, or wrong
    down: str  # time the cooperative cannot take orders
    note: str = ""


def _fmt(minutes: float) -> str:
    if minutes <= 0:
        return "none"
    if minutes < 1:
        return f"{minutes * 60:.0f} s"
    if minutes < 10 and abs(minutes - round(minutes)) > 1e-9:
        return f"{minutes:.1f} min"
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours = minutes / 60
    return f"{hours:.0f} h" if abs(hours - round(hours)) < 1e-9 else f"{hours:.1f} h"


def restore_minutes(p: Params) -> float:
    return p.restore_base_min + p.restore_per_gb_min * p.table_gb


def failover_minutes(p: Params) -> float:
    return (p.health_check_s + p.dns_ttl_s) / 60


def rebuild_minutes(p: Params) -> float:
    """Notice, deploy the standby from code, restore the newest copy, point DNS at it."""
    return p.person_notices_min + p.deploy_from_code_min + restore_minutes(p) + p.repoint_min


def drill(disaster: str, strategy: str, p: Params = DEFAULTS) -> Outcome:
    """What one strategy does for one disaster, from the parameters."""
    if disaster == DISASTERS[0]:  # region unavailable
        if strategy == "one region":
            return Outcome(
                "none, if the region comes back",
                _fmt(p.outage_hours * 60),
                "nothing to fail over to; if it never comes back, everything since the last export",
            )
        if strategy == "backup & restore":
            down = rebuild_minutes(p)
            return Outcome(
                f"up to {p.backup_every_h:.0f} h (the last copy)",
                _fmt(down),
                "deploy the standby from code, restore the newest copy, point DNS at it",
            )
        if strategy == "warm standby":
            return Outcome(
                f"~{p.replication_lag_s:.0f} s of replication lag",
                f"{_fmt(failover_minutes(p))} to read and accept; "
                f"{_fmt(p.person_notices_min + p.person_decides_min)} until charged",
                "DNS fails over by itself; a person moves the write region for charge and fulfil",
            )
        return Outcome(
            f"~{p.replication_lag_s:.0f} s of replication lag",
            f"{_fmt(failover_minutes(p))} for the clients that were on the dead region",
            "writes made in both regions collide when the region returns: see `drill.py conflict`",
        )
    if disaster == DISASTERS[1]:  # script deletes every order
        down = p.deletion_noticed_min + restore_minutes(p) + p.repoint_min
        note = "PITR restore to the second before, into a new table; merge the orders placed since"
        if strategy in ("warm standby", "active-active"):
            note = f"the deletion reached the other region in ~{p.replication_lag_s:.0f} s; " + note
        return Outcome("none (PITR)", _fmt(down), note)
    if disaster == DISASTERS[2]:  # corruption
        return Outcome(
            f"none lost; wrong for {p.corruption_noticed_h:.0f} h",
            "none",
            "service stays up; repair from the PITR copy and chapter 4's event archive; "
            "every replica is equally wrong",
        )
    # credentials leaked: the primary's table, PITR and vault go together
    if strategy == "one region":
        return Outcome("everything", "until rebuilt by hand", "PITR dies with the table")
    if strategy == "backup & restore":
        return Outcome(
            f"up to {p.backup_every_h:.0f} h",
            _fmt(rebuild_minutes(p)),
            "only if the standby vault is out of the leaked credentials' reach (exercise 5)",
        )
    return Outcome(
        f"~{p.replication_lag_s:.0f} s, or up to {p.backup_every_h:.0f} h",
        f"{_fmt(failover_minutes(p))}, or {_fmt(p.deploy_from_code_min + restore_minutes(p))}",
        "the replica survives only if the credentials cannot reach that region; "
        "else the vault copy, with the same condition",
    )


def drill_table(p: Params = DEFAULTS) -> str:
    rows = ["| Disaster | " + " | ".join(STRATEGIES) + " |", "|---|" + "---|" * len(STRATEGIES)]
    for d in DISASTERS:
        cells = []
        for s in STRATEGIES:
            o = drill(d, s, p)
            cells.append(f"lost: {o.lost}; down: {o.down}")
        rows.append(f"| {d} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


# --- Drill two: a counter under last-writer-wins replication ---------------------


@dataclass(order=True)
class _Pending:
    arrive_at: float
    seq: int
    region: str = field(compare=False)
    key: str = field(compare=False)
    item: dict = field(compare=False)
    version: float = field(compare=False)


class FakeGlobalTable:
    """Two (or more) regional copies of one table, replicated asynchronously.

    Each region applies its own writes at once, conditions included, and ships
    the written item to the other regions, where it lands ``lag_s`` later.
    When two regions wrote the same item, the one with the later write time
    wins, as DynamoDB global tables resolve conflicts. The count of lost
    writes is the point of the drill.
    """

    def __init__(self, regions: list[str], lag_s: float = 1.0) -> None:
        self.regions = regions
        self.lag_s = lag_s
        self.copies: dict[str, dict[str, tuple[dict, float]]] = {r: {} for r in regions}
        self.pending: list[_Pending] = []
        self.seq = 0
        self.conflicts = 0  # deliveries that met a different local write (2 per conflict)
        self.condition_failures = 0

    def put(self, region: str, key: str, item: dict, now: float) -> None:
        self.copies[region][key] = (dict(item), now)
        self._ship(region, key, item, now)

    def decrement_if_enough(self, region: str, key: str, qty: int, now: float) -> bool:
        """``SET available = available - :q`` with ``available >= :q``, in one region."""
        item, _ = self.copies[region][key]
        if item["available"] < qty:
            self.condition_failures += 1
            return False
        new = {**item, "available": item["available"] - qty}
        self.copies[region][key] = (new, now)
        self._ship(region, key, new, now)
        return True

    def _ship(self, origin: str, key: str, item: dict, now: float) -> None:
        for r in self.regions:
            if r != origin:
                self.seq += 1
                heapq.heappush(
                    self.pending, _Pending(now + self.lag_s, self.seq, r, key, dict(item), now)
                )

    def settle(self, until: float) -> None:
        """Deliver every replication due by ``until``, last writer wins."""
        while self.pending and self.pending[0].arrive_at <= until:
            p = heapq.heappop(self.pending)
            current = self.copies[p.region].get(p.key)
            if current is not None and current[0] != p.item:
                self.conflicts += 1  # counted in each region that saw it: 2 per conflict
            if current is None or p.version >= current[1]:
                self.copies[p.region][p.key] = (p.item, p.version)
            # else the local write is newer: it stays and the replicated one is dropped

    def read(self, region: str, key: str) -> dict:
        return self.copies[region][key][0]


@dataclass(frozen=True)
class ConflictResult:
    stock_start: int
    reserved: int
    stock_end: dict[str, int]
    forgotten: int  # reservations the counter does not know about
    oversold: int  # units promised beyond the shelf
    backordered: int


def reserve_from_regions(
    orders: list[tuple[str, int]], stock: int = 10, lag_s: float = 1.0, sku: str = "SHT31-NODE"
) -> ConflictResult:
    """Each order ``(region, qty)`` is fulfilled in the region named, all at t = 0."""
    regions = ["us-east-1", "eu-west-1"]
    table = FakeGlobalTable(regions, lag_s=lag_s)
    table.put("us-east-1", sku, {"sku": sku, "available": stock}, now=-10.0)
    table.settle(until=0.0)
    reserved = backordered = 0
    for i, (region, qty) in enumerate(orders):
        if table.decrement_if_enough(region, sku, qty, now=0.0 + i * 1e-3):
            reserved += qty
        else:
            backordered += 1
    table.settle(until=60.0)
    end = {r: table.read(r, sku)["available"] for r in regions}
    forgotten = reserved - (stock - min(end.values()))
    oversold = max(0, reserved - stock)
    return ConflictResult(stock, reserved, end, forgotten, oversold, backordered)


def conflict_report() -> str:
    both = reserve_from_regions([("us-east-1", 6), ("eu-west-1", 6)])
    one = reserve_from_regions([("us-east-1", 6), ("us-east-1", 6)])
    rows = [
        "| | active-active (one order in each region) "
        "| one write region (both orders in us-east-1) |",
        "|---|---:|---:|",
        f"| units on the shelf | {both.stock_start} | {one.stock_start} |",
        f"| units reserved | {both.reserved} | {one.reserved} |",
        f"| `available` after replication settles | {both.stock_end['us-east-1']} in both regions "
        f"| {one.stock_end['us-east-1']} in both regions |",
        f"| reservations the counter lost | {both.forgotten} | {one.forgotten} |",
        f"| units promised beyond the shelf | {both.oversold} | {one.oversold} |",
        f"| orders backordered | {both.backordered} | {one.backordered} |",
    ]
    return "\n".join(rows)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "conflict":
        print(conflict_report())
        print()
        print(
            "Each region checked `available >= 6` against its own copy, both saw 10, both wrote 4; "
            "last writer wins keeps 4. The condition held in each region and failed across them."
        )
    else:
        p = Params()
        print(drill_table(p))
        print()
        print(
            f"Parameters (illustrative, drill.py): outage {p.outage_hours:.0f} h, replication lag "
            f"{p.replication_lag_s:.0f} s, health check {p.health_check_s:.0f} s + TTL "
            f"{p.dns_ttl_s:.0f} s, a person notices in {p.person_notices_min:.0f} min and "
            f"decides in {p.person_decides_min:.0f}, deploy from code "
            f"{p.deploy_from_code_min:.0f} min, "
            f"restore {restore_minutes(p):.0f} min for {p.table_gb} GB, backups every "
            f"{p.backup_every_h:.0f} h."
        )
