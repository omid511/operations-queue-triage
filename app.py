"""Streamlit entrypoint for the end-to-end Operations Queue Triage workflow."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from queue_triage import (  # noqa: E402
    DEFAULT_POLICY,
    DEFAULT_SLA_POLICY,
    IngestionError,
    PolicyDefinition,
    apply_decisions,
    apply_sla_policy,
    compare_queues,
    filter_queue,
    policy_fingerprint,
    prioritize_queue,
    read_table,
    rejected_csv_bytes,
    suggest_mapping,
    summarize_queue,
    trend_report,
    triage_csv_bytes,
    validate_table,
    workload_report,
)
from queue_triage.storage import SQLiteStore  # noqa: E402

FIXTURE_PATH = ROOT / "data" / "tickets.csv"
DB_PATH = ROOT / "data" / "triage.sqlite3"
REQUIRED_POLICY_PRIORITIES = ("urgent", "high", "normal", "low")
DECISIONS = ("keep", "escalate", "snooze", "resolve")


def _default_as_of(tickets) -> datetime:
    latest = max(ticket.created_at for ticket in tickets)
    return (latest + timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)


def _display_time(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def _format_mapping(mapping: dict[str, str | None]) -> str:
    return ", ".join(f"{key} ← {value or 'unmapped'}" for key, value in mapping.items())


def _policy_from_form(team_names: list[str]) -> PolicyDefinition | None:
    with st.sidebar.expander("Create a policy version", expanded=False):
        st.caption("New versions are stored locally and used for reproducible snapshots.")
        name = st.text_input("Policy name", value="Operations standard", key="policy_name")
        version = st.number_input("Version", min_value=1, value=1, step=1, key="policy_version")
        defaults = {
            priority: st.number_input(f"{priority.title()} SLA hours", min_value=1, value=DEFAULT_SLA_POLICY[priority], step=1, key=f"policy_{priority}")
            for priority in REQUIRED_POLICY_PRIORITIES
        }
        override_team = st.selectbox("Team override", ["None", *team_names], key="override_team")
        team_hours = {}
        if override_team != "None":
            override = {
                priority: st.number_input(f"{override_team} · {priority.title()} hours", min_value=1, value=defaults[priority], step=1, key=f"override_{priority}")
                for priority in REQUIRED_POLICY_PRIORITIES
            }
            team_hours[override_team] = override
        risk_fraction = st.slider("At-risk fraction of SLA", 0.1, 0.5, 0.25, 0.05, key="risk_fraction")
        minimum_warning = st.number_input("Minimum warning hours", min_value=0.0, value=1.0, step=0.5, key="minimum_warning")
        if st.button("Save policy version", type="primary", key="save_policy"):
            if not name.strip():
                st.error("Policy name is required.")
            else:
                return PolicyDefinition(name.strip(), int(version), defaults, team_hours, risk_fraction, minimum_warning)
    return None


def _mapping_controls(raw_table, mapping: dict[str, str | None]) -> dict[str, str | None] | None:
    options = ["— unmapped —", *raw_table.headers]
    needs_mapping = any(mapping.get(column) is None for column in ("ticket_id", "created_at", "status", "assignee", "team", "subject", "priority"))
    with st.expander("Review source mapping", expanded=needs_mapping):
        st.caption("Map source columns to the stable ticket contract before validation. Optional fields may stay unmapped.")
        with st.form("mapping_form"):
            chosen: dict[str, str | None] = {}
            for canonical in ("ticket_id", "created_at", "status", "assignee", "team", "subject", "priority", "last_updated_at"):
                current = mapping.get(canonical)
                default_index = options.index(current) if current in options else 0
                selected = st.selectbox(canonical, options, index=default_index, key=f"map_{canonical}")
                chosen[canonical] = None if selected == options[0] else selected
            submitted = st.form_submit_button("Apply mapping")
        if submitted:
            return chosen
        st.caption(_format_mapping(mapping))
    return mapping


def _queue_table(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "Ticket": row["ticket_id"],
            "State": row.get("effective_state", row["sla_state"]),
            "Score": row["priority_score"],
            "Why this is here": row["why_priority"],
            "Decision": row.get("decision", "Unreviewed"),
            "Priority": str(row["priority"]).title(),
            "Owner": row["assignee"],
            "Team": row["team"],
            "Subject": row["subject"],
            "Due (UTC)": _display_time(row["due_at"]),
        }
        for row in rows
    ]


def _render_triage(store: SQLiteStore, snapshot_id: int, queue: list[dict[str, object]], source_bytes: bytes, policy: PolicyDefinition, as_of: datetime) -> None:
    decisions = store.decisions_for_snapshot(snapshot_id)
    queue_with_decisions = apply_decisions(queue, decisions)
    summary = summarize_queue(queue_with_decisions)
    st.subheader("Triage desk")
    metrics = st.columns(5)
    metrics[0].metric("Tickets", summary["total"])
    metrics[1].metric("Open", summary["open"])
    metrics[2].metric("Breached", summary["breached"])
    metrics[3].metric("At risk", summary["at_risk"])
    metrics[4].metric("Unassigned", summary["unassigned"])
    st.caption(f"Snapshot #{snapshot_id} · as of {_display_time(as_of)} · {policy.name} v{policy.version}")

    filter_cols = st.columns([1, 1, 1, 1.5])
    states = filter_cols[0].multiselect("SLA state", ["Breached", "At risk", "On track", "Closed"], default=["Breached", "At risk", "On track"])
    teams = filter_cols[1].multiselect("Team", sorted({str(row["team"]) for row in queue_with_decisions}))
    assignees = filter_cols[2].multiselect("Owner", sorted({str(row["assignee"]) for row in queue_with_decisions}))
    search = filter_cols[3].text_input("Find ticket or subject", placeholder="e.g. payout")
    filtered = filter_queue(queue_with_decisions, set(states), set(assignees), set(teams))
    if search.strip():
        needle = search.strip().lower()
        filtered = [row for row in filtered if needle in str(row["ticket_id"]).lower() or needle in str(row["subject"]).lower()]

    if not filtered:
        st.info("No tickets match this view. Clear a filter or import a newer snapshot to continue triage.")
    else:
        chart_data = [{"State": state, "Tickets": sum(row["sla_state"] == state for row in filtered)} for state in ("Breached", "At risk", "On track", "Closed")]
        st.bar_chart(chart_data, x="State", y="Tickets", horizontal=True, height=180)
        st.dataframe(_queue_table(filtered), hide_index=True, use_container_width=True, column_config={"Score": st.column_config.NumberColumn(format="%d")})
        st.download_button("Export current triage view", triage_csv_bytes(filtered), "triage-queue.csv", "text/csv", type="primary")

    st.markdown("#### Record a decision")
    if not filtered:
        st.caption("A ticket must be visible in the current view before it can be reviewed.")
    else:
        with st.form("decision_form"):
            ticket_ids = [str(row["ticket_id"]) for row in filtered]
            selected_id = st.selectbox("Ticket", ticket_ids)
            selected_row = next(row for row in filtered if str(row["ticket_id"]) == selected_id)
            decision = st.selectbox("Decision", DECISIONS, format_func=lambda value: value.title())
            note = st.text_area("Audit note", placeholder="What did you check or change?")
            actor = st.text_input("Operator", value="operator")
            submitted = st.form_submit_button("Save decision")
        if submitted:
            if not note.strip():
                st.error("An audit note is required for every decision.")
            else:
                store.record_decision(snapshot_id, selected_id, decision, note, str(selected_row["sla_state"]), int(selected_row["priority_score"]), actor)
                st.success(f"Recorded {decision} for {selected_id}.")
                st.rerun()

    with st.expander("Save this view"):
        with st.form("saved_view_form"):
            view_name = st.text_input("View name", placeholder="My unassigned breaches")
            save_view = st.form_submit_button("Save view")
        if save_view:
            if view_name.strip():
                store.save_view(view_name, {"states": states, "teams": teams, "assignees": assignees, "search": search}, policy)
                st.success("View saved locally.")
            else:
                st.error("A view name is required.")


def _render_reports(store: SQLiteStore) -> None:
    snapshots = []
    for metadata in store.list_snapshots():
        bundle = store.snapshot_bundle(int(metadata["id"]))
        if bundle:
            snapshots.append({**metadata, **bundle})
    if not snapshots:
        st.info("No snapshots yet. Import a ticket export to start the history.")
        return
    latest = snapshots[0]
    latest_queue = latest["queue"]
    summary = summarize_queue(latest_queue)
    st.subheader("Operational reporting")
    metrics = st.columns(5)
    metrics[0].metric("Breached", summary["breached"])
    metrics[1].metric("Aging open", sum(row["age_hours"] >= 24 and row["status"] not in {"resolved", "closed", "cancelled"} for row in latest_queue))
    metrics[2].metric("Resolved", summary["total"] - summary["open"])
    metrics[3].metric("Unassigned", summary["unassigned"])
    metrics[4].metric("Snapshots", len(snapshots))
    st.markdown("#### Workload by team and owner")
    workload = workload_report(latest_queue)
    if workload:
        st.dataframe(workload, hide_index=True, use_container_width=True)
    else:
        st.info("The latest snapshot has no workload rows.")
    st.markdown("#### Snapshot trend")
    trend = trend_report(snapshots)
    st.dataframe(trend, hide_index=True, use_container_width=True)
    st.line_chart(trend, x="as_of", y=["open", "breached", "at_risk"], height=240)


def _render_history(store: SQLiteStore, current_snapshot_id: int, current_queue: list[dict[str, object]]) -> None:
    st.subheader("History and change review")
    snapshots = store.list_snapshots()
    if len(snapshots) < 2:
        st.info("Import or run the same source at a different as-of/policy to see what changed.")
    else:
        choices = {f"#{row['id']} · {row['source_name']} · {row['as_of']}": int(row["id"]) for row in snapshots if int(row["id"]) != current_snapshot_id}
        selected_label = st.selectbox("Compare current snapshot with", list(choices))
        previous_queue = store.snapshot_queue(choices[selected_label])
        comparison = compare_queues(previous_queue, current_queue)
        counts = comparison["counts"]
        cols = st.columns(4)
        cols[0].metric("Added", counts["added"])
        cols[1].metric("Removed", counts["removed"])
        cols[2].metric("Changed", counts["changed"])
        cols[3].metric("Unchanged", counts["unchanged"])
        if comparison["changed"]:
            st.dataframe(comparison["changed"], hide_index=True, use_container_width=True)
        else:
            st.success("No field-level changes between these snapshots.")
    st.markdown("#### Decision audit")
    audit = store.decision_history()
    if audit:
        st.dataframe(audit, hide_index=True, use_container_width=True)
    else:
        st.info("No decisions recorded yet. Use the Triage desk to create the first audit event.")


def _render_admin(store: SQLiteStore, raw_table, mapping: dict[str, str | None], validation) -> None:
    st.subheader("Imports, mappings, and policies")
    st.write(f"Detected `{raw_table.format.upper()}` with {len(raw_table.headers)} columns and {len(raw_table.rows)} data rows.")
    st.code(_format_mapping(mapping))
    if validation.rejected:
        st.download_button("Export rejected-row report", rejected_csv_bytes(validation.rejected), "rejected-rows.csv", "text/csv")
    st.markdown("#### Import history")
    imports = store.list_imports()
    st.dataframe(imports, hide_index=True, use_container_width=True) if imports else st.info("No imports persisted yet.")
    st.markdown("#### Policy versions")
    policies = [{"id": row["id"], "name": row["name"], "version": row["version"], "created_at": row["created_at"], "policy": row["policy"].to_dict()} for row in store.list_policies()]
    st.dataframe(policies, hide_index=True, use_container_width=True) if policies else st.info("No policies persisted yet.")
    st.markdown("#### Saved views")
    views = store.list_views()
    st.dataframe(views, hide_index=True, use_container_width=True) if views else st.info("No saved views yet.")


st.set_page_config(page_title="Operations Queue Triage", page_icon="🧭", layout="wide")
st.title("Operations Queue Triage")
st.caption("From raw export to recorded decision, with a reproducible history of what changed.")

store = SQLiteStore(DB_PATH)
store.seed_policy(DEFAULT_POLICY)

with st.sidebar:
    st.header("1 · Import and map")
    uploaded = st.file_uploader("Upload CSV, TSV, or XLSX", type=["csv", "tsv", "xlsx", "xlsm"])
    source_bytes = uploaded.getvalue() if uploaded else FIXTURE_PATH.read_bytes()
    source_name = uploaded.name if uploaded else "synthetic demo fixture"
    st.caption(f"Source: {source_name}")

try:
    raw_table = read_table(source_bytes, source_name)
except IngestionError as exc:
    st.error(str(exc))
    st.info("Use a UTF-8 CSV/TSV or an XLSX workbook with a single header row.")
    st.stop()

suggested = suggest_mapping(raw_table.headers)
mapping_key = f"mapping::{source_name}::{len(source_bytes)}"
if st.session_state.get("mapping_key") != mapping_key:
    st.session_state["mapping_key"] = mapping_key
    st.session_state["mapping"] = suggested
mapping = _mapping_controls(raw_table, dict(st.session_state.get("mapping", suggested)))
st.session_state["mapping"] = mapping
required_unmapped = [column for column in ("ticket_id", "created_at", "status", "assignee", "team", "subject", "priority") if mapping.get(column) is None]
if required_unmapped:
    st.error(f"Map required fields before importing: {', '.join(required_unmapped)}")
    st.stop()

validation = validate_table(raw_table, mapping)
if validation.rejected:
    st.warning(f"{len(validation.rejected)} row(s) rejected; valid rows remain available for triage.")
    with st.expander("Review rejected rows", expanded=False):
        st.dataframe([{"row": row.row_number, "ticket_id": row.ticket_id or "—", "reason": row.reason} for row in validation.rejected], hide_index=True, use_container_width=True)
if not validation.valid:
    st.error("No valid tickets were found. Fix the mapping or source rows, then retry the import.")
    st.stop()

with st.sidebar:
    st.header("2 · Version the policy")
    saved_policy_rows = store.list_policies()
    saved_labels = {f"{row['name']} v{row['version']}": row["policy"] for row in saved_policy_rows}
    selected_policy_label = st.selectbox("Saved policy", list(saved_labels), index=0)
    policy = saved_labels[selected_policy_label]
    custom_policy = _policy_from_form(sorted({ticket.team for ticket in validation.valid}))
    if custom_policy:
        policy = custom_policy
        store.seed_policy(policy)
        st.success(f"Using {policy.name} v{policy.version} for this run.")
    default_as_of = _default_as_of(validation.valid)
    as_of_date = st.date_input("As-of date (UTC)", value=default_as_of.date())
    as_of_time = st.time_input("As-of time (UTC)", value=default_as_of.time())
    as_of = datetime.combine(as_of_date, as_of_time, tzinfo=UTC)

st.caption(
    f"Policy: {policy.name} v{policy.version} · default windows "
    + " / ".join(f"{priority} {policy.default_hours[priority]}h" for priority in REQUIRED_POLICY_PRIORITIES)
    + ". Naive timestamps are UTC; closed tickets never breach."
)

import_record = store.import_tickets(source_name, source_bytes, mapping, validation.valid, validation.rejected)
queue = prioritize_queue(apply_sla_policy(validation.valid, as_of, policy))
snapshot_key = policy_fingerprint(policy, as_of, source_bytes)
snapshot_id, snapshot_replayed = store.create_snapshot(import_record.id, policy, as_of, queue, snapshot_key)
st.caption(f"Import #{import_record.id} · snapshot #{snapshot_id} · {'replayed' if snapshot_replayed else 'new'} · key `{snapshot_key}`")

tabs = st.tabs(["Triage desk", "Reports", "History", "Imports & policies"])
with tabs[0]:
    _render_triage(store, snapshot_id, queue, source_bytes, policy, as_of)
with tabs[1]:
    _render_reports(store)
with tabs[2]:
    _render_history(store, snapshot_id, queue)
with tabs[3]:
    _render_admin(store, raw_table, mapping, validation)
