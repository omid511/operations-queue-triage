# Changelog

## Unreleased

- Continue hardening the reproducible import, SLA snapshot, triage, and audit workflow.

## 0.2.0 - 2026-08-09

- Reconciled the canonical package/release version with `pyproject.toml` and
  the release workflow: tag `v0.2.0` builds the `0.2.0` wheel.
- Kept the existing `v0.1.0` release intact; it remains historical despite
  carrying the earlier mismatched artifact.

## 0.1.0 - 2026-08-09

- Published the end-to-end synthetic operations queue demo.
- Added replay-safe SQLite persistence, versioned SLA policies, triage decisions,
  history comparison, reports, saved views, exports, tests, CI, CodeQL, and a
  release artifact workflow.
