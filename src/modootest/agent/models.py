"""Data models and statuses for agent interface execution outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExecutionStatus(str, Enum):
    """Subprocess and execution outcome categories."""

    NOT_EXECUTED = "not_executed"
    NO_TESTS_SELECTED = "no_tests_selected"
    COLLECTION_PASSED = "collection_passed"
    EXECUTION_PASSED = "execution_passed"
    TESTS_FAILED = "tests_failed"
    INTERRUPTED = "interrupted"
    PYTEST_INTERNAL_ERROR = "pytest_internal_error"
    PYTEST_USAGE_ERROR = "pytest_usage_error"
    NO_TESTS_COLLECTED = "no_tests_collected"
    TIMEOUT = "timeout"
    LAUNCH_ERROR = "launch_error"
    CANCELLED = "cancelled"
    UNKNOWN_SUBPROCESS_FAILURE = "unknown_subprocess_failure"


def map_subprocess_returncode(
    returncode: int,
    collect_only: bool = False,
) -> tuple[ExecutionStatus, int]:
    """Map a subprocess returncode to an ExecutionStatus and canonical exit code."""
    if returncode == 0:
        return (
            ExecutionStatus.COLLECTION_PASSED
            if collect_only
            else ExecutionStatus.EXECUTION_PASSED,
            0,
        )
    if returncode == 1:
        return ExecutionStatus.TESTS_FAILED, 1
    if returncode == 2:
        return ExecutionStatus.INTERRUPTED, 2
    if returncode == 3:
        return ExecutionStatus.PYTEST_INTERNAL_ERROR, 3
    if returncode == 4:
        return ExecutionStatus.PYTEST_USAGE_ERROR, 4
    if returncode == 5:
        return ExecutionStatus.NO_TESTS_COLLECTED, 5
    if returncode in (130, -2):
        return ExecutionStatus.INTERRUPTED, 130
    if returncode == 124:
        return ExecutionStatus.TIMEOUT, 124
    if returncode in (143, -15, -9):
        return ExecutionStatus.CANCELLED, 143
    return ExecutionStatus.UNKNOWN_SUBPROCESS_FAILURE, returncode


@dataclass(frozen=True)
class ExecutionResult:
    """Structured result of test collection or execution."""

    status: ExecutionStatus
    returncode: int | None = None
    executed: bool = False
    collect_only: bool = False
    test_count: int = 0
    log_file: str | None = None
    summary: str | None = None
