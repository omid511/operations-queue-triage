"""CI smoke check using only the project's pure domain module."""

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from queue_triage import (
    DEFAULT_POLICY,
    DEFAULT_SLA_POLICY,
    apply_sla_policy,
    load_csv_bytes,
    prioritize_queue,
    summarize_queue,
)
from queue_triage.storage import SQLiteStore

fixture = Path(__file__).parents[1] / "data" / "tickets.csv"
result = load_csv_bytes(fixture.read_bytes())
assert len(result.valid) == 30, len(result.valid)
assert len(result.rejected) == 2, len(result.rejected)
as_of = datetime(2026, 1, 24, 12, tzinfo=UTC)
queue = prioritize_queue(apply_sla_policy(result.valid, as_of, DEFAULT_SLA_POLICY))
summary = summarize_queue(queue)
assert summary["open"] > 0
assert summary["breached"] > 0
assert queue[0]["why_priority"]
with TemporaryDirectory() as temporary_directory:
    store = SQLiteStore(Path(temporary_directory) / "triage.sqlite3")
    mapping = {column: column for column in result.columns}
    first = store.import_tickets("tickets.csv", fixture.read_bytes(), mapping, result.valid, result.rejected)
    replay = store.import_tickets("tickets.csv", fixture.read_bytes(), mapping, result.valid, result.rejected)
    snapshot_id, _ = store.create_snapshot(first.id, DEFAULT_POLICY, as_of, queue, "smoke-key")
    same_snapshot, replayed = store.create_snapshot(first.id, DEFAULT_POLICY, as_of, queue, "smoke-key")
    assert first.id == replay.id and replay.replayed
    assert snapshot_id == same_snapshot and replayed
    store.close()
print(
    f"smoke ok: {summary['total']} valid tickets, {summary['breached']} breached, top={queue[0]['ticket_id']}, SQLite replay ok"
)
