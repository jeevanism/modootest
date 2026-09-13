"""Unit tests for deterministic serializers and data models."""

from __future__ import annotations

import json

from modootest.agent.models import ExecutionResult, ExecutionStatus
from modootest.agent.serializers import (
    serialize_diagnostic,
    serialize_execution,
    serialize_plan,
    serialize_result_envelope,
    serialize_selection,
    serialize_watch_event,
    to_json_str,
    to_ndjson_line,
)
from modootest.intelligence.classifier import ChangeCategory, FileClassification
from modootest.intelligence.git import GitChangeRecord
from modootest.intelligence.ownership import OwnedChangeRecord
from modootest.intelligence.planner import Diagnostic, DiagnosticSeverity, PlanResult
from modootest.intelligence.selector import (
    ExcludedTarget,
    SelectedTest,
    SelectionMode,
    TestSelectionResult,
)


def _make_sample_plan() -> PlanResult:
    rec1 = GitChangeRecord(
        status="M",
        old_path=None,
        new_path="addons/crm/models/lead.py",
        old_mode="100644",
        new_mode="100644",
        old_sha="000",
        new_sha="111",
        status_field="M",
    )
    owned1 = OwnedChangeRecord(record=rec1, old_owner="crm", new_owner="crm")
    fc1 = FileClassification(
        owned_record=owned1,
        category=ChangeCategory.MODEL_CLASS,
        reason="Python model modification",
        module_update=True,
        fresh_process=False,
    )

    rec2 = GitChangeRecord(
        status="A",
        old_path=None,
        new_path="addons/sale/data/data.xml",
        old_mode="000000",
        new_mode="100644",
        old_sha="000",
        new_sha="222",
        status_field="A",
    )
    owned2 = OwnedChangeRecord(record=rec2, old_owner=None, new_owner="sale")
    fc2 = FileClassification(
        owned_record=owned2,
        category=ChangeCategory.XML_DATA,
        reason="Data file addition",
        module_update=True,
        fresh_process=False,
    )

    diag1 = Diagnostic(
        severity=DiagnosticSeverity.WARNING,
        message="Plan uncertain",
        code="uncertain_change",
        category="planning",
        remediation="Verify change manually",
        context={"file": "addons/crm/models/lead.py", "auth_token": "super_secret_token"},
    )
    diag2 = Diagnostic(
        severity=DiagnosticSeverity.INFO,
        message="Module update required",
        code="module_update_required",
        category="lifecycle",
        remediation="Update addon",
    )

    return PlanResult(
        git_root="/repo",
        comparison_mode="working-tree",
        base_commit="commit123",
        head_commit=None,
        addons_paths=("addons", "custom_addons"),
        is_complete=False,
        fresh_process_required=False,
        module_update_targets=("sale", "crm"),
        changed_addons=("sale", "crm"),
        deleted_addons=(),
        downstream_impact_addons=("sale_crm",),
        file_classifications=(fc2, fc1),  # Unsorted order
        diagnostics=(diag1, diag2),
    )


def test_diagnostic_sanitization_and_context_redaction() -> None:
    diag = Diagnostic(
        severity=DiagnosticSeverity.ERROR,
        message="Failed with <ValueError: bad data> in auth",
        code="PARSE_ERROR",  # uppercase to verify normalization
        category="PARSER",
        remediation="Check credentials",
        context={
            "user_password": "super_secret_password",
            "api_key": "12345-secret",
            "exception": "<CustomError: connection refused>",
            "safe_value": 42,
            "items": ["token=abc", "normal_string"],
        },
    )
    serialized = serialize_diagnostic(diag)
    assert serialized["code"] == "parse_error"
    assert serialized["category"] == "parser"
    assert "<ValueError: bad data>" not in serialized["message"]
    assert "[Exception]" in serialized["message"]

    ctx = serialized["context"]
    assert ctx["user_password"] == "[REDACTED]"
    assert ctx["api_key"] == "[REDACTED]"
    assert "[Exception]" in ctx["exception"]
    assert ctx["safe_value"] == 42


def test_diagnostic_context_is_bounded() -> None:
    nested: dict[str, object] = {"value": "kept"}
    for _ in range(10):
        nested = {"child": nested}
    diag = Diagnostic(
        severity=DiagnosticSeverity.INFO,
        message="bounded context",
        context={
            **{f"key_{index:03d}": index for index in range(300)},
            "_nested": nested,
            "_values": list(range(300)),
        },
    )

    context = serialize_diagnostic(diag)["context"]
    assert context["_truncated"] is True
    assert len(context) == 257
    assert context["_nested"]["child"]["child"]["child"]["child"]["child"]["child"] == "[TRUNCATED]"
    assert context["_values"][-1] == "[TRUNCATED]"


def test_deterministic_sorting_of_plan_and_selection() -> None:
    plan = _make_sample_plan()
    serialized_plan = serialize_plan(plan)

    # Collections must be canonically sorted
    assert serialized_plan["module_update_targets"] == ["crm", "sale"]
    assert serialized_plan["changed_addons"] == ["crm", "sale"]
    assert serialized_plan["addons_paths"] == ["addons", "custom_addons"]

    # File classifications sorted by path
    paths = [fc["path"] for fc in serialized_plan["file_classifications"]]
    assert paths == sorted(paths)

    # Diagnostics sorted by severity, code, message
    diags = serialized_plan["diagnostics"]
    keys = [(d["severity"], d["code"], d["message"]) for d in diags]
    assert keys == sorted(keys)

    selection = TestSelectionResult(
        mode=SelectionMode.DIRECT,
        is_complete=False,
        selected_tests=(
            SelectedTest(path="addons/sale/tests/test_b.py", addon="sale", reason="Changed"),
            SelectedTest(path="addons/crm/tests/test_a.py", addon="crm", reason="Changed"),
        ),
        excluded_targets=(
            ExcludedTarget(addon="sale", reason="No test directory"),
            ExcludedTarget(addon="crm", reason="No tests found"),
        ),
        diagnostics=plan.diagnostics,
        plan=plan,
        broadened_to_all_tests=False,
        explanation="Direct mode",
    )

    serialized_sel = serialize_selection(selection)
    sel_paths = [st["path"] for st in serialized_sel["selected_tests"]]
    assert sel_paths == ["addons/crm/tests/test_a.py", "addons/sale/tests/test_b.py"]
    assert serialized_sel["test_paths"] == sorted(selection.test_paths)

    exc_addons = [et["addon"] for et in serialized_sel["excluded_targets"]]
    assert exc_addons == ["crm", "sale"]


def test_envelope_and_execution_serialization() -> None:
    exec_res = ExecutionResult(
        status=ExecutionStatus.EXECUTION_PASSED,
        returncode=0,
        executed=True,
        collect_only=False,
        test_count=5,
        log_file="logs/test.log",
        summary="All tests passed",
    )
    serialized_exec = serialize_execution(exec_res)
    assert serialized_exec["status"] == "execution_passed"
    assert serialized_exec["returncode"] == 0
    assert serialized_exec["executed"] is True
    assert serialized_exec["test_count"] == 5

    envelope = serialize_result_envelope(
        command="impacted",
        status="complete",
        exit_code=0,
        is_complete=True,
        diagnostics=[],
        payload={"sample": "data"},
        execution=exec_res,
    )
    assert envelope["$schema"] == "https://modootest.dev/schemas/v1/result.json"
    assert envelope["contract_version"] == "1.0"
    assert envelope["command"] == "impacted"
    assert envelope["status"] == "complete"
    assert envelope["exit_code"] == 0
    assert envelope["is_complete"] is True
    assert envelope["payload"] == {"sample": "data"}
    assert envelope["execution"]["status"] == "execution_passed"

    json_str = to_json_str(envelope)
    parsed = json.loads(json_str)
    assert parsed == envelope


def test_watch_event_serialization() -> None:
    event = serialize_watch_event(
        event="change_detected",
        sequence=42,
        data={"changed_files": ["a.py", "b.py"], "debounce": 0.3},
    )
    assert event["$schema"] == "https://modootest.dev/schemas/v1/watch_event.json"
    assert event["contract_version"] == "1.0"
    assert event["sequence"] == 42
    assert event["event"] == "change_detected"

    ndjson = to_ndjson_line(event)
    assert "\n" not in ndjson
    parsed = json.loads(ndjson)
    assert parsed == event


def test_sensitive_string_redaction_variants() -> None:
    """Verify attached and separate sensitive option flags and URIs are cleanly redacted."""
    from modootest.agent.serializers import _sanitize_string

    assert _sanitize_string("--db_password=mysecret") == "--db_password=[REDACTED]"
    assert _sanitize_string("--db_password mysecret") == "--db_password [REDACTED]"
    assert _sanitize_string("-w supersecret") == "-w [REDACTED]"
    assert _sanitize_string("--pg_password=foo123") == "--pg_password=[REDACTED]"
    assert (
        _sanitize_string("postgresql://odoo:secret_pass@127.0.0.1:5432/db")
        == "postgresql://odoo:[REDACTED]@127.0.0.1:5432/db"
    )


def test_schema_field_presence_in_all_serializers() -> None:
    """Verify $schema is included in plan and execution, and contract_version in envelope/events."""
    plan = _make_sample_plan()
    sel_plan = serialize_plan(plan)
    assert sel_plan["$schema"] == "https://modootest.dev/schemas/v1/plan.json"

    exec_res = ExecutionResult(
        status=ExecutionStatus.UNKNOWN_SUBPROCESS_FAILURE,
        returncode=99,
        executed=True,
        collect_only=False,
        test_count=1,
        log_file="logs/test.log",
        summary="Unknown failure",
    )
    sel_exec = serialize_execution(exec_res)
    assert sel_exec["$schema"] == "https://modootest.dev/schemas/v1/execution.json"
    assert sel_exec["status"] == "unknown_subprocess_failure"
