from datetime import UTC, datetime, timedelta
from pathlib import Path

from queue_triage.ingestion import map_rows, read_table, suggest_mapping, validate_table
from queue_triage.storage import SQLiteStore
from queue_triage.triage import (
    DEFAULT_SLA_POLICY,
    PolicyDefinition,
    Ticket,
    apply_sla_policy,
    filter_queue,
    load_csv_bytes,
    prioritize_queue,
    summarize_queue,
    triage_csv_bytes,
)
from queue_triage.workflow import apply_decisions, compare_queues, trend_report, workload_report

FIXTURE = Path(__file__).parents[1] / "data" / "tickets.csv"


def test_fixture_has_valid_rows_and_actionable_rejections():
    result = load_csv_bytes(FIXTURE.read_bytes())

    assert len(result.valid) == 30
    assert len(result.rejected) == 2
    assert "duplicate" in result.rejected[0].reason
    assert "ISO-8601" in result.rejected[1].reason


def test_sla_boundary_is_breached_at_due_time():
    created = datetime(2026, 1, 20, 8, tzinfo=UTC)
    ticket = Ticket("BOUNDARY", created, "open", "A", "Team", "Boundary check", "urgent")

    before = apply_sla_policy([ticket], created + timedelta(hours=4) - timedelta(seconds=1))[0]
    at_due = apply_sla_policy([ticket], created + timedelta(hours=4))[0]

    assert before["sla_state"] == "At risk"
    assert at_due["sla_state"] == "Breached"


def test_closed_ticket_does_not_breach_after_due_time():
    created = datetime(2026, 1, 20, 8, tzinfo=UTC)
    ticket = Ticket("CLOSED", created, "closed", "A", "Team", "Closed check", "urgent")

    row = apply_sla_policy([ticket], created + timedelta(days=10))[0]

    assert row["sla_state"] == "Closed"


def test_priority_is_explainable_and_deterministic():
    as_of = datetime(2026, 1, 21, 12, tzinfo=UTC)
    tickets = [
        Ticket("LOW", datetime(2026, 1, 21, 11, tzinfo=UTC), "open", "A", "Team", "Low", "low"),
        Ticket("URGENT", datetime(2026, 1, 20, 8, tzinfo=UTC), "open", "Unassigned", "Team", "Urgent", "urgent"),
    ]

    rows = prioritize_queue(apply_sla_policy(tickets, as_of, DEFAULT_SLA_POLICY))

    assert rows[0]["ticket_id"] == "URGENT"
    assert rows[0]["priority_score"] > rows[1]["priority_score"]
    assert "Breached SLA" in rows[0]["why_priority"]


def test_filters_and_empty_summary_are_safe():
    rows = [{"sla_state": "Breached", "status": "open", "assignee": "A", "team": "T"}]

    assert filter_queue(rows, {"Closed"}) == []
    assert summarize_queue([]) == {"total": 0, "open": 0, "breached": 0, "at_risk": 0, "unassigned": 0}


def test_export_has_stable_columns():
    row = apply_sla_policy(
        [Ticket("EXPORT", datetime(2026, 1, 20, 8, tzinfo=UTC), "open", "A", "Team", "Export", "high")],
        datetime(2026, 1, 20, 9, tzinfo=UTC),
    )[0]
    output = triage_csv_bytes(prioritize_queue([row])).decode()

    assert output.splitlines()[0].startswith("ticket_id,sla_state,priority_score,why_priority")
    assert "EXPORT" in output


def test_alias_mapping_and_malformed_data_are_visible():
    content = b"ID,Opened At,State,Owner,Group,Title,Severity\nA-1,2026-01-20T08:00:00+03:30,open,,Core,Access issue,urgent\nA-2,not-a-date,open,Sam,Core,Broken,high\n"
    table = read_table(content, "tickets.csv")
    mapping = suggest_mapping(table.headers)
    result = validate_table(table, mapping)

    assert mapping["ticket_id"] == "ID"
    assert len(result.valid) == 1
    assert result.valid[0].created_at.isoformat() == "2026-01-20T04:30:00+00:00"
    assert "ISO-8601" in result.rejected[0].reason
    assert map_rows(table, mapping)[0]["team"] == "Core"


def test_team_policy_version_changes_due_time_and_state():
    created = datetime(2026, 1, 20, 8, tzinfo=UTC)
    ticket = Ticket("POLICY", created, "open", "A", "Core", "Policy check", "urgent")
    policy = PolicyDefinition("Core fast lane", 2, DEFAULT_SLA_POLICY, {"Core": {"urgent": 2}})

    row = apply_sla_policy([ticket], created + timedelta(hours=3), policy)[0]

    assert row["sla_hours"] == 2
    assert row["sla_state"] == "Breached"
    assert row["policy_version"] == 2


def test_sqlite_import_snapshot_replay_and_decision_audit(tmp_path):
    store = SQLiteStore(tmp_path / "triage.sqlite3")
    ticket = Ticket("SQL-1", datetime(2026, 1, 20, 8, tzinfo=UTC), "open", "A", "Core", "Persist", "urgent")
    mapping = {"ticket_id": "ticket_id"}
    first = store.import_tickets("demo.csv", b"same", mapping, [ticket], [])
    replay = store.import_tickets("demo.csv", b"same", mapping, [ticket], [])
    queue = prioritize_queue(apply_sla_policy([ticket], datetime(2026, 1, 20, 13, tzinfo=UTC)))
    snapshot_id, snapshot_replayed = store.create_snapshot(first.id, PolicyDefinition("Test", 1, DEFAULT_SLA_POLICY), datetime(2026, 1, 20, 13, tzinfo=UTC), queue, "key")
    same_snapshot, replayed_again = store.create_snapshot(first.id, PolicyDefinition("Test", 1, DEFAULT_SLA_POLICY), datetime(2026, 1, 20, 13, tzinfo=UTC), queue, "key")
    store.record_decision(snapshot_id, "SQL-1", "escalate", "Checked customer impact", "Breached", int(queue[0]["priority_score"]))

    assert replay.replayed is True
    assert replay.id == first.id
    assert snapshot_replayed is False
    assert replayed_again is True
    assert same_snapshot == snapshot_id
    assert store.decisions_for_snapshot(snapshot_id)[0]["decision"] == "escalate"
    assert apply_decisions(queue, store.decisions_for_snapshot(snapshot_id))[0]["decision"] == "escalate"
    store.close()


def test_history_comparison_workload_and_trend():
    old = [{"ticket_id": "A", "status": "open", "assignee": "Unassigned", "team": "Core", "priority": "high", "subject": "Old", "sla_state": "Breached", "priority_score": 130, "due_at": "y"}]
    new = [{"ticket_id": "A", "status": "resolved", "assignee": "Sam", "team": "Core", "priority": "high", "subject": "Old", "sla_state": "Closed", "priority_score": 0, "due_at": "y"}, {"ticket_id": "B", "status": "open", "assignee": "Sam", "team": "Core", "priority": "normal", "subject": "New", "sla_state": "At risk", "priority_score": 60, "due_at": "z"}]
    comparison = compare_queues(old, new)
    workload = workload_report(new)
    trend = trend_report([{"id": 1, "as_of": "2026-01-20", "queue": old}, {"id": 2, "as_of": "2026-01-21", "queue": new}])

    assert comparison["counts"] == {"added": 1, "removed": 0, "changed": 1, "unchanged": 0}
    assert workload[0]["assignee"] == "Sam"
    assert trend[-1]["resolved"] == 1
