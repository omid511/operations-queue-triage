# Operations Queue Triage

Operations Queue Triage is a local-first operations decision system: import a
ticket export, map and validate its columns, create a reproducible SLA
snapshot, triage the queue, record an auditable decision, and inspect what
changed over time.

The repository contains synthetic data only. It is a demo and must not be used
to upload private customer, employee, or incident data to a public app.

## Run locally

Python 3.11 is the tested runtime. From this directory:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
streamlit run app.py
```

The app starts with the synthetic fixture when no file is uploaded. Local
SQLite persistence is created at `data/triage.sqlite3` and is ignored by Git.
Delete that file only when you intentionally want to reset the demo history.

## End-to-end workflow

1. **Import and map:** upload CSV, TSV, XLSX, or XLSM. Common aliases such as
   `Owner`, `Group`, `Title`, `Severity`, and `Opened At` are suggested. Any
   unmapped required field blocks the import until it is corrected.
2. **Validate:** malformed timestamps, unknown status/priority, duplicates,
   missing fields, and invalid update order are rejected with row numbers and
   downloadable feedback. The valid rows remain usable.
3. **Version policy:** choose a saved policy or create a new version with
   severity windows, at-risk thresholds, and optional team-specific SLA hours.
4. **Create snapshot:** the import hash, mapping, policy version, and UTC
   as-of timestamp create an idempotent SQLite snapshot. Replaying the same
   source/policy/as-of does not duplicate history.
5. **Triage and review:** filter by SLA state, team, owner, or search; inspect
   the deterministic score and reason; export the current queue; then record
   `keep`, `escalate`, `snooze`, or `resolve` with a required audit note.
6. **Report:** use Reports for breached, aging, resolved, unassigned, workload,
   and snapshot trend metrics. History compares added/removed/changed tickets
   and shows the decision audit trail. Saved views preserve useful filters.

## CSV/XLSX contract

Required domain fields are `ticket_id`, `created_at`, `status`, `assignee`,
`team`, `subject`, and `priority`. `last_updated_at` is optional. Source
headers can use the suggested aliases shown in the mapping panel. CSV/TSV
files must be UTF-8; XLSX uses the active worksheet's first row as headers.

`created_at` and `last_updated_at` accept ISO-8601 values. A timestamp without
an offset is interpreted as UTC. Status values are `open`, `pending`,
`in_progress`, `resolved`, `closed`, or `cancelled`; priority values are
`urgent`, `high`, `normal`, or `low`. Blank assignees are shown as
`Unassigned`.

## SLA and scoring policy

The as-of timestamp is the point-in-time used for every calculation. The
standard policy is urgent 4h, high 8h, normal 24h, and low 72h. A policy
version can override those windows by team. Closed/resolved/cancelled tickets
never breach. Open tickets are `At risk` inside the final configured fraction
of the window, with a minimum warning window; they are `Breached` at or after
due time.

Queue score is deterministic: SLA state adds 100/60/20/0, priority adds
40/30/20/10, unassigned adds 15, and age adds one point per 12 hours up to 20.
Ties break by due time, then ticket ID. Every row stores `why_priority`, and
the policy name/version is stored with the snapshot.

## Persistence model

`src/queue_triage/storage.py` keeps imports, source hashes, mappings, valid and
rejected rows, policy versions, snapshots, snapshot queue rows, decision events,
and saved views in SQLite. It uses unique source-hash/mapping and
import/policy/as-of keys for replay safety. SQLite is intentionally local and
ephemeral on Streamlit Community Cloud; Community Cloud is suitable for the
synthetic demo, not durable shared operations.

For a durable multi-user deployment, keep the pure domain and UI workflow but
replace the repository seam with a managed Postgres database (or a hosted
SQLite-compatible service with backups), add authentication/authorization,
and move uploaded files to private object storage. Those changes are outside
this demo's trust boundary.

## Tests and CI

```bash
pytest -q
python -m compileall -q app.py src tests scripts
PYTHONPATH=src python scripts/typecheck.py
ruff check app.py src tests scripts
python scripts/smoke_check.py
```

GitHub Actions installs pinned Python 3.11 dependencies, compiles the project,
runs the public API type contract, Ruff, tests, the fixture + SQLite replay
smoke check, builds a wheel, and starts Streamlit to verify `/_stcore/health`.
The workflow is `.github/workflows/ci.yml`.

## Streamlit Community Cloud

1. Push this project to a GitHub repository.
2. In Streamlit Community Cloud, choose **New app**, select the repository and
   branch, and use `app.py` as the main file.
3. Select Python 3.11 if the deployment UI offers a runtime choice; the repo
   also includes `.python-version` and `runtime.txt`.
4. Deploy. Dependencies are pinned in `requirements.txt`.

No secrets are required. Do not add ticket exports, API keys, or
`.streamlit/secrets.toml` to the repository. Keep the synthetic fixture and
demo-only privacy boundary if the app is public.

## Scope and next risks

This version intentionally has no live helpdesk integration, authentication,
notifications, multi-user conflict handling, or real customer data. Before
using it operationally, validate timezone conventions with the source system,
add authentication and durable shared storage, and review the policy/version
change process with the operations owner.
