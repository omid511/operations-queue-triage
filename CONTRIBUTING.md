# Contributing

This is a portfolio-quality demo with synthetic data. Keep changes focused on
the operations decision workflow and preserve the public privacy boundary.

## Before opening a pull request

```bash
python -m pip install -r requirements-dev.txt
python -m compileall -q app.py src tests scripts
PYTHONPATH=src python scripts/typecheck.py
ruff check app.py src tests scripts
pytest -q
PYTHONPATH=src python scripts/smoke_check.py
```

Use a short imperative commit subject, explain non-obvious trade-offs, and
include screenshots or fixture details when changing the Streamlit workflow.
Never commit real ticket exports, secrets, or `.streamlit/secrets.toml`.
