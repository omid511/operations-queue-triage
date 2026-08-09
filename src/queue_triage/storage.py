"""Small SQLite repository for imports, snapshots, policies, decisions, and views."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Mapping

from .triage import PolicyDefinition, RejectedRow, Ticket


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _dump(value: object) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True)


def _load(value: str) -> object:
    return json.loads(value)


def _ticket_payload(ticket: Ticket) -> dict[str, object]:
    return {
        "ticket_id": ticket.ticket_id,
        "created_at": ticket.created_at.isoformat(),
        "status": ticket.status,
        "assignee": ticket.assignee,
        "team": ticket.team,
        "subject": ticket.subject,
        "priority": ticket.priority,
        "last_updated_at": ticket.last_updated_at.isoformat() if ticket.last_updated_at else None,
    }


def _row_payload(row: Mapping[str, object]) -> dict[str, object]:
    return dict(row)


def _restore_datetime_fields(row: dict[str, object]) -> dict[str, object]:
    for field in ("created_at", "due_at", "last_updated_at", "decision_at"):
        value = row.get(field)
        if isinstance(value, str) and value:
            try:
                row[field] = datetime.fromisoformat(value)
            except ValueError:
                pass
    return row


@dataclass(frozen=True)
class ImportRecord:
    id: int
    source_name: str
    source_hash: str
    valid_count: int
    rejected_count: int
    replayed: bool


class SQLiteStore:
    """A deliberately boring repository; SQLite is enough for a local demo."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY,
                source_name TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                mapping_json TEXT NOT NULL,
                valid_count INTEGER NOT NULL,
                rejected_count INTEGER NOT NULL,
                tickets_json TEXT NOT NULL,
                rejected_json TEXT NOT NULL,
                UNIQUE(source_hash, mapping_json)
            );
            CREATE TABLE IF NOT EXISTS policies (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                version INTEGER NOT NULL,
                policy_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(name, version)
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY,
                import_id INTEGER NOT NULL REFERENCES imports(id),
                policy_id INTEGER NOT NULL REFERENCES policies(id),
                as_of TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(import_id, policy_id, as_of)
            );
            CREATE TABLE IF NOT EXISTS snapshot_tickets (
                id INTEGER PRIMARY KEY,
                snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                ticket_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(snapshot_id, ticket_id)
            );
            CREATE TABLE IF NOT EXISTS decision_events (
                id INTEGER PRIMARY KEY,
                snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                ticket_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                note TEXT NOT NULL,
                previous_state TEXT NOT NULL,
                previous_score INTEGER NOT NULL,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saved_views (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                filters_json TEXT NOT NULL,
                policy_id INTEGER REFERENCES policies(id),
                created_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def seed_policy(self, policy: PolicyDefinition) -> int:
        existing = self.connection.execute(
            "SELECT id FROM policies WHERE name = ? AND version = ?", (policy.name, policy.version)
        ).fetchone()
        if existing:
            return int(existing["id"])
        cursor = self.connection.execute(
            "INSERT INTO policies(name, version, policy_json, created_at) VALUES (?, ?, ?, ?)",
            (policy.name, policy.version, _dump(policy.to_dict()), _now()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_policies(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT id, name, version, policy_json, created_at FROM policies ORDER BY name, version DESC"
        ).fetchall()
        return [{**dict(row), "policy": PolicyDefinition.from_dict(_load(row["policy_json"]))} for row in rows]

    def import_tickets(
        self,
        source_name: str,
        source_bytes: bytes,
        mapping: Mapping[str, str | None],
        tickets: Iterable[Ticket],
        rejected: Iterable[RejectedRow],
    ) -> ImportRecord:
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        mapping_json = _dump(dict(mapping))
        existing = self.connection.execute(
            "SELECT id, source_name, source_hash, valid_count, rejected_count FROM imports WHERE source_hash = ? AND mapping_json = ?",
            (source_hash, mapping_json),
        ).fetchone()
        if existing:
            return ImportRecord(
                int(existing["id"]),
                existing["source_name"],
                existing["source_hash"],
                int(existing["valid_count"]),
                int(existing["rejected_count"]),
                True,
            )
        valid = list(tickets)
        invalid = list(rejected)
        rejected_payload = [
            {"row_number": row.row_number, "ticket_id": row.ticket_id, "reason": row.reason, "values": dict(row.values)}
            for row in invalid
        ]
        cursor = self.connection.execute(
            "INSERT INTO imports(source_name, source_hash, imported_at, mapping_json, valid_count, rejected_count, tickets_json, rejected_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_name,
                source_hash,
                _now(),
                mapping_json,
                len(valid),
                len(invalid),
                _dump([_ticket_payload(ticket) for ticket in valid]),
                _dump(rejected_payload),
            ),
        )
        self.connection.commit()
        return ImportRecord(int(cursor.lastrowid), source_name, source_hash, len(valid), len(invalid), False)

    def tickets_for_import(self, import_id: int) -> list[Ticket]:
        row = self.connection.execute("SELECT tickets_json FROM imports WHERE id = ?", (import_id,)).fetchone()
        if not row:
            return []
        tickets = []
        for value in _load(row["tickets_json"]):
            tickets.append(
                Ticket(
                    value["ticket_id"],
                    datetime.fromisoformat(value["created_at"]),
                    value["status"],
                    value["assignee"],
                    value["team"],
                    value["subject"],
                    value["priority"],
                    datetime.fromisoformat(value["last_updated_at"]) if value.get("last_updated_at") else None,
                )
            )
        return tickets

    def create_snapshot(
        self,
        import_id: int,
        policy: PolicyDefinition,
        as_of: datetime,
        queue: Iterable[Mapping[str, object]],
        input_hash: str,
    ) -> tuple[int, bool]:
        policy_id = self.seed_policy(policy)
        as_of_value = as_of.isoformat()
        existing = self.connection.execute(
            "SELECT id FROM snapshots WHERE import_id = ? AND policy_id = ? AND as_of = ?",
            (import_id, policy_id, as_of_value),
        ).fetchone()
        if existing:
            return int(existing["id"]), True
        cursor = self.connection.execute(
            "INSERT INTO snapshots(import_id, policy_id, as_of, input_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (import_id, policy_id, as_of_value, input_hash, _now()),
        )
        snapshot_id = int(cursor.lastrowid)
        self.connection.executemany(
            "INSERT INTO snapshot_tickets(snapshot_id, ticket_id, payload_json) VALUES (?, ?, ?)",
            [(snapshot_id, str(row["ticket_id"]), _dump(_row_payload(row))) for row in queue],
        )
        self.connection.commit()
        return snapshot_id, False

    def list_imports(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT id, source_name, source_hash, imported_at, valid_count, rejected_count FROM imports ORDER BY id DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_snapshots(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT s.id, s.import_id, s.policy_id, s.as_of, s.input_hash, s.created_at, i.source_name, p.name AS policy_name, p.version AS policy_version "
            "FROM snapshots s JOIN imports i ON i.id = s.import_id JOIN policies p ON p.id = s.policy_id ORDER BY s.as_of DESC, s.id DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def snapshot_queue(self, snapshot_id: int) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT payload_json FROM snapshot_tickets WHERE snapshot_id = ? ORDER BY id", (snapshot_id,)
        ).fetchall()
        return [_restore_datetime_fields(dict(_load(row["payload_json"]))) for row in rows]

    def snapshot_bundle(self, snapshot_id: int) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT id, import_id, policy_id, as_of, input_hash, created_at FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if not row:
            return None
        return {**dict(row), "queue": self.snapshot_queue(snapshot_id)}

    def record_decision(
        self,
        snapshot_id: int,
        ticket_id: str,
        decision: str,
        note: str,
        previous_state: str,
        previous_score: int,
        actor: str = "operator",
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO decision_events(snapshot_id, ticket_id, decision, note, previous_state, previous_score, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                ticket_id,
                decision,
                note.strip(),
                previous_state,
                int(previous_score),
                actor.strip() or "operator",
                _now(),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def decisions_for_snapshot(self, snapshot_id: int) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT id, snapshot_id, ticket_id, decision, note, previous_state, previous_score, actor, created_at FROM decision_events WHERE snapshot_id = ? ORDER BY id",
            (snapshot_id,),
        ).fetchall()
        return [{**dict(row), "created_at": datetime.fromisoformat(row["created_at"])} for row in rows]

    def save_view(self, name: str, filters: Mapping[str, object], policy: PolicyDefinition | None = None) -> int:
        policy_id = self.seed_policy(policy) if policy else None
        self.connection.execute(
            "INSERT INTO saved_views(name, filters_json, policy_id, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET filters_json=excluded.filters_json, policy_id=excluded.policy_id",
            (name.strip(), _dump(dict(filters)), policy_id, _now()),
        )
        self.connection.commit()
        row = self.connection.execute("SELECT id FROM saved_views WHERE name = ?", (name.strip(),)).fetchone()
        return int(row["id"])

    def list_views(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT id, name, filters_json, policy_id, created_at FROM saved_views ORDER BY name"
        ).fetchall()
        return [{**dict(row), "filters": _load(row["filters_json"])} for row in rows]

    def decision_history(self, limit: int = 100) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT d.*, s.as_of, i.source_name FROM decision_events d JOIN snapshots s ON s.id = d.snapshot_id JOIN imports i ON i.id = s.import_id ORDER BY d.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
