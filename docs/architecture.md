# Architecture

```text
CSV / TSV / XLSX
      |
      v
  import + map ----> validation + rejection report
      |
      v
 versioned SLA policy + as-of time
      |
      v
 deterministic queue scoring
      |
      +--> triage decision audit
      +--> reports / history / exports
      |
      v
 local SQLite repository
```

The domain calculations are kept separate from Streamlit rendering and the
SQLite repository. An import hash, mapping, policy version, and as-of timestamp
form the replay key, so repeating an import does not silently create a second
snapshot. The UI is intentionally local-first: replacing the repository seam
with authenticated Postgres and private object storage is the production path.

## Deliberate trade-offs

- Deterministic rules are favored over an opaque model because operators need
  an explainable reason for every queue rank.
- SQLite keeps the public demo portable and inspectable, but is not shared or
  durable on ephemeral Streamlit hosting.
- Synthetic fixtures make CI and public evaluation safe; live helpdesk
  integrations are intentionally outside this repository's trust boundary.
