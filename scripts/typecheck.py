"""Lightweight public API/type contract check for the dependency-free core."""

from __future__ import annotations

import inspect
import typing

from queue_triage import apply_sla_policy, compare_queues, validate_table, workload_report

PUBLIC_FUNCTIONS = (apply_sla_policy, compare_queues, validate_table, workload_report)
for function in PUBLIC_FUNCTIONS:
    typing.get_type_hints(function)
    missing = [
        parameter.name
        for parameter in inspect.signature(function).parameters.values()
        if parameter.annotation is inspect.Parameter.empty
    ]
    assert not missing, f"{function.__name__} missing annotations: {missing}"
print(f"type contract ok: {len(PUBLIC_FUNCTIONS)} public functions")
