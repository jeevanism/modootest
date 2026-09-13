"""Command-line interface for modootest."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Sequence

from modootest.agent.models import ExecutionResult, ExecutionStatus, map_subprocess_returncode
from modootest.agent.serializers import (
    serialize_plan,
    serialize_result_envelope,
    serialize_selection,
    to_json_str,
)
from modootest.intelligence.git import GitError, discover_git_root
from modootest.intelligence.manifest import ManifestError
from modootest.intelligence.planner import (
    Diagnostic,
    DiagnosticSeverity,
    build_plan,
)
from modootest.intelligence.selector import (
    SelectionMode,
    SelectorError,
    select_impacted_tests,
)
from modootest.watch.snapshot import WatchSnapshotError, validate_watch_roots
from modootest.watch.supervisor import WatchSupervisor


from modootest.agent.pytest_args import (
    ALLOWED_CAPTURE_OPTIONS,
    ALLOWED_COLOR_OPTIONS,
    ALLOWED_IMPORT_MODES,
    ALLOWED_STANDALONE_FLAGS,
    ALLOWED_TB_STYLES,
    ALLOWED_VALUE_OPTIONS,
    FORBIDDEN_EXACT_ARGS,
    FORBIDDEN_PREFIX_ARGS,
    _redact_pytest_arg,
    _validate_pytest_args,
    redact_pytest_arg,
    validate_pytest_args,
)



def _pytest_returncode_category(returncode: int) -> str:
    """Return concise failure category string for pytest return code."""
    mapping = {
        0: "success",
        1: "tests failed",
        2: "interrupted",
        3: "internal error",
        4: "usage error",
        5: "no tests collected",
    }
    return mapping.get(returncode, f"exit code {returncode}")


def _run_pytest_subprocess(
    git_root: Path,
    test_paths: Sequence[str],
    pytest_args: Sequence[str],
    collect_only: bool,
    log_file_path: Path | None,
    modootest_config: Path | None = None,
    timeout: float = 120.0,
) -> tuple[int, str, Path]:
    """Execute pytest safely using argument vectors with redirected log output."""
    cmd = [sys.executable, "-m", "pytest"]
    if collect_only:
        cmd.append("--collect-only")
    if modootest_config is not None:
        cmd.append(f"--modootest-config={modootest_config}")
    cmd.extend(test_paths)
    cmd.extend(pytest_args)

    if log_file_path is not None:
        log_path = log_file_path
    else:
        default_log_dir = git_root / ".ai-handoff" / "0009-impacted-testing" / "logs"
        if default_log_dir.is_dir():
            log_path = default_log_dir / "pytest_impacted.log"
        else:
            tmp = tempfile.NamedTemporaryFile(
                delete=False, prefix="modootest_pytest_", suffix=".log"
            )
            tmp.close()
            log_path = Path(tmp.name)

    log_path.parent.mkdir(parents=True, exist_ok=True)

    sanitized_env = dict(os.environ)
    sanitized_env["PYTHONUNBUFFERED"] = "1"
    sanitized_env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(git_root),
            env=sanitized_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
        combined_output = (
            f"=== Pytest Command ===\n{' '.join(cmd)}\n\n"
            f"=== STDOUT ===\n{stdout_text}\n\n"
            f"=== STDERR ===\n{stderr_text}\n"
        )
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(combined_output)

        category = _pytest_returncode_category(proc.returncode)
        return proc.returncode, category, log_path
    except subprocess.TimeoutExpired as exc:
        stdout_text = (
            exc.stdout
            if isinstance(exc.stdout, str)
            else (exc.stdout.decode() if exc.stdout else "")
        )
        stderr_text = (
            exc.stderr
            if isinstance(exc.stderr, str)
            else (exc.stderr.decode() if exc.stderr else "")
        )
        combined_output = (
            f"=== Pytest Command (TIMED OUT after {timeout}s) ===\n{' '.join(cmd)}\n\n"
            f"=== STDOUT ===\n{stdout_text}\n\n"
            f"=== STDERR ===\n{stderr_text}\n"
        )
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(combined_output)
        return -1, "timeout", log_path
    except Exception as exc:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Failed to execute pytest: {exc}\n")
        return -1, f"execution error: {exc}", log_path


def _resolve_modootest_config_path(raw_path: str) -> Path:
    """Resolve a user-supplied config before the pytest subprocess changes cwd."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"modootest configuration file does not exist: {raw_path}") from None
    if not resolved.is_file():
        raise ValueError(f"modootest configuration path is not a file: {raw_path}")
    return resolved


class ArgparseJsonError(Exception):
    """Raised by ModootestArgumentParser when JSON format is requested and argument parsing fails."""

    def __init__(self, message: str, command: str = "modootest") -> None:
        super().__init__(message)
        self.message = message
        self.command = command


class ModootestArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that intercepts errors and emits JSON when --format json was requested."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._json_requested: bool = False
        self._active_subcommand: str = "modootest"

    def error(self, message: str) -> None:
        if self._json_requested:
            raise ArgparseJsonError(message, command=self._active_subcommand)
        super().error(message)


def _emit_json_error(
    command: str,
    message: str,
    code: str,
    category: str,
    remediation: str | None = None,
    exit_code: int = 2,
) -> int:
    """Format and emit a schema-valid JSON error document to stdout."""
    diag = Diagnostic(
        severity=DiagnosticSeverity.ERROR,
        message=message,
        code=code,
        category=category,
        remediation=remediation,
    )
    doc = serialize_result_envelope(
        command=command,
        status="error",
        exit_code=exit_code,
        is_complete=False,
        diagnostics=[diag],
        payload=None,
        execution=None,
    )
    sys.stdout.write(to_json_str(doc) + "\n")
    return exit_code


def _add_impacted_args(
    parser: argparse.ArgumentParser,
    require_impacted_flag: bool = False,
) -> None:
    """Add arguments for impacted test selection and execution."""
    if require_impacted_flag:
        parser.add_argument(
            "--impacted",
            action="store_true",
            help="Select and run impacted tests based on change intelligence plan.",
        )
    else:
        parser.add_argument(
            "--impacted",
            action="store_true",
            default=True,
            help="Impacted testing flag (enabled by default for 'modootest impacted').",
        )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to target Git repository (default: current directory).",
    )
    parser.add_argument(
        "--addons-path",
        action="append",
        dest="addons_paths",
        required=True,
        help="Explicit addon search root relative to Git root (can be specified multiple times).",
    )
    parser.add_argument(
        "--working-tree",
        action="store_true",
        help="Compare HEAD to current working tree (including staged, unstaged, and untracked changes).",
    )
    parser.add_argument(
        "--base",
        help="Base commit revision (must be specified together with --head).",
    )
    parser.add_argument(
        "--head",
        help="Head commit revision (must be specified together with --base).",
    )
    parser.add_argument(
        "--mode",
        choices=["direct", "dependent", "safe"],
        default="direct",
        help="Test selection mode: direct, dependent, or safe (default: direct).",
    )
    parser.add_argument(
        "--tests-root",
        action="append",
        dest="tests_roots",
        help="Explicit test discovery root relative to Git root (default: tests).",
    )
    parser.add_argument(
        "--pytest-arg",
        action="append",
        dest="pytest_args",
        help="Pass-through argument to pytest (can be specified multiple times).",
    )
    parser.add_argument(
        "--modootest-config",
        dest="modootest_config",
        help="Odoo configuration file forwarded to the selected pytest run.",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Report selection and ask pytest to collect without running tests.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the selected tests sequentially using pytest.",
    )
    parser.add_argument(
        "--log-file",
        help="Optional path to write detailed pytest subprocess output.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Subprocess timeout in seconds for pytest execution (default: 120.0).",
    )
    parser.add_argument(
        "--format",
        choices=["human", "json"],
        default=argparse.SUPPRESS,
        help="Output format: human-readable text or machine-readable JSON (default: human).",
    )



def create_parser(
    json_requested: bool = False,
    active_subcommand: str = "modootest",
) -> ModootestArgumentParser:
    """Create top-level argument parser for modootest CLI."""
    parser = ModootestArgumentParser(
        prog="modootest",
        description="modootest: Modern Odoo Test framework CLI.",
    )
    parser._json_requested = json_requested
    parser._active_subcommand = active_subcommand

    parser.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
        help="Output format: human text or machine-readable JSON (default: human).",
    )

    subparsers = parser.add_subparsers(
        dest="subcommand",
        metavar="<command>",
        parser_class=ModootestArgumentParser,
    )

    plan_parser = subparsers.add_parser(
        "plan",
        usage="%(prog)s --repo PATH --addons-path RELPATH [--addons-path RELPATH ...] (--base REV --head REV | --working-tree) [--format human|json]",
        help="Compute deterministic change intelligence and lifecycle execution plan.",
        description="Analyze Git changes, manifest dependencies, and lifecycle requirements.",
    )
    plan_parser._json_requested = json_requested
    plan_parser._active_subcommand = "plan"
    plan_parser.add_argument(
        "--repo",
        default=".",
        help="Path to target Git repository (default: current directory).",
    )
    plan_parser.add_argument(
        "--addons-path",
        action="append",
        dest="addons_paths",
        required=True,
        help="Explicit addon search root relative to Git root (can be specified multiple times).",
    )
    plan_parser.add_argument(
        "--working-tree",
        action="store_true",
        help="Compare HEAD to current working tree (including staged, unstaged, and untracked changes).",
    )
    plan_parser.add_argument(
        "--base",
        help="Base commit revision (must be specified together with --head).",
    )
    plan_parser.add_argument(
        "--head",
        help="Head commit revision (must be specified together with --base).",
    )
    plan_parser.add_argument(
        "--format",
        choices=["human", "json"],
        default=argparse.SUPPRESS,
        help="Output format: human-readable text or machine-readable JSON (default: human).",
    )


    test_parser = subparsers.add_parser(
        "test",
        usage=(
            "%(prog)s --impacted --repo PATH --addons-path RELPATH [--addons-path RELPATH ...] "
            "(--base REV --head REV | --working-tree) [--mode direct|dependent|safe] "
            "[--tests-root PATH ...] [--pytest-arg ARG ...] [--modootest-config FILE] "
            "[--collect-only] [--execute] "
            "[--format human|json]"
        ),
        help="Select and execute impacted tests.",
        description="Select impacted tests based on change intelligence plan and execute or dry-run them.",
    )
    test_parser._json_requested = json_requested
    test_parser._active_subcommand = "test"
    _add_impacted_args(test_parser, require_impacted_flag=True)

    impacted_parser = subparsers.add_parser(
        "impacted",
        usage=(
            "%(prog)s --repo PATH --addons-path RELPATH [--addons-path RELPATH ...] "
            "(--base REV --head REV | --working-tree) [--mode direct|dependent|safe] "
            "[--tests-root PATH ...] [--pytest-arg ARG ...] [--modootest-config FILE] "
            "[--collect-only] [--execute] "
            "[--format human|json]"
        ),
        help="Select and execute impacted tests (alias for 'modootest test --impacted').",
        description="Select impacted tests based on change intelligence plan and execute or dry-run them.",
    )
    impacted_parser._json_requested = json_requested
    impacted_parser._active_subcommand = "impacted"
    _add_impacted_args(impacted_parser, require_impacted_flag=False)

    watch_parser = subparsers.add_parser(
        "watch",
        usage=(
            "%(prog)s --repo PATH --addons-path RELPATH [--addons-path RELPATH ...] "
            "[--mode direct|dependent|safe] [--tests-root PATH ...] [--pytest-arg ARG ...] "
            "[--modootest-config FILE] "
            "[--collect-only] [--debounce SEC] [--poll-interval SEC] [--timeout SEC] "
            "[--grace-period SEC] [--format human|json]"
        ),
        help="Continuously monitor files and execute impacted tests on change.",
        description="Watch addon and test roots for changes, debounce bursts, and run impacted tests.",
    )
    watch_parser._json_requested = json_requested
    watch_parser._active_subcommand = "watch"
    watch_parser.add_argument(
        "--repo",
        default=".",
        help="Path to target Git repository (default: current directory).",
    )
    watch_parser.add_argument(
        "--addons-path",
        action="append",
        dest="addons_paths",
        required=True,
        help="Explicit addon search root relative to Git root (can be specified multiple times).",
    )
    watch_parser.add_argument(
        "--mode",
        choices=["direct", "dependent", "safe"],
        default="direct",
        help="Test selection mode: direct, dependent, or safe (default: direct).",
    )
    watch_parser.add_argument(
        "--tests-root",
        action="append",
        dest="tests_roots",
        help="Explicit test discovery root relative to Git root (default: tests).",
    )
    watch_parser.add_argument(
        "--pytest-arg",
        action="append",
        dest="pytest_args",
        help="Pass-through argument to pytest (can be specified multiple times).",
    )
    watch_parser.add_argument(
        "--modootest-config",
        help="Odoo configuration file forwarded to each watched pytest run.",
    )
    watch_parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Report selection and ask pytest to collect without running tests.",
    )
    watch_parser.add_argument(
        "--log-file",
        help="Optional path to write detailed pytest subprocess output.",
    )
    watch_parser.add_argument(
        "--debounce",
        type=float,
        default=0.3,
        help="Debounce interval in seconds to coalesce rapid file changes (default: 0.3).",
    )
    watch_parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Polling interval in seconds for checking filesystem snapshots (default: 0.5).",
    )
    watch_parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Execution timeout in seconds for pytest subprocess (default: 120.0).",
    )
    watch_parser.add_argument(
        "--grace-period",
        type=float,
        default=2.0,
        help="Termination grace period in seconds before SIGKILL escalation (default: 2.0).",
    )
    watch_parser.add_argument(
        "--format",
        choices=["human", "json"],
        default=argparse.SUPPRESS,
        help="Output format: human-readable text or machine-readable streaming JSON (default: human).",
    )


    return parser


def run_plan(args: argparse.Namespace) -> int:
    """Execute plan command and return appropriate exit code (0, 1, or 2)."""
    is_json = getattr(args, "format", "human") == "json"
    has_wt = bool(args.working_tree)
    has_base = bool(args.base)
    has_head = bool(args.head)

    if not has_wt and not (has_base and has_head):
        if (has_base and not has_head) or (has_head and not has_base):
            msg = "Both --base and --head must be specified in commit comparison mode."
        else:
            msg = "Exactly one comparison mode must be specified: either --working-tree or both --base and --head."
        if is_json:
            return _emit_json_error(
                "plan",
                msg,
                code="invalid_comparison_mode",
                category="cli",
                remediation="Specify either --working-tree or both --base and --head.",
            )
        sys.stderr.write(f"Error: {msg}\n")
        return 2

    if has_wt and (has_base or has_head):
        msg = "Cannot specify --base or --head together with --working-tree."
        if is_json:
            return _emit_json_error(
                "plan",
                msg,
                code="invalid_comparison_mode",
                category="cli",
                remediation="Specify either --working-tree or both --base and --head, not both.",
            )
        sys.stderr.write(f"Error: {msg}\n")
        return 2

    if not args.addons_paths:
        msg = "At least one --addons-path search root must be specified."
        if is_json:
            return _emit_json_error(
                "plan",
                msg,
                code="missing_addons_path",
                category="cli",
                remediation="Specify at least one --addons-path search root.",
            )
        sys.stderr.write(f"Error: {msg}\n")
        return 2

    try:
        plan = build_plan(
            repo_path=args.repo,
            addons_paths=args.addons_paths,
            base_rev=args.base,
            head_rev=args.head,
            working_tree=args.working_tree,
        )
    except (GitError, ManifestError) as err:
        if is_json:
            code = "git_error" if isinstance(err, GitError) else "manifest_error"
            cat = "git" if isinstance(err, GitError) else "manifest"
            return _emit_json_error(
                "plan",
                str(err),
                code=code,
                category=cat,
                remediation="Check git repository and manifest files.",
            )
        sys.stderr.write(f"Error: {err}\n")
        return 2
    except Exception as err:
        if is_json:
            return _emit_json_error(
                "plan",
                f"Fatal error during planning: {err}",
                code="planning_error",
                category="planning",
            )
        sys.stderr.write(f"Fatal error during planning: {err}\n")
        return 2

    if is_json:
        status = "complete" if plan.is_complete else "incomplete"
        exit_code = 0 if plan.is_complete else 1
        doc = serialize_result_envelope(
            command="plan",
            status=status,
            exit_code=exit_code,
            is_complete=plan.is_complete,
            diagnostics=plan.diagnostics,
            payload=serialize_plan(plan),
            execution=None,
        )
        sys.stdout.write(to_json_str(doc) + "\n")
        return exit_code

    output = plan.format_human_readable()
    sys.stdout.write(output + "\n")

    if not plan.is_complete:
        return 1

    return 0


def run_impacted(args: argparse.Namespace) -> int:
    """Execute impacted test selection and optional collection/execution."""
    is_json = getattr(args, "format", "human") == "json"
    cmd_name = getattr(args, "subcommand", "impacted")
    if cmd_name not in ("impacted", "test"):
        cmd_name = "impacted"

    has_wt = bool(args.working_tree)
    has_base = bool(args.base)
    has_head = bool(args.head)

    if not has_wt and not (has_base and has_head):
        if (has_base and not has_head) or (has_head and not has_base):
            msg = "Both --base and --head must be specified in commit comparison mode."
        else:
            msg = "Exactly one comparison mode must be specified: either --working-tree or both --base and --head."
        if is_json:
            return _emit_json_error(
                cmd_name,
                msg,
                code="invalid_comparison_mode",
                category="cli",
                remediation="Specify either --working-tree or both --base and --head.",
            )
        sys.stderr.write(f"Error: {msg}\n")
        return 2

    if has_wt and (has_base or has_head):
        msg = "Cannot specify --base or --head together with --working-tree."
        if is_json:
            return _emit_json_error(
                cmd_name,
                msg,
                code="invalid_comparison_mode",
                category="cli",
                remediation="Specify either --working-tree or both --base and --head, not both.",
            )
        sys.stderr.write(f"Error: {msg}\n")
        return 2

    if not args.addons_paths:
        msg = "At least one --addons-path search root must be specified."
        if is_json:
            return _emit_json_error(
                cmd_name,
                msg,
                code="missing_addons_path",
                category="cli",
                remediation="Specify at least one --addons-path search root.",
            )
        sys.stderr.write(f"Error: {msg}\n")
        return 2

    if args.collect_only and args.execute:
        msg = "Cannot specify both --collect-only and --execute."
        if is_json:
            return _emit_json_error(
                cmd_name,
                msg,
                code="conflicting_execution_modes",
                category="cli",
                remediation="Specify at most one of --collect-only or --execute.",
            )
        sys.stderr.write(f"Error: {msg}\n")
        return 2

    # Step 1: Build plan
    try:
        plan = build_plan(
            repo_path=args.repo,
            addons_paths=args.addons_paths,
            base_rev=args.base,
            head_rev=args.head,
            working_tree=args.working_tree,
        )
    except (GitError, ManifestError) as err:
        if is_json:
            code = "git_error" if isinstance(err, GitError) else "manifest_error"
            cat = "git" if isinstance(err, GitError) else "manifest"
            return _emit_json_error(
                cmd_name,
                str(err),
                code=code,
                category=cat,
                remediation="Verify git repository and manifest files.",
            )
        sys.stderr.write(f"Error: {err}\n")
        return 2
    except Exception as err:
        if is_json:
            return _emit_json_error(
                cmd_name,
                f"Fatal error during planning: {err}",
                code="planning_error",
                category="planning",
            )
        sys.stderr.write(f"Fatal error during planning: {err}\n")
        return 2

    # Step 2: Select impacted tests
    tests_roots = args.tests_roots
    try:
        selection = select_impacted_tests(
            plan=plan,
            mode=args.mode,
            tests_roots=tests_roots,
        )
    except (SelectorError, ValueError) as err:
        if is_json:
            return _emit_json_error(
                cmd_name,
                str(err),
                code="selector_error",
                category="selection",
                remediation="Verify test discovery roots and paths.",
            )
        sys.stderr.write(f"Error: {err}\n")
        return 2
    except Exception as err:
        if is_json:
            return _emit_json_error(
                cmd_name,
                f"Fatal error during test selection: {err}",
                code="selection_error",
                category="selection",
            )
        sys.stderr.write(f"Fatal error during test selection: {err}\n")
        return 2

    # Step 3: Validate pytest arguments (must validate even in dry-run mode!)
    git_root = Path(plan.git_root)
    raw_pytest_args = args.pytest_args or []
    is_valid, err_msg, safe_pytest_args = _validate_pytest_args(
        raw_pytest_args, git_root
    )
    if not is_valid:
        if is_json:
            return _emit_json_error(
                cmd_name,
                err_msg,
                code="invalid_pytest_arguments",
                category="pytest",
                remediation="Use only allowed safe arguments from allowlist.",
            )
        sys.stderr.write(f"Error: {err_msg}\n")
        return 2

    config_path: Path | None = None
    if args.modootest_config:
        try:
            config_path = _resolve_modootest_config_path(args.modootest_config)
        except ValueError as err:
            if is_json:
                return _emit_json_error(
                    cmd_name,
                    str(err),
                    code="invalid_modootest_config",
                    category="configuration",
                    remediation="Provide an existing Odoo configuration file.",
                )
            sys.stderr.write(f"Error: {err}\n")
            return 2

    # If dry-run (neither --collect-only nor --execute)
    if not args.collect_only and not args.execute:
        if is_json:
            status = "complete" if selection.is_complete else "incomplete"
            exit_code = 0 if selection.is_complete else 1
            exec_res = ExecutionResult(
                status=ExecutionStatus.NOT_EXECUTED,
                returncode=None,
                executed=False,
                collect_only=False,
                test_count=len(selection.test_paths),
                log_file=None,
                summary="Dry-run selection completed; no tests executed.",
            )
            doc = serialize_result_envelope(
                command=cmd_name,
                status=status,
                exit_code=exit_code,
                is_complete=selection.is_complete,
                diagnostics=selection.diagnostics,
                payload=serialize_selection(selection),
                execution=exec_res,
            )
            sys.stdout.write(to_json_str(doc) + "\n")
            return exit_code
        else:
            sys.stdout.write(selection.format_human_readable() + "\n")
            return 0 if selection.is_complete else 1

    # If no tests were selected
    if not selection.selected_tests:
        if is_json:
            status = "complete" if selection.is_complete else "incomplete"
            exit_code = 0 if selection.is_complete else 1
            action_name = "collect" if args.collect_only else "execute"
            exec_res = ExecutionResult(
                status=ExecutionStatus.NO_TESTS_SELECTED,
                returncode=None,
                executed=False,
                collect_only=bool(args.collect_only),
                test_count=0,
                log_file=None,
                summary=f"No tests selected to {action_name}.",
            )
            doc = serialize_result_envelope(
                command=cmd_name,
                status=status,
                exit_code=exit_code,
                is_complete=selection.is_complete,
                diagnostics=selection.diagnostics,
                payload=serialize_selection(selection),
                execution=exec_res,
            )
            sys.stdout.write(to_json_str(doc) + "\n")
            return exit_code
        else:
            sys.stdout.write(selection.format_human_readable() + "\n")
            action_name = "collect" if args.collect_only else "execute"
            sys.stdout.write(f"No tests selected to {action_name}.\n")
            return 0 if selection.is_complete else 1

    # Step 4: Run pytest subprocess
    log_path = Path(args.log_file) if args.log_file else None
    returncode, category, written_log = _run_pytest_subprocess(
        git_root=git_root,
        test_paths=selection.test_paths,
        pytest_args=safe_pytest_args,
        collect_only=bool(args.collect_only),
        log_file_path=log_path,
        modootest_config=config_path,
        timeout=float(args.timeout),
    )

    if not is_json:
        sys.stdout.write(selection.format_human_readable() + "\n")
        if returncode != 0:
            sys.stderr.write(
                f"pytest failed: exit code {returncode} ({category}). Log: {written_log}\n"
            )
            return 3

        action_str = "collection" if args.collect_only else "execution"
        sys.stdout.write(f"pytest {action_str} succeeded. Log: {written_log}\n")
        return 0 if selection.is_complete else 1

    # JSON mode handling
    if category == "timeout":
        exec_status = ExecutionStatus.TIMEOUT
        summary = "Pytest subprocess timed out"
        executed = True
        reported_rc: int | None = 124
    elif category.startswith("execution error"):
        exec_status = ExecutionStatus.LAUNCH_ERROR
        summary = "Pytest subprocess failed to launch"
        executed = False
        reported_rc = None
    else:
        exec_status, reported_rc = map_subprocess_returncode(returncode, bool(args.collect_only))
        executed = True
        if exec_status == ExecutionStatus.COLLECTION_PASSED:
            summary = "pytest collection succeeded"
        elif exec_status == ExecutionStatus.EXECUTION_PASSED:
            summary = "pytest execution succeeded"
        elif exec_status == ExecutionStatus.TESTS_FAILED:
            summary = "pytest tests failed"
        elif exec_status == ExecutionStatus.INTERRUPTED:
            summary = "pytest execution was interrupted"
        elif exec_status == ExecutionStatus.PYTEST_INTERNAL_ERROR:
            summary = "pytest internal error"
        elif exec_status == ExecutionStatus.PYTEST_USAGE_ERROR:
            summary = "pytest usage error"
        elif exec_status == ExecutionStatus.NO_TESTS_COLLECTED:
            summary = "pytest collected no tests"
        elif exec_status == ExecutionStatus.UNKNOWN_SUBPROCESS_FAILURE:
            summary = f"pytest failed with unknown returncode {returncode}"
        else:
            summary = f"pytest exited with code {returncode}"

    log_str = str(written_log)
    try:
        log_str = str(written_log.resolve().relative_to(git_root.resolve()))
    except ValueError:
        pass

    exec_res = ExecutionResult(
        status=exec_status,
        returncode=reported_rc,
        executed=executed,
        collect_only=bool(args.collect_only),
        test_count=len(selection.test_paths),
        log_file=log_str,
        summary=summary,
    )


    if returncode != 0:
        exit_code = 3
        status = "incomplete" if not selection.is_complete else "error"
        is_complete = False
    else:
        exit_code = 0 if selection.is_complete else 1
        status = "complete" if selection.is_complete else "incomplete"
        is_complete = selection.is_complete

    doc = serialize_result_envelope(
        command=cmd_name,
        status=status,
        exit_code=exit_code,
        is_complete=is_complete,
        diagnostics=selection.diagnostics,
        payload=serialize_selection(selection),
        execution=exec_res,
    )
    sys.stdout.write(to_json_str(doc) + "\n")
    return exit_code


def run_test(args: argparse.Namespace) -> int:
    """Handle 'modootest test' subcommand."""
    is_json = getattr(args, "format", "human") == "json"
    if not getattr(args, "impacted", False):
        if is_json:
            return _emit_json_error(
                "test",
                "'modootest test' currently requires --impacted.",
                code="missing_impacted_flag",
                category="cli",
                remediation="Specify --impacted flag when invoking 'modootest test'.",
            )
        sys.stderr.write(
            "Error: 'modootest test' currently requires --impacted.\n"
            "Usage: modootest test --impacted --repo PATH --addons-path RELPATH ...\n"
        )
        return 2
    return run_impacted(args)


def run_watch(args: argparse.Namespace) -> int:
    """Execute watch supervisor."""
    is_json = getattr(args, "format", "human") == "json"

    if not args.addons_paths:
        if is_json:
            return _emit_json_error(
                "watch",
                "At least one --addons-path search root must be specified.",
                code="missing_addons_path",
                category="cli",
                remediation="Specify --addons-path RELPATH.",
            )
        sys.stderr.write("Error: At least one --addons-path search root must be specified.\n")
        return 2

    if args.debounce <= 0 or args.poll_interval <= 0 or args.timeout <= 0 or args.grace_period <= 0:
        err_msg = "Watch timing parameters (--debounce, --poll-interval, --timeout, --grace-period) must all be positive numbers."
        if is_json:
            return _emit_json_error(
                "watch",
                err_msg,
                code="invalid_timing_parameter",
                category="cli",
                remediation="Provide positive numbers for debounce, poll-interval, timeout, and grace-period.",
            )
        sys.stderr.write(f"Error: {err_msg}\n")
        return 2

    try:
        git_root = discover_git_root(Path(args.repo))
    except Exception as err:
        if is_json:
            return _emit_json_error(
                "watch",
                str(err),
                code="invalid_git_repository",
                category="git",
                remediation="Ensure --repo points to a valid Git repository root.",
            )
        sys.stderr.write(f"Error: {err}\n")
        return 2

    try:
        raw_roots: list[str] = list(args.addons_paths)
        if args.tests_roots:
            raw_roots.extend(args.tests_roots)
        else:
            def_tests = git_root / "tests"
            if def_tests.is_dir():
                raw_roots.append("tests")
        validate_watch_roots(git_root, raw_roots)
    except Exception as err:
        if is_json:
            return _emit_json_error(
                "watch",
                str(err),
                code="invalid_watch_roots",
                category="watch",
                remediation="Provide valid, repository-contained directory roots without symlinks.",
            )
        sys.stderr.write(f"Error: {err}\n")
        return 2

    raw_pytest_args = args.pytest_args or []
    is_valid, err_msg, safe_pytest_args = validate_pytest_args(
        raw_pytest_args, git_root
    )
    if not is_valid:
        if is_json:
            return _emit_json_error(
                "watch",
                err_msg,
                code="invalid_pytest_arguments",
                category="pytest",
                remediation="Use only allowed safe arguments from allowlist.",
            )
        sys.stderr.write(f"Error: {err_msg}\n")
        return 2

    config_path: Path | None = None
    if args.modootest_config:
        try:
            config_path = _resolve_modootest_config_path(args.modootest_config)
        except ValueError as err:
            if is_json:
                return _emit_json_error(
                    "watch",
                    str(err),
                    code="invalid_modootest_config",
                    category="configuration",
                    remediation="Provide an existing Odoo configuration file.",
                )
            sys.stderr.write(f"Error: {err}\n")
            return 2

    try:
        supervisor = WatchSupervisor(
            git_root=git_root,
            addons_paths=args.addons_paths,
            mode=args.mode,
            tests_roots=args.tests_roots,
            pytest_args=safe_pytest_args,
            modootest_config=config_path,
            collect_only=bool(args.collect_only),
            log_file=Path(args.log_file) if args.log_file else None,
            debounce_interval=float(args.debounce),
            poll_interval=float(args.poll_interval),
            timeout=float(args.timeout),
            grace_period=float(args.grace_period),
            format_type=getattr(args, "format", "human"),
        )
        return supervisor.run()
    except (GitError, ManifestError, SelectorError, ValueError, WatchSnapshotError) as err:
        if is_json:
            return _emit_json_error(
                "watch",
                str(err),
                code="watch_setup_error",
                category="watch",
                remediation="Check watch configuration and repository status.",
            )
        sys.stderr.write(f"Error: {err}\n")
        return 2



def _preprocess_argv(argv: Sequence[str]) -> list[str]:
    """Preprocess CLI argv tokens so option-like values passed to --pytest-arg are bound cleanly."""
    processed: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        token = argv[i]
        if token == "--pytest-arg" and i + 1 < n:
            processed.append(f"--pytest-arg={argv[i + 1]}")
            i += 2
        else:
            processed.append(token)
            i += 1
    return processed


def _is_json_format_requested(argv: Sequence[str]) -> bool:
    """Check if --format json was explicitly requested in raw argv."""
    for i, token in enumerate(argv):
        if token == "--format=json":
            return True
        if token == "--format" and i + 1 < len(argv) and argv[i + 1] == "json":
            return True
    return False


def _detect_subcommand(argv: Sequence[str]) -> str:
    """Determine likely subcommand from raw argv."""
    known = {"plan", "test", "impacted", "watch"}
    for token in argv:
        if token in known:
            return token
    return "modootest"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for modootest command."""
    if argv is None:
        argv = sys.argv[1:]

    argv = _preprocess_argv(argv)
    json_requested = _is_json_format_requested(argv)
    detected_subcommand = _detect_subcommand(argv)

    if not argv:
        if json_requested:
            return _emit_json_error(
                "modootest",
                "No command specified.",
                code="missing_command",
                category="cli",
                remediation="Specify a command: plan, test, impacted, or watch.",
            )
        parser = create_parser(json_requested=False)
        parser.print_help(sys.stderr)
        return 2

    parser = create_parser(
        json_requested=json_requested,
        active_subcommand=detected_subcommand,
    )

    try:
        args = parser.parse_args(argv)
    except ArgparseJsonError as exc:
        return _emit_json_error(
            exc.command,
            exc.message,
            code="invalid_cli_arguments",
            category="cli",
            remediation="Check command arguments and syntax using modootest <command> --help.",
            exit_code=2,
        )
    except SystemExit as exc:
        return exc.code

    if args.subcommand == "plan":
        return run_plan(args)
    elif args.subcommand == "test":
        return run_test(args)
    elif args.subcommand == "impacted":
        return run_impacted(args)
    elif args.subcommand == "watch":
        return run_watch(args)
    else:
        if json_requested:
            return _emit_json_error(
                "modootest",
                "No valid subcommand specified.",
                code="missing_subcommand",
                category="cli",
                remediation="Provide a valid subcommand: plan, test, impacted, watch.",
            )
        parser.print_help(sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
