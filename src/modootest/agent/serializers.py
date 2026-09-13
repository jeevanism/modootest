"""Deterministic JSON serializers for modootest agent interface."""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from modootest.agent.models import ExecutionResult
from modootest.intelligence.classifier import FileClassification
from modootest.intelligence.planner import Diagnostic, PlanResult
from modootest.intelligence.selector import TestSelectionResult

_SENSITIVE_KEY_RE = re.compile(r"(password|secret|token|credential|api[_-]?key|auth)", re.IGNORECASE)
_SENSITIVE_OPTION_ATTACHED_RE = re.compile(
    r"(?<!\S)(--[A-Za-z0-9_.-]*(?:password|secret|token|credential|api[_-]?key|auth)[A-Za-z0-9_.-]*=)(?:'[^']*'|\"[^\"]*\"|\S+)",
    re.IGNORECASE,
)
_SENSITIVE_OPTION_SEPARATE_RE = re.compile(
    r"(?<!\S)(--[A-Za-z0-9_.-]*(?:password|secret|token|credential|api[_-]?key|auth)[A-Za-z0-9_.-]*\s+|-w\s+)(?:'[^']*'|\"[^\"]*\"|\S+)",
    re.IGNORECASE,
)
_SENSITIVE_URI_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:\s/@]+:)([^@\s/]+)(@)")
_EXCEPTION_REPR_RE = re.compile(r"<[A-Za-z0-9_.]*Exception[^>]*>|<[A-Za-z0-9_.]*Error[^>]*>")
_MAX_CONTEXT_DEPTH = 6
_MAX_CONTEXT_ITEMS = 256


def _sanitize_string(s: str) -> str:
    """Sanitize strings to prevent leaking exception reprs or credentials."""
    if _EXCEPTION_REPR_RE.search(s):
        s = _EXCEPTION_REPR_RE.sub("[Exception]", s)
    if _SENSITIVE_OPTION_ATTACHED_RE.search(s):
        s = _SENSITIVE_OPTION_ATTACHED_RE.sub(r"\1[REDACTED]", s)
    if _SENSITIVE_OPTION_SEPARATE_RE.search(s):
        s = _SENSITIVE_OPTION_SEPARATE_RE.sub(r"\1[REDACTED]", s)
    if _SENSITIVE_URI_RE.search(s):
        s = _SENSITIVE_URI_RE.sub(r"\1[REDACTED]\3", s)
    # Bounded length per string
    if len(s) > 1000:
        s = s[:997] + "..."
    return s



def _sanitize_value(value: Any, depth: int) -> Any:
    """Convert an arbitrary context value to bounded JSON-compatible data."""
    if depth >= _MAX_CONTEXT_DEPTH:
        return "[TRUNCATED]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return _sanitize_context(value, depth=depth + 1)
    if isinstance(value, (list, tuple)):
        items = [
            _sanitize_value(item, depth + 1)
            for item in value[:_MAX_CONTEXT_ITEMS]
        ]
        if len(value) > _MAX_CONTEXT_ITEMS:
            items.append("[TRUNCATED]")
        return items
    return _sanitize_string(str(value))


def _sanitize_context(context: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Sanitize structured context dictionary with bounded size and key sorting."""
    sanitized: dict[str, Any] = {}
    keys = sorted(context.keys(), key=str)
    for k in keys[:_MAX_CONTEXT_ITEMS]:
        k_str = k if isinstance(k, str) else str(k)
        if _SENSITIVE_KEY_RE.search(k_str):
            sanitized[k_str] = "[REDACTED]"
            continue
        sanitized[k_str] = _sanitize_value(context[k], depth)
    if len(keys) > _MAX_CONTEXT_ITEMS:
        sanitized["_truncated"] = True
    return sanitized



def serialize_diagnostic(diag: Diagnostic) -> dict[str, Any]:
    """Serialize a Diagnostic instance to a JSON-compatible dict."""
    category = diag.category.lower() if diag.category else "general"
    code = diag.code.lower() if diag.code else "diagnostic"
    msg = _sanitize_string(diag.message)

    out: dict[str, Any] = {
        "category": category,
        "code": code,
        "context": _sanitize_context(diag.context) if diag.context is not None else None,
        "message": msg,
        "remediation": _sanitize_string(diag.remediation) if diag.remediation is not None else None,
        "severity": diag.severity.value,
    }
    return out


def serialize_file_classification(fc: FileClassification) -> dict[str, Any]:
    """Serialize a FileClassification instance."""
    rec = fc.owned_record.record
    return {
        "affected_addons": sorted(fc.owned_record.affected_addons),
        "category": fc.category.value,
        "fresh_process": bool(fc.fresh_process),
        "is_uncertain": bool(fc.is_uncertain),
        "module_update": fc.module_update,
        "new_owner": fc.owned_record.new_owner,
        "old_owner": fc.owned_record.old_owner,
        "old_path": rec.old_path,
        "path": rec.effective_path,
        "reason": fc.reason,
        "status": rec.status,
    }


def serialize_plan(plan: PlanResult) -> dict[str, Any]:
    """Serialize a PlanResult instance into a schema-valid dictionary."""
    fcs = sorted(
        [serialize_file_classification(fc) for fc in plan.file_classifications],
        key=lambda x: x["path"],
    )
    diags = sorted(
        [serialize_diagnostic(d) for d in plan.diagnostics],
        key=lambda d: (d["severity"], d["code"], d["message"]),
    )
    return {
        "$schema": "https://modootest.dev/schemas/v1/plan.json",
        "addons_paths": sorted(plan.addons_paths),
        "base_commit": plan.base_commit,
        "changed_addons": sorted(plan.changed_addons),
        "comparison_mode": plan.comparison_mode,
        "deleted_addons": sorted(plan.deleted_addons),
        "diagnostics": diags,
        "downstream_impact_addons": sorted(plan.downstream_impact_addons),
        "file_classifications": fcs,
        "fresh_process_required": bool(plan.fresh_process_required),
        "git_root": plan.git_root,
        "graph_snapshot_policy": plan.graph_snapshot_policy,
        "head_commit": plan.head_commit,
        "is_complete": bool(plan.is_complete),
        "module_update_targets": sorted(plan.module_update_targets),
    }


def serialize_selection(selection: TestSelectionResult) -> dict[str, Any]:
    """Serialize a TestSelectionResult instance into a schema-valid dictionary."""
    selected = sorted(
        [
            {"addon": st.addon, "path": st.path, "reason": st.reason}
            for st in selection.selected_tests
        ],
        key=lambda s: s["path"],
    )
    excluded = sorted(
        [
            {"addon": et.addon, "reason": et.reason}
            for et in selection.excluded_targets
        ],
        key=lambda e: e["addon"],
    )
    diags = sorted(
        [serialize_diagnostic(d) for d in selection.diagnostics],
        key=lambda d: (d["severity"], d["code"], d["message"]),
    )
    return {
        "$schema": "https://modootest.dev/schemas/v1/selection.json",
        "broadened_to_all_tests": bool(selection.broadened_to_all_tests),
        "diagnostics": diags,
        "excluded_targets": excluded,
        "explanation": selection.explanation,
        "is_complete": bool(selection.is_complete),
        "mode": selection.mode.value,
        "plan": serialize_plan(selection.plan),
        "selected_tests": selected,
        "test_paths": sorted(selection.test_paths),
    }


def serialize_execution(execution: ExecutionResult) -> dict[str, Any]:
    """Serialize an ExecutionResult instance into a schema-valid dictionary."""
    return {
        "$schema": "https://modootest.dev/schemas/v1/execution.json",
        "collect_only": bool(execution.collect_only),
        "executed": bool(execution.executed),
        "log_file": execution.log_file,
        "returncode": execution.returncode,
        "status": execution.status.value,
        "summary": execution.summary,
        "test_count": execution.test_count,
    }



def serialize_result_envelope(
    command: str,
    status: str,
    exit_code: int,
    is_complete: bool,
    diagnostics: Sequence[Diagnostic],
    payload: dict[str, Any] | None = None,
    execution: ExecutionResult | None = None,
) -> dict[str, Any]:
    """Build the root result envelope matching result.json schema."""
    diags = sorted(
        [serialize_diagnostic(d) for d in diagnostics],
        key=lambda d: (d["severity"], d["code"], d["message"]),
    )
    exec_data = serialize_execution(execution) if execution is not None else None
    return {
        "$schema": "https://modootest.dev/schemas/v1/result.json",
        "command": command,
        "contract_version": "1.0",
        "diagnostics": diags,
        "execution": exec_data,
        "exit_code": exit_code,
        "is_complete": bool(is_complete),
        "payload": payload,
        "status": status,
    }


serialize_envelope = serialize_result_envelope


def serialize_watch_event(
    event: str,
    sequence: int,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Serialize a streaming event matching watch_event.json schema."""
    clean_data = _sanitize_context(data)
    return {
        "$schema": "https://modootest.dev/schemas/v1/watch_event.json",
        "contract_version": "1.0",
        "data": clean_data,
        "event": event,
        "sequence": sequence,
    }


def to_json_str(doc: dict[str, Any]) -> str:
    """Format dictionary to deterministic formatted JSON string."""
    return json.dumps(doc, indent=2, sort_keys=True)


def to_ndjson_line(doc: dict[str, Any]) -> str:
    """Format dictionary to single-line deterministic JSON for streaming events."""
    return json.dumps(doc, sort_keys=True)
