"""Subprocess execution and process-group lifecycle management for watch supervisor."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Protocol, Sequence

from modootest.agent.models import ExecutionResult, ExecutionStatus, map_subprocess_returncode


def map_pytest_returncode(returncode: int, collect_only: bool) -> tuple[ExecutionStatus, str]:
    """Map pytest returncode to ExecutionStatus and concise summary."""
    status, _ = map_subprocess_returncode(returncode, collect_only)
    if status == ExecutionStatus.COLLECTION_PASSED:
        summary = "pytest collection succeeded"
    elif status == ExecutionStatus.EXECUTION_PASSED:
        summary = "pytest execution succeeded"
    elif status == ExecutionStatus.TESTS_FAILED:
        summary = "pytest tests failed"
    elif status == ExecutionStatus.INTERRUPTED:
        summary = "pytest execution was interrupted"
    elif status == ExecutionStatus.PYTEST_INTERNAL_ERROR:
        summary = "pytest internal error"
    elif status == ExecutionStatus.PYTEST_USAGE_ERROR:
        summary = "pytest command-line usage error"
    elif status == ExecutionStatus.NO_TESTS_COLLECTED:
        summary = "pytest collected no tests"
    elif status == ExecutionStatus.TIMEOUT:
        summary = "pytest execution timed out"
    elif status == ExecutionStatus.CANCELLED:
        summary = "pytest execution was cancelled"
    elif status == ExecutionStatus.UNKNOWN_SUBPROCESS_FAILURE:
        summary = f"pytest failed with unknown returncode {returncode}"
    else:
        summary = f"pytest exited with returncode {returncode}"
    return status, summary


class ProcessRunnerProtocol(Protocol):
    """Protocol for pytest subprocess execution seams."""

    def start(
        self,
        cmd: Sequence[str],
        cwd: Path,
        log_file: Path,
        timeout: float,
    ) -> None: ...

    def is_running(self) -> bool: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def cancel(self, grace_period: float = 2.0) -> None: ...

    def get_result(self, collect_only: bool, test_count: int, log_file: Path) -> ExecutionResult: ...


class PytestProcessRunner:
    """Production process runner using process-group isolation and graceful escalation."""

    def __init__(
        self,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._log_file_handle: Any = None
        self._sleep_fn = sleep_fn
        self._was_cancelled = False
        self._was_timed_out = False
        self._start_time: float = 0.0
        self._timeout: float = 120.0
        self._log_file: Path | None = None

    def start(
        self,
        cmd: Sequence[str],
        cwd: Path,
        log_file: Path,
        timeout: float = 120.0,
    ) -> None:
        """Start pytest child process in isolated process group on POSIX."""
        self._was_cancelled = False
        self._was_timed_out = False
        self._timeout = timeout
        self._start_time = time.monotonic()
        self._log_file = log_file

        log_file.parent.mkdir(parents=True, exist_ok=True)
        self._log_file_handle = open(log_file, "w", encoding="utf-8")
        try:
            self._log_file_handle.write(f"=== Pytest Command ===\n{' '.join(cmd)}\n\n=== Subprocess Output ===\n")
            self._log_file_handle.flush()

            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONDONTWRITEBYTECODE"] = "1"

            kwargs: dict[str, Any] = {
                "cwd": str(cwd),
                "env": env,
                "stdout": self._log_file_handle,
                "stderr": subprocess.STDOUT,
                "text": True,
            }

            # Isolate child process group on POSIX systems
            if os.name == "posix":
                kwargs["start_new_session"] = True

            self._proc = subprocess.Popen(list(cmd), **kwargs)
        except Exception:
            self._cleanup_handle()
            raise

    def is_running(self) -> bool:
        """Return True if child process is currently alive and not timed out."""
        if self._proc is None:
            return False
        return self.poll() is None

    def poll(self) -> int | None:
        """Poll child process returncode or check timeout."""
        if self._proc is None:
            return None
        if self._was_timed_out:
            return -1
        if self._was_cancelled:
            return -1
        rc = self._proc.poll()
        if rc is None:
            if (time.monotonic() - self._start_time) >= self._timeout:
                self._was_timed_out = True
                self.cancel(grace_period=1.0)
                return -1
            return None
        self._cleanup_handle()
        return rc

    def wait(self, timeout: float | None = None) -> int:
        """Wait for child process to terminate and reap it."""
        if self._proc is None:
            return 0
        try:
            return self._proc.wait(timeout=timeout)
        finally:
            self._cleanup_handle()

    def cancel(self, grace_period: float = 2.0) -> None:
        """Cancel active process group with SIGTERM escalating to SIGKILL after grace period."""
        if self._proc is None or self._proc.poll() is not None:
            self._cleanup_handle()
            return

        self._was_cancelled = True
        pid = self._proc.pid

        # Attempt to terminate process group on POSIX
        terminated_group = False
        if os.name == "posix":
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                terminated_group = True
            except (OSError, ProcessLookupError):
                pass

        if not terminated_group:
            try:
                self._proc.terminate()
            except OSError:
                pass

        # Wait with grace period
        deadline = time.monotonic() + grace_period
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                self._cleanup_handle()
                return
            self._sleep_fn(0.01)

        # Escalate to SIGKILL if still running
        if self._proc.poll() is None:
            killed_group = False
            if os.name == "posix":
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGKILL)
                    killed_group = True
                except (OSError, ProcessLookupError):
                    pass
            if not killed_group:
                try:
                    self._proc.kill()
                except OSError:
                    pass

        # Reap zombie
        try:
            self._proc.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError):
            pass
        finally:
            self._cleanup_handle()

    def _cleanup_handle(self) -> None:
        if self._log_file_handle is not None and not self._log_file_handle.closed:
            try:
                self._log_file_handle.flush()
                self._log_file_handle.close()
            except OSError:
                pass
            self._log_file_handle = None

    def get_result(self, collect_only: bool, test_count: int, log_file: Path) -> ExecutionResult:
        """Construct ExecutionResult from finished run."""
        log_str = str(log_file)
        if self._was_timed_out:
            return ExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                returncode=124,
                executed=True,
                collect_only=collect_only,
                test_count=test_count,
                log_file=log_str,
                summary="Execution timed out",
            )
        if self._was_cancelled:
            return ExecutionResult(
                status=ExecutionStatus.CANCELLED,
                returncode=143,
                executed=True,
                collect_only=collect_only,
                test_count=test_count,
                log_file=log_str,
                summary="Execution cancelled by watcher",
            )
        if self._proc is None:
            return ExecutionResult(
                status=ExecutionStatus.LAUNCH_ERROR,
                returncode=-1,
                executed=False,
                collect_only=collect_only,
                test_count=test_count,
                log_file=log_str,
                summary="Process failed to start",
            )

        rc = self._proc.poll() if self._proc.poll() is not None else self._proc.wait()
        status, summary = map_pytest_returncode(rc, collect_only)
        return ExecutionResult(
            status=status,
            returncode=rc,
            executed=True,
            collect_only=collect_only,
            test_count=test_count,
            log_file=log_str,
            summary=summary,
        )
