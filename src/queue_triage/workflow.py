"""Pure workflow comparisons and reporting helpers."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping


def compare_queues(previous: Iterable[Mapping[str, object]], current: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Explain what changed between two imported snapshots."""

    old = {str(row["ticket_id"]): row for row in previous}
    new = {str(row["ticket_id"]): row for row in current}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = []
    fields = ("status", "assignee", "team", "priority", "subject", "sla_state", "priority_score", "due_at")
    for ticket_id in sorted(set(old) & set(new)):
        differences = {
            field: {"before": old[ticket_id].get(field), "after": new[ticket_id].get(field)}
            for field in fields
            if str(old[ticket_id].get(field)) != str(new[ticket_id].get(field))
        }
        if differences:
            changed.append({"ticket_id": ticket_id, "fields": differences})
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": len(set(old) & set(new)) - len(changed),
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed), "unchanged": len(set(old) & set(new)) - len(changed)},
    }


def apply_decisions(rows: Iterable[Mapping[str, object]], decisions: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Decorate a queue with the latest recorded decision for each ticket."""

    latest: dict[str, Mapping[str, object]] = {}
    for decision in decisions:
        latest[str(decision["ticket_id"])] = decision
    output = []
    for row in rows:
        enriched = dict(row)
        decision = latest.get(str(row["ticket_id"]))
        enriched["decision"] = decision["decision"] if decision else "Unreviewed"
        enriched["decision_note"] = decision["note"] if decision else ""
        enriched["decision_at"] = decision["created_at"] if decision else None
        enriched["effective_state"] = "Resolved by override" if decision and decision["decision"] == "resolve" else row["sla_state"]
        output.append(enriched)
    return output


def workload_report(rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Summarize ownership load and risk without hiding unassigned work."""

    buckets: dict[tuple[str, str], dict[str, object]] = defaultdict(lambda: {"team": "", "assignee": "", "tickets": 0, "open": 0, "breached": 0, "at_risk": 0})
    for row in rows:
        key = (str(row["team"]), str(row["assignee"]))
        bucket = buckets[key]
        bucket["team"], bucket["assignee"] = key
        bucket["tickets"] += 1
        bucket["open"] += str(row["status"]) not in {"resolved", "closed", "cancelled"}
        bucket["breached"] += str(row["sla_state"]) == "Breached"
        bucket["at_risk"] += str(row["sla_state"]) == "At risk"
    return sorted(buckets.values(), key=lambda row: (-int(row["breached"]), -int(row["open"]), str(row["team"]), str(row["assignee"])))


def trend_report(snapshots: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return one compact trend row per persisted as-of snapshot."""

    rows = []
    for snapshot in snapshots:
        queue = list(snapshot.get("queue", []))
        rows.append(
            {
                "snapshot_id": snapshot["id"],
                "as_of": snapshot["as_of"],
                "tickets": len(queue),
                "open": sum(str(row["status"]) not in {"resolved", "closed", "cancelled"} for row in queue),
                "breached": sum(str(row["sla_state"]) == "Breached" for row in queue),
                "at_risk": sum(str(row["sla_state"]) == "At risk" for row in queue),
                "resolved": sum(str(row["status"]) in {"resolved", "closed", "cancelled"} for row in queue),
                "unassigned": sum(str(row["assignee"]) == "Unassigned" for row in queue),
            }
        )
    return sorted(rows, key=lambda row: str(row["as_of"]))
