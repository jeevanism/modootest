"""Watch supervisor coordinating change detection, debounce, and test execution."""

from __future__ import annotations

from pathlib import Path
import signal
import sys
import time
from typing import Any, Callable, Sequence

from modootest.agent.models import ExecutionResult, ExecutionStatus
from modootest.agent.pytest_args import validate_pytest_args
from modootest.agent.serializers import (
    serialize_result_envelope,
    serialize_watch_event,
    to_json_str,
    to_ndjson_line,
)
from modootest.intelligence.git import GitError, check_head_exists, discover_git_root
from modootest.intelligence.manifest import ManifestError
from modootest.intelligence.planner import (
    Diagnostic,
    DiagnosticSeverity,
    build_plan,
)
from modootest.intelligence.selector import SelectionMode, SelectorError, select_impacted_tests
from modootest.watch.runner import ProcessRunnerProtocol, PytestProcessRunner
from modootest.watch.snapshot import (
    WatchSnapshotError,
    detect_changes,
    take_snapshot,
    validate_watch_roots,
)


class WatchSupervisor:
    """Production supervisor for modootest watch mode."""

    def __init__(
        self,
        git_root: Path,
        addons_paths: Sequence[str],
        mode: SelectionMode | str = SelectionMode.DIRECT,
        tests_roots: Sequence[str] | None = None,
        pytest_args: Sequence[str] | None = None,
        modootest_config: Path | None = None,
        collect_only: bool = False,
        debounce_interval: float = 0.3,
        poll_interval: float = 0.5,
        timeout: float = 120.0,
        grace_period: float = 2.0,
        format_type: str = "human",
        output_format: str | None = None,
        log_file: Path | None = None,
        output_fn: Callable[[str], None] = sys.stdout.write,
        runner_factory: Callable[[], ProcessRunnerProtocol] = PytestProcessRunner,
        snapshot_fn: Callable[..., dict[str, Any]] = take_snapshot,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.git_root = git_root
        self.addons_paths = list(addons_paths)
        if not isinstance(mode, SelectionMode):
            try:
                self.mode = SelectionMode(mode)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid selection mode: {mode!r}") from exc
        else:
            self.mode = mode

        if debounce_interval <= 0:
            raise ValueError(f"debounce_interval must be positive, got {debounce_interval}")
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be positive, got {poll_interval}")
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        if grace_period <= 0:
            raise ValueError(f"grace_period must be positive, got {grace_period}")

        self.tests_roots = list(tests_roots) if tests_roots else None
        self.pytest_args = list(pytest_args) if pytest_args else []
        self.modootest_config = modootest_config
        self._safe_pytest_args: list[str] = []
        self.collect_only = collect_only
        self.debounce_interval = float(debounce_interval)
        self.poll_interval = float(poll_interval)
        self.timeout = float(timeout)
        self.grace_period = float(grace_period)
        self.format_type = output_format if output_format is not None else format_type
        self.log_file = log_file

        self._output_fn = output_fn
        self._runner_factory = runner_factory
        self._snapshot_fn = snapshot_fn
        self._clock_fn = clock_fn
        self._sleep_fn = sleep_fn

        self._stop_requested = False
        self._sequence = 0
        self._active_runner: ProcessRunnerProtocol | None = None

    def _emit_event(self, event_type: str, data: dict[str, Any], human_msg: str) -> None:
        self._sequence += 1
        if self.format_type == "json":
            payload = serialize_watch_event(event_type, self._sequence, data)
            self._output_fn(to_ndjson_line(payload) + "\n")
        else:
            self._output_fn(f"[{event_type.upper()}] {human_msg}\n")

    def stop(self) -> None:
        """Signal the supervisor to stop intake and exit."""
        self._stop_requested = True

    def _debounce(
        self,
        watch_paths: list[Path],
        ignore_set: set[Path],
        current_snap: dict[str, Any],
    ) -> dict[str, Any]:
        """Debounce burst changes using monotonic clock."""
        last_change = self._clock_fn()
        while not self._stop_requested:
            elapsed = self._clock_fn() - last_change
            if elapsed >= self.debounce_interval:
                break
            self._sleep_fn(min(self.poll_interval, 0.05))
            snap = self._snapshot_fn(self.git_root, watch_paths, ignore_set)
            c, m, d = detect_changes(current_snap, snap)
            if c or m or d:
                current_snap = snap
                last_change = self._clock_fn()
        return current_snap

    def _execute_cycle_iterative(
        self,
        watch_paths: list[Path],
        ignore_set: set[Path],
        last_snapshot: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Execute one test selection/run cycle iteratively.

        Returns (updated_snapshot, restart_needed).
        """
        try:
            plan = build_plan(
                repo_path=self.git_root,
                addons_paths=self.addons_paths,
                working_tree=True,
            )
            selection = select_impacted_tests(
                plan=plan,
                mode=self.mode,
                tests_roots=self.tests_roots,
            )
        except (GitError, ManifestError, SelectorError, ValueError) as exc:
            self._emit_event(
                "planning_error",
                {"error": str(exc)},
                f"Planning/selection error: {exc}",
            )
            return last_snapshot, False

        test_paths = selection.test_paths
        safe_pytest_args = self._safe_pytest_args

        if not test_paths:
            self._emit_event(
                "run_completed",
                {
                    "collect_only": self.collect_only,
                    "executed": False,
                    "returncode": 0,
                    "status": "no_tests_selected",
                    "summary": "No impacted tests selected",
                    "test_count": 0,
                },
                "No tests selected.",
            )
            return last_snapshot, False

        log_path = self.log_file
        if log_path is None:
            log_dir = self.git_root / ".ai-handoff" / "0010-agent-interface" / "logs"
            if log_dir.is_dir():
                log_path = log_dir / "pytest_watch.log"
            else:
                log_path = self.git_root / ".ai-handoff" / "pytest_watch.log"

        runner = self._runner_factory()
        self._active_runner = runner

        cmd = [sys.executable, "-m", "pytest"]
        if self.collect_only:
            cmd.append("--collect-only")
        if self.modootest_config is not None:
            cmd.append(f"--modootest-config={self.modootest_config}")
        cmd.extend(test_paths)
        cmd.extend(safe_pytest_args)

        self._emit_event(
            "run_started",
            {
                "collect_only": self.collect_only,
                "mode": self.mode.value,
                "test_count": len(test_paths),
                "test_paths": list(test_paths),
            },
            f"Starting run for {len(test_paths)} test files ({self.mode.value} mode)...",
        )

        try:
            runner.start(cmd=cmd, cwd=self.git_root, log_file=log_path, timeout=self.timeout)
        except Exception as exc:
            self._emit_event(
                "run_completed",
                {
                    "collect_only": self.collect_only,
                    "error": str(exc),
                    "executed": False,
                    "returncode": None,
                    "status": "launch_error",
                    "summary": f"Process launch failed: {exc}",
                    "test_count": len(test_paths),
                },
                f"Failed to launch pytest subprocess: {exc}",
            )
            self._active_runner = None
            return last_snapshot, False

        # Monitor loop during run
        current_snap = last_snapshot
        restart_needed = False

        while runner.is_running():
            if self._stop_requested:
                runner.cancel(grace_period=self.grace_period)
                break

            # Poll for file changes during run
            snap = self._snapshot_fn(self.git_root, watch_paths, ignore_set)
            c, m, d = detect_changes(current_snap, snap)
            if c or m or d:
                changed = sorted(c + m + d)
                self._emit_event(
                    "change_detected",
                    {"changed_files": changed},
                    f"File changes detected during run ({len(changed)} files). Cancelling active run...",
                )
                runner.cancel(grace_period=self.grace_period)
                res = runner.get_result(
                    collect_only=self.collect_only,
                    test_count=len(test_paths),
                    log_file=log_path,
                )
                self._emit_event(
                    "run_completed",
                    {
                        "collect_only": res.collect_only,
                        "executed": res.executed,
                        "log_file": res.log_file,
                        "returncode": res.returncode,
                        "status": res.status.value,
                        "summary": res.summary,
                        "test_count": res.test_count,
                    },
                    f"Run cancelled: {res.summary}",
                )
                self._active_runner = None
                current_snap = snap
                current_snap = self._debounce(watch_paths, ignore_set, current_snap)
                restart_needed = True
                break

            self._sleep_fn(min(self.poll_interval, 0.05))

        self._active_runner = None

        if restart_needed:
            return current_snap, True

        res = runner.get_result(
            collect_only=self.collect_only,
            test_count=len(test_paths),
            log_file=log_path,
        )
        self._emit_event(
            "run_completed",
            {
                "collect_only": res.collect_only,
                "executed": res.executed,
                "log_file": res.log_file,
                "returncode": res.returncode,
                "status": res.status.value,
                "summary": res.summary,
                "test_count": res.test_count,
            },
            f"Run finished: {res.status.value} (exit code: {res.returncode}). Log: {res.log_file}",
        )
        return current_snap, False

    def run(self) -> int:
        """Run the supervisor main loop iteratively until stopped."""
        # 1. Discover canonical Git root
        try:
            canonical_git_root = discover_git_root(self.git_root)
            check_head_exists(canonical_git_root)
        except GitError as err:
            if self.format_type == "json":
                diag = Diagnostic(
                    severity=DiagnosticSeverity.ERROR,
                    message=str(err),
                    code="invalid_git_repository",
                    category="git",
                    remediation="Ensure repository points to a valid Git repository root.",
                )
                doc = serialize_result_envelope(
                    command="watch",
                    status="error",
                    exit_code=2,
                    is_complete=False,
                    diagnostics=[diag],
                )
                self._output_fn(to_json_str(doc) + "\n")
            else:
                sys.stderr.write(f"Error: {err}\n")
            return 2

        self.git_root = canonical_git_root

        # 2. Validate watch roots fail-closed
        try:
            raw_roots: list[str] = list(self.addons_paths)
            if self.tests_roots:
                raw_roots.extend(self.tests_roots)
            else:
                def_tests = self.git_root / "tests"
                if def_tests.is_dir():
                    raw_roots.append("tests")
            watch_paths = validate_watch_roots(self.git_root, raw_roots)
        except (WatchSnapshotError, ValueError) as err:
            if self.format_type == "json":
                diag = Diagnostic(
                    severity=DiagnosticSeverity.ERROR,
                    message=str(err),
                    code="invalid_watch_roots",
                    category="watch",
                    remediation="Provide valid, repository-contained directory roots without symlinks.",
                )
                doc = serialize_result_envelope(
                    command="watch",
                    status="error",
                    exit_code=2,
                    is_complete=False,
                    diagnostics=[diag],
                )
                self._output_fn(to_json_str(doc) + "\n")
            else:
                sys.stderr.write(f"Error: {err}\n")
            return 2

        # 3. Validate pytest args fail-closed before handlers, watch_started, or snapshot
        is_valid, err_msg, safe_pytest_args = validate_pytest_args(
            self.pytest_args, self.git_root
        )
        if not is_valid:
            if self.format_type == "json":
                diag = Diagnostic(
                    severity=DiagnosticSeverity.ERROR,
                    message=err_msg,
                    code="invalid_pytest_arguments",
                    category="pytest",
                    remediation="Use only allowed safe arguments from allowlist.",
                )
                doc = serialize_result_envelope(
                    command="watch",
                    status="error",
                    exit_code=2,
                    is_complete=False,
                    diagnostics=[diag],
                )
                self._output_fn(to_json_str(doc) + "\n")
            else:
                sys.stderr.write(f"Error: {err_msg}\n")
            return 2

        self._safe_pytest_args = safe_pytest_args

        # 4. Signal handlers installation inside try...finally
        original_sigint = None
        original_sigterm = None
        installed_handlers = False

        watch_started = False
        try:
            try:
                def _sig_handler(signum: int, frame: Any) -> None:
                    self._stop_requested = True

                original_sigint = signal.signal(signal.SIGINT, _sig_handler)
                original_sigterm = signal.signal(signal.SIGTERM, _sig_handler)
                installed_handlers = True
            except (ValueError, AttributeError):
                pass

            # 5. Emit watch_started
            self._emit_event(
                "watch_started",
                {
                    "addons_paths": list(self.addons_paths),
                    "debounce": self.debounce_interval,
                    "mode": self.mode.value,
                    "poll_interval": self.poll_interval,
                    "repo": str(self.git_root),
                    "timeout": self.timeout,
                },
                f"Watch started on repository '{self.git_root}' ({len(watch_paths)} watch roots).",
            )
            watch_started = True

            ignore_set: set[Path] = set()
            if self.log_file:
                ignore_set.add(self.log_file)
            default_log_dir = self.git_root / ".ai-handoff"
            ignore_set.add(default_log_dir)

            # Baseline snapshot
            try:
                snapshot = self._snapshot_fn(self.git_root, watch_paths, ignore_set)
            except Exception as exc:
                self._emit_event("planning_error", {"error": str(exc)}, f"Snapshot failure: {exc}")
                return 2

            # 6. Iterative main loop
            need_run = True

            while not self._stop_requested:
                if need_run:
                    need_run = False
                    snapshot, restart_needed = self._execute_cycle_iterative(
                        watch_paths, ignore_set, snapshot
                    )
                    if restart_needed:
                        need_run = True
                        continue

                self._sleep_fn(self.poll_interval)
                if self._stop_requested:
                    break

                new_snap = self._snapshot_fn(self.git_root, watch_paths, ignore_set)
                c, m, d = detect_changes(snapshot, new_snap)
                if c or m or d:
                    changed = sorted(c + m + d)
                    self._emit_event(
                        "change_detected",
                        {"changed_files": changed},
                        f"Change detected: {len(changed)} file(s).",
                    )
                    snapshot = new_snap
                    snapshot = self._debounce(watch_paths, ignore_set, snapshot)
                    need_run = True

            return 0
        finally:
            if self._active_runner is not None and self._active_runner.is_running():
                self._active_runner.cancel(grace_period=self.grace_period)
            self._active_runner = None
            if watch_started:
                self._emit_event("watch_stopped", {}, "Watch stopped.")
            if installed_handlers:
                try:
                    if original_sigint is not None:
                        signal.signal(signal.SIGINT, original_sigint)
                    if original_sigterm is not None:
                        signal.signal(signal.SIGTERM, original_sigterm)
                except (ValueError, AttributeError):
                    pass
