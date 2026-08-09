"""Pure, deterministic ticket ingestion and queue-prioritization functions.

Policy: timestamps without an offset are interpreted as UTC. SLA due time is
created_at plus the priority's configured duration. At-risk means the ticket
is open and inside the final quarter of its SLA window (with a one-hour
minimum window). The as-of timestamp is always supplied by the caller.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable, Mapping, Sequence

REQUIRED_COLUMNS = (
    "ticket_id",
    "created_at",
    "status",
    "assignee",
    "team",
    "subject",
    "priority",
)
OPTIONAL_COLUMNS = ("last_updated_at",)
ALLOWED_PRIORITIES = ("urgent", "high", "normal", "low")
ALLOWED_STATUSES = ("open", "pending", "in_progress", "resolved", "closed", "cancelled")
CLOSED_STATUSES = frozenset({"resolved", "closed", "cancelled"})
DEFAULT_SLA_POLICY = {"urgent": 4, "high": 8, "normal": 24, "low": 72}
STANDARD_POLICY_NAME = "Standard response windows"


@dataclass(frozen=True)
class PolicyDefinition:
    """Versioned SLA policy with optional team-specific windows."""

    name: str
    version: int
    default_hours: Mapping[str, int]
    team_hours: Mapping[str, Mapping[str, int]] | None = None
    risk_fraction: float = 0.25
    minimum_warning_hours: float = 1.0

    def hours_for(self, priority: str, team: str) -> int:
        team_policy = (self.team_hours or {}).get(team, {})
        return int(team_policy.get(priority, self.default_hours[priority]))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "default_hours": dict(self.default_hours),
            "team_hours": {team: dict(hours) for team, hours in (self.team_hours or {}).items()},
            "risk_fraction": self.risk_fraction,
            "minimum_warning_hours": self.minimum_warning_hours,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "PolicyDefinition":
        return cls(
            str(value["name"]),
            int(value["version"]),
            {str(key): int(hours) for key, hours in dict(value["default_hours"]).items()},
            {
                str(team): {str(key): int(hours) for key, hours in dict(hours).items()}
                for team, hours in dict(value.get("team_hours", {})).items()
            },
            float(value.get("risk_fraction", 0.25)),
            float(value.get("minimum_warning_hours", 1.0)),
        )


DEFAULT_POLICY = PolicyDefinition(STANDARD_POLICY_NAME, 1, DEFAULT_SLA_POLICY)


@dataclass(frozen=True)
class Ticket:
    """A validated source ticket with normalized values."""

    ticket_id: str
    created_at: datetime
    status: str
    assignee: str
    team: str
    subject: str
    priority: str
    last_updated_at: datetime | None = None


@dataclass(frozen=True)
class RejectedRow:
    row_number: int
    ticket_id: str
    reason: str
    values: Mapping[str, str]


@dataclass(frozen=True)
class ValidationResult:
    valid: tuple[Ticket, ...]
    rejected: tuple[RejectedRow, ...]
    columns: tuple[str, ...]
    missing_columns: tuple[str, ...] = ()


def parse_timestamp(value: str, field_name: str = "timestamp") -> datetime:
    """Parse an ISO timestamp, treating an omitted offset as UTC."""

    raw = value.strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601 (example: 2026-01-20T09:00Z)") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _clean(value: object) -> str:
    return str(value or "").strip()


def validate_rows(rows: Iterable[Mapping[str, object]], columns: Sequence[str] | None = None) -> ValidationResult:
    """Validate and normalize raw CSV rows, retaining an actionable rejection list."""

    row_list = [dict(row) for row in rows]
    discovered = tuple(columns or (tuple(row_list[0].keys()) if row_list else ()))
    missing = tuple(column for column in REQUIRED_COLUMNS if column not in discovered)
    if missing:
        rejected = tuple(
            RejectedRow(index, _clean(row.get("ticket_id")), f"Missing required columns: {', '.join(missing)}", row)
            for index, row in enumerate(row_list, start=2)
        )
        return ValidationResult((), rejected, discovered, missing)

    valid: list[Ticket] = []
    rejected: list[RejectedRow] = []
    seen_ids: set[str] = set()
    for row_number, row in enumerate(row_list, start=2):
        ticket_id = _clean(row.get("ticket_id"))
        try:
            if not ticket_id:
                raise ValueError("ticket_id is required")
            if ticket_id in seen_ids:
                raise ValueError("duplicate ticket_id; only the first occurrence is kept")
            created_at = parse_timestamp(_clean(row.get("created_at")), "created_at")
            status = _clean(row.get("status")).lower()
            if status not in ALLOWED_STATUSES:
                raise ValueError(f"status must be one of: {', '.join(ALLOWED_STATUSES)}")
            priority = _clean(row.get("priority")).lower()
            if priority not in ALLOWED_PRIORITIES:
                raise ValueError(f"priority must be one of: {', '.join(ALLOWED_PRIORITIES)}")
            assignee = _clean(row.get("assignee")) or "Unassigned"
            team = _clean(row.get("team")) or "Unassigned"
            subject = _clean(row.get("subject"))
            if not subject:
                raise ValueError("subject is required")
            last_updated_raw = _clean(row.get("last_updated_at"))
            last_updated_at = parse_timestamp(last_updated_raw, "last_updated_at") if last_updated_raw else None
            if last_updated_at and last_updated_at < created_at:
                raise ValueError("last_updated_at cannot be earlier than created_at")
            valid.append(Ticket(ticket_id, created_at, status, assignee, team, subject, priority, last_updated_at))
            seen_ids.add(ticket_id)
        except ValueError as exc:
            rejected.append(RejectedRow(row_number, ticket_id, str(exc), row))
    return ValidationResult(tuple(valid), tuple(rejected), discovered)


def load_csv_bytes(content: bytes) -> ValidationResult:
    """Decode UTF-8 CSV content and validate its rows."""

    if not content.strip():
        return ValidationResult((), (RejectedRow(1, "", "CSV is empty", {}),), ())
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ValidationResult((), (RejectedRow(1, "", "CSV must be UTF-8 encoded", {}),), ())
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return ValidationResult((), (RejectedRow(1, "", "CSV must include a header row", {}),), ())
    rows = list(reader)
    return validate_rows(rows, tuple(reader.fieldnames))


def _as_of_utc(as_of: datetime) -> datetime:
    if as_of.tzinfo is None:
        return as_of.replace(tzinfo=UTC)
    return as_of.astimezone(UTC)


def apply_sla_policy(
    tickets: Iterable[Ticket],
    as_of: datetime,
    sla_policy: Mapping[str, int] | PolicyDefinition = DEFAULT_SLA_POLICY,
) -> list[dict[str, object]]:
    """Add due time, SLA state, age, and an auditable explanation to each ticket."""

    now = _as_of_utc(as_of)
    output: list[dict[str, object]] = []
    for ticket in tickets:
        if isinstance(sla_policy, PolicyDefinition):
            sla_hours = sla_policy.hours_for(ticket.priority, ticket.team)
            risk_fraction = sla_policy.risk_fraction
            minimum_warning_hours = sla_policy.minimum_warning_hours
            policy_name = sla_policy.name
            policy_version = sla_policy.version
        else:
            sla_hours = int(sla_policy[ticket.priority])
            risk_fraction = 0.25
            minimum_warning_hours = 1.0
            policy_name = STANDARD_POLICY_NAME
            policy_version = 1
        due_at = ticket.created_at + timedelta(hours=sla_hours)
        age_hours = max(0.0, (now - ticket.created_at).total_seconds() / 3600)
        remaining_hours = (due_at - now).total_seconds() / 3600
        risk_window = max(minimum_warning_hours, sla_hours * risk_fraction)
        if ticket.status in CLOSED_STATUSES:
            sla_state = "Closed"
        elif remaining_hours <= 0:
            sla_state = "Breached"
        elif remaining_hours <= risk_window:
            sla_state = "At risk"
        else:
            sla_state = "On track"
        output.append(
            {
                "ticket_id": ticket.ticket_id,
                "created_at": ticket.created_at,
                "due_at": due_at,
                "status": ticket.status,
                "assignee": ticket.assignee,
                "team": ticket.team,
                "subject": ticket.subject,
                "priority": ticket.priority,
                "last_updated_at": ticket.last_updated_at,
                "age_hours": round(age_hours, 1),
                "remaining_hours": round(remaining_hours, 1),
                "sla_state": sla_state,
                "sla_hours": sla_hours,
                "policy_name": policy_name,
                "policy_version": policy_version,
            }
        )
    return output


def _priority_score(row: Mapping[str, object]) -> int:
    state_weight = {"Breached": 100, "At risk": 60, "On track": 20, "Closed": 0}
    priority_weight = {"urgent": 40, "high": 30, "normal": 20, "low": 10}
    score = state_weight[str(row["sla_state"])] + priority_weight[str(row["priority"])]
    if str(row["assignee"]) == "Unassigned":
        score += 15
    score += min(20, int(float(row["age_hours"]) // 12))
    return score


def _explanation(row: Mapping[str, object]) -> str:
    state = str(row["sla_state"])
    bits = [f"{state} SLA", f"{str(row['priority']).title()} priority"]
    if str(row["assignee"]) == "Unassigned":
        bits.append("unassigned")
    if state == "Breached":
        bits.append(f"due {row['due_at'].strftime('%b %-d %H:%M')} UTC")
    elif state == "At risk":
        bits.append(f"{row['remaining_hours']}h left")
    else:
        bits.append(f"{row['age_hours']}h old")
    return "; ".join(bits)


def prioritize_queue(rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Score and sort rows deterministically, with a reason for every score."""

    prioritized = []
    for row in rows:
        enriched = dict(row)
        enriched["priority_score"] = _priority_score(enriched)
        enriched["why_priority"] = _explanation(enriched)
        prioritized.append(enriched)
    return sorted(
        prioritized,
        key=lambda row: (
            -int(row["priority_score"]),
            row["due_at"],
            str(row["ticket_id"]),
        ),
    )


def filter_queue(
    rows: Iterable[Mapping[str, object]],
    states: set[str] | None = None,
    assignees: set[str] | None = None,
    teams: set[str] | None = None,
) -> list[dict[str, object]]:
    """Apply exact-match filters while preserving the input order."""

    return [
        dict(row)
        for row in rows
        if (not states or str(row["sla_state"]) in states)
        and (not assignees or str(row["assignee"]) in assignees)
        and (not teams or str(row["team"]) in teams)
    ]


def summarize_queue(rows: Iterable[Mapping[str, object]]) -> dict[str, int]:
    rows_list = list(rows)
    return {
        "total": len(rows_list),
        "open": sum(str(row["status"]) not in CLOSED_STATUSES for row in rows_list),
        "breached": sum(str(row["sla_state"]) == "Breached" for row in rows_list),
        "at_risk": sum(str(row["sla_state"]) == "At risk" for row in rows_list),
        "unassigned": sum(str(row["assignee"]) == "Unassigned" for row in rows_list),
    }


def policy_fingerprint(sla_policy: Mapping[str, int] | PolicyDefinition, as_of: datetime, input_bytes: bytes) -> str:
    """Return a short reproducibility key for the displayed queue."""

    policy = json.dumps(
        sla_policy.to_dict() if isinstance(sla_policy, PolicyDefinition) else dict(sla_policy), sort_keys=True
    )
    payload = f"as_of={_as_of_utc(as_of).isoformat()}|{policy}|".encode() + input_bytes
    return hashlib.sha256(payload).hexdigest()[:12]


def _format_csv_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return "" if value is None else str(value)


def triage_csv_bytes(rows: Iterable[Mapping[str, object]]) -> bytes:
    """Serialize a queue view for handoff to the operator's next tool."""

    fields = (
        "ticket_id",
        "sla_state",
        "priority_score",
        "why_priority",
        "priority",
        "status",
        "assignee",
        "team",
        "subject",
        "created_at",
        "due_at",
        "age_hours",
        "remaining_hours",
    )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _format_csv_value(row.get(field)) for field in fields})
    return buffer.getvalue().encode("utf-8")


def rejected_csv_bytes(rows: Iterable[RejectedRow]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(("row_number", "ticket_id", "reason"))
    for row in rows:
        writer.writerow((row.row_number, row.ticket_id, row.reason))
    return buffer.getvalue().encode("utf-8")
