"""Unit tests for modootest agent CLI interface, --format human|json, and exit codes."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from modootest.cli.main import main
from modootest.intelligence.classifier import ChangeCategory, FileClassification
from modootest.intelligence.git import GitChangeRecord
from modootest.intelligence.ownership import OwnedChangeRecord
from modootest.intelligence.planner import Diagnostic, DiagnosticSeverity, PlanResult
from modootest.intelligence.selector import (
    SelectedTest,
    SelectionMode,
    TestSelectionResult,
)
from modootest.watch.supervisor import WatchSupervisor


def _mock_plan(is_complete: bool = True) -> PlanResult:
    rec = GitChangeRecord(
        status="M",
        old_path=None,
        new_path="addons/crm/models/lead.py",
        old_mode="100644",
        new_mode="100644",
        old_sha="000",
        new_sha="111",
        status_field="M",
    )
    owned = OwnedChangeRecord(record=rec, old_owner="crm", new_owner="crm")
    fc = FileClassification(
        owned_record=owned,
        category=ChangeCategory.MODEL_CLASS,
        reason="Python model modification",
        module_update=True,
        fresh_process=False,
    )
    return PlanResult(
        git_root="/repo",
        comparison_mode="working-tree",
        base_commit="abc1234",
        head_commit=None,
        addons_paths=("addons",),
        is_complete=is_complete,
        fresh_process_required=False,
        module_update_targets=("crm",),
        changed_addons=("crm",),
        deleted_addons=(),
        downstream_impact_addons=(),
        file_classifications=(fc,),
        diagnostics=(),
    )


def _mock_selection(is_complete: bool = True, selected_tests: tuple = ()) -> TestSelectionResult:
    plan = _mock_plan(is_complete=is_complete)
    return TestSelectionResult(
        mode=SelectionMode.DIRECT,
        is_complete=is_complete,
        selected_tests=selected_tests,
        excluded_targets=(),
        diagnostics=(),
        plan=plan,
        broadened_to_all_tests=False,
        explanation="Direct mode selection",
    )


def test_cli_plan_json_output_complete(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("modootest.cli.main.build_plan", return_value=_mock_plan(is_complete=True)):
        code = main(["plan", "--repo", "/repo", "--addons-path", "addons", "--working-tree", "--format", "json"])
        assert code == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        data = json.loads(captured.out)
        assert data["command"] == "plan"
        assert data["status"] == "complete"
        assert data["exit_code"] == 0
        assert data["is_complete"] is True
        assert data["payload"]["git_root"] == "/repo"


def test_cli_plan_json_output_incomplete(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("modootest.cli.main.build_plan", return_value=_mock_plan(is_complete=False)):
        code = main(["plan", "--repo", "/repo", "--addons-path", "addons", "--working-tree", "--format", "json"])
        assert code == 1
        captured = capsys.readouterr()
        assert captured.err == ""
        data = json.loads(captured.out)
        assert data["status"] == "incomplete"
        assert data["exit_code"] == 1
        assert data["is_complete"] is False


def test_cli_plan_human_output_compatible(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("modootest.cli.main.build_plan", return_value=_mock_plan(is_complete=True)):
        code = main(["plan", "--repo", "/repo", "--addons-path", "addons", "--working-tree", "--format", "human"])
        assert code == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert "modootest Change Intelligence Plan" in captured.out
        assert "Status: COMPLETE" in captured.out


def test_cli_argparse_malformed_input_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    # 1. Unrecognized argument
    code = main(["plan", "--format", "json", "--addons-path", "addons", "--working-tree", "--bogus-flag"])
    assert code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    data = json.loads(captured.out)
    assert data["status"] == "error"
    assert data["exit_code"] == 2
    assert any("unrecognized arguments" in d["message"].lower() for d in data["diagnostics"])

    # 2. Missing required --addons-path in plan
    code = main(["plan", "--format", "json", "--working-tree"])
    assert code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    data = json.loads(captured.out)
    assert data["exit_code"] == 2
    assert any("required" in d["message"].lower() for d in data["diagnostics"])

    # 3. No command specified with --format json
    code = main(["--format", "json"])
    assert code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    data = json.loads(captured.out)
    assert data["command"] == "modootest"
    assert data["status"] == "error"


def test_cli_test_without_impacted_flag_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["test", "--format", "json", "--repo", ".", "--addons-path", "addons", "--working-tree"])
    assert code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    data = json.loads(captured.out)
    assert data["command"] == "test"
    assert data["status"] == "error"
    assert any("requires --impacted" in d["message"].lower() for d in data["diagnostics"])


def test_cli_impacted_dry_run_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    st = SelectedTest(path="addons/crm/tests/test_a.py", addon="crm", reason="Direct")
    sel = _mock_selection(is_complete=True, selected_tests=(st,))
    with patch("modootest.cli.main.build_plan", return_value=sel.plan), patch(
        "modootest.cli.main.select_impacted_tests", return_value=sel
    ):
        code = main([
            "impacted",
            "--repo",
            "/repo",
            "--addons-path",
            "addons",
            "--working-tree",
            "--format",
            "json",
        ])
        assert code == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        data = json.loads(captured.out)
        assert data["command"] == "impacted"
        assert data["status"] == "complete"
        assert data["execution"]["status"] == "not_executed"
        assert data["execution"]["executed"] is False


def test_cli_impacted_no_tests_selected_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    sel = _mock_selection(is_complete=True, selected_tests=())
    with patch("modootest.cli.main.build_plan", return_value=sel.plan), patch(
        "modootest.cli.main.select_impacted_tests", return_value=sel
    ):
        code = main([
            "impacted",
            "--repo",
            "/repo",
            "--addons-path",
            "addons",
            "--working-tree",
            "--execute",
            "--format",
            "json",
        ])
        assert code == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        data = json.loads(captured.out)
        assert data["execution"]["status"] == "no_tests_selected"
        assert data["execution"]["executed"] is False


@pytest.mark.parametrize(
    ("returncode", "category", "collect_only", "expected_status", "expected_exit", "expected_executed", "expected_rc"),
    [
        (0, "success", False, "execution_passed", 0, True, 0),
        (0, "success", True, "collection_passed", 0, True, 0),
        (1, "tests failed", False, "tests_failed", 3, True, 1),
        (2, "interrupted", False, "interrupted", 3, True, 2),
        (3, "internal error", False, "pytest_internal_error", 3, True, 3),
        (4, "usage error", False, "pytest_usage_error", 3, True, 4),
        (5, "no tests collected", False, "no_tests_collected", 3, True, 5),
        (99, "unknown code", False, "unknown_subprocess_failure", 3, True, 99),
        (-1, "timeout", False, "timeout", 3, True, 124),
        (-1, "execution error: binary not found", False, "launch_error", 3, False, None),
    ],
)
def test_cli_all_subprocess_outcomes_json_mode(
    capsys: pytest.CaptureFixture[str],
    returncode: int,
    category: str,
    collect_only: bool,
    expected_status: str,
    expected_exit: int,
    expected_executed: bool,
    expected_rc: int | None,
) -> None:
    st = SelectedTest(path="addons/crm/tests/test_a.py", addon="crm", reason="Direct")
    sel = _mock_selection(is_complete=True, selected_tests=(st,))
    log_path = Path("/repo/logs/pytest.log")

    argv = [
        "impacted",
        "--repo",
        "/repo",
        "--addons-path",
        "addons",
        "--working-tree",
        "--format",
        "json",
    ]
    if collect_only:
        argv.append("--collect-only")
    else:
        argv.append("--execute")

    with patch("modootest.cli.main.build_plan", return_value=sel.plan), patch(
        "modootest.cli.main.select_impacted_tests", return_value=sel
    ), patch(
        "modootest.cli.main._run_pytest_subprocess",
        return_value=(returncode, category, log_path),
    ):
        code = main(argv)
        assert code == expected_exit
        captured = capsys.readouterr()
        assert captured.err == ""
        data = json.loads(captured.out)
        assert data["exit_code"] == expected_exit
        assert data["execution"]["status"] == expected_status
        assert data["execution"]["executed"] is expected_executed
        assert data["execution"]["returncode"] == expected_rc


def test_cli_global_format_json_flag_placement(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that --format json works identically when placed before or after subcommand."""
    with patch("modootest.cli.main.build_plan", return_value=_mock_plan(is_complete=True)):
        # Before subcommand: modootest --format json plan ...
        code1 = main(["--format", "json", "plan", "--repo", "/repo", "--addons-path", "addons", "--working-tree"])
        assert code1 == 0
        captured1 = capsys.readouterr()
        assert captured1.err == ""
        data1 = json.loads(captured1.out)
        assert data1["command"] == "plan"
        assert data1["status"] == "complete"

        # After subcommand: modootest plan --format json ...
        code2 = main(["plan", "--format", "json", "--repo", "/repo", "--addons-path", "addons", "--working-tree"])
        assert code2 == 0
        captured2 = capsys.readouterr()
        assert captured2.err == ""
        data2 = json.loads(captured2.out)
        assert data2["command"] == "plan"
        assert data2["status"] == "complete"
        assert data1 == data2


def test_cli_sensitive_arg_redaction_in_diagnostics(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that sensitive option values in rejected pytest arguments are redacted in diagnostics."""
    st = SelectedTest(path="addons/crm/tests/test_a.py", addon="crm", reason="Direct")
    sel = _mock_selection(is_complete=True, selected_tests=(st,))
    with patch("modootest.cli.main.build_plan", return_value=sel.plan), patch(
        "modootest.cli.main.select_impacted_tests", return_value=sel
    ):
        # Test attached sensitive arg
        code = main([
            "impacted",
            "--repo",
            "/repo",
            "--addons-path",
            "addons",
            "--working-tree",
            "--pytest-arg",
            "--db_password=SUPER_SECRET_VALUE",
            "--format",
            "json",
        ])
        assert code == 2
        captured = capsys.readouterr()
        assert captured.err == ""
        data = json.loads(captured.out)
        assert data["status"] == "error"
        # Secret value must NEVER appear anywhere in the output!
        assert "SUPER_SECRET_VALUE" not in captured.out
        diag_msg = data["diagnostics"][0]["message"]
        assert "--db_password=[REDACTED]" in diag_msg

        # Test separate sensitive arg
        code2 = main([
            "impacted",
            "--repo",
            "/repo",
            "--addons-path",
            "addons",
            "--working-tree",
            "--pytest-arg",
            "--db_password",
            "--pytest-arg",
            "ANOTHER_SECRET_VAL",
            "--format",
            "json",
        ])
        assert code2 == 2
        captured2 = capsys.readouterr()
        assert "ANOTHER_SECRET_VAL" not in captured2.out


def test_cli_unsafe_pytest_arg_rejected_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    st = SelectedTest(path="addons/crm/tests/test_a.py", addon="crm", reason="Direct")
    sel = _mock_selection(is_complete=True, selected_tests=(st,))
    with patch("modootest.cli.main.build_plan", return_value=sel.plan), patch(
        "modootest.cli.main.select_impacted_tests", return_value=sel
    ):
        code = main([
            "impacted",
            "--repo",
            "/repo",
            "--addons-path",
            "addons",
            "--working-tree",
            "--pytest-arg",
            "-d=testdb",
            "--format",
            "json",
        ])
        assert code == 2
        captured = capsys.readouterr()
        assert captured.err == ""
        data = json.loads(captured.out)
        assert data["status"] == "error"
        assert data["exit_code"] == 2
        assert any("database/server mutation" in d["message"].lower() for d in data["diagnostics"])


def test_cli_watch_mode_string_normalized_through_seam(tmp_path: Path) -> None:
    """Verify that CLI string modes are normalized to SelectionMode enum instances."""
    git_root = tmp_path / "repo"
    git_root.mkdir()
    (git_root / "addons").mkdir()
    modootest_config = tmp_path / "modootest.conf"
    modootest_config.write_text("[options]\n")

    captured_supervisors: list[WatchSupervisor] = []

    class MockSupervisor:
        def __init__(self, **kwargs: object) -> None:
            self.mode = kwargs.get("mode")
            self.supervisor = WatchSupervisor(**kwargs)
            captured_supervisors.append(self.supervisor)

        def run(self) -> int:
            return 0

    with patch("modootest.cli.main.discover_git_root", return_value=git_root), patch(
        "modootest.cli.main.validate_watch_roots", return_value=[git_root / "addons"]
    ), patch("modootest.cli.main.WatchSupervisor", MockSupervisor):
        for mode_str, expected_enum in [
            ("direct", SelectionMode.DIRECT),
            ("dependent", SelectionMode.DEPENDENT),
            ("safe", SelectionMode.SAFE),
        ]:
            code = main([
                "watch",
                "--repo",
                str(git_root),
                "--addons-path",
                "addons",
                "--mode",
                mode_str,
                "--modootest-config",
                str(modootest_config),
            ])
            assert code == 0
            sup = captured_supervisors[-1]
            assert isinstance(sup.mode, SelectionMode)
            assert sup.mode == expected_enum
            assert sup.mode.value == mode_str
            assert getattr(sup, "modootest_config", None) == modootest_config.resolve()


def test_cli_watch_invalid_pytest_args_json_and_human(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that invalid watch pytest args exit 2 before any runner/snapshot with clean outputs."""
    git_root = tmp_path / "repo"
    git_root.mkdir()
    (git_root / "addons").mkdir()

    with patch("modootest.cli.main.discover_git_root", return_value=git_root), patch(
        "modootest.cli.main.validate_watch_roots", return_value=[git_root / "addons"]
    ):
        # 1. JSON mode: exit 2, stdout error envelope, empty stderr
        code_json = main([
            "watch",
            "--repo",
            str(git_root),
            "--addons-path",
            "addons",
            "--pytest-arg",
            "--database=injected",
            "--format",
            "json",
        ])
        assert code_json == 2
        captured_json = capsys.readouterr()
        assert captured_json.err == ""
        data = json.loads(captured_json.out)
        assert data["command"] == "watch"
        assert data["status"] == "error"
        assert data["exit_code"] == 2
        assert any("database/server mutation" in d["message"].lower() for d in data["diagnostics"])

        # 2. Human mode: exit 2, stderr error message
        code_human = main([
            "watch",
            "--repo",
            str(git_root),
            "--addons-path",
            "addons",
            "--pytest-arg",
            "--database=injected",
        ])
        assert code_human == 2
        captured_human = capsys.readouterr()
        assert "database/server mutation" in captured_human.err
        assert captured_human.err.startswith("Error:")
