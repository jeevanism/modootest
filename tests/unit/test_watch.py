"""Unit tests for watch mode snapshotting, debounce, cancellation, and supervisor lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
import signal
import time
from typing import Sequence
from unittest.mock import MagicMock
import pytest

from modootest.agent.models import ExecutionResult, ExecutionStatus
from modootest.watch.runner import ProcessRunnerProtocol
from modootest.watch.snapshot import detect_changes, take_snapshot
from modootest.watch.supervisor import WatchSupervisor


def test_snapshot_ignores_caches_git_and_symlinks(tmp_path: Path) -> None:
    git_root = tmp_path / "repo"
    git_root.mkdir()
    addons_dir = git_root / "addons"
    addons_dir.mkdir()

    # Regular file
    reg_file = addons_dir / "models.py"
    reg_file.write_text("class Lead: pass", encoding="utf-8")

    # Ignored directories
    (git_root / ".git").mkdir()
    (git_root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (addons_dir / "__pycache__").mkdir()
    (addons_dir / "__pycache__" / "models.cpython-312.pyc").write_text("bytecode", encoding="utf-8")
    (git_root / ".pytest_cache").mkdir()
    (git_root / ".ai-handoff").mkdir()
    (git_root / ".ai-handoff" / "test.log").write_text("log", encoding="utf-8")

    # Ignored files
    (addons_dir / "temp.swp").write_text("swap", encoding="utf-8")
    (addons_dir / "temp.tmp").write_text("tmp", encoding="utf-8")
    (addons_dir / "backup.py~").write_text("backup", encoding="utf-8")

    # Symlink directory and symlink file
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.py").write_text("secret", encoding="utf-8")

    symlink_dir = addons_dir / "sym_dir"
    try:
        symlink_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        pass

    symlink_file = addons_dir / "sym_file.py"
    try:
        symlink_file.symlink_to(outside_dir / "secret.py")
    except OSError:
        pass

    snapshot = take_snapshot(git_root, [addons_dir])

    # Only regular file should be present
    assert "addons/models.py" in snapshot
    assert not any(".git" in k for k in snapshot)
    assert not any("__pycache__" in k for k in snapshot)
    assert not any(".pytest_cache" in k for k in snapshot)
    assert not any(".ai-handoff" in k for k in snapshot)
    assert not any(".swp" in k for k in snapshot)
    assert not any("sym_dir" in k for k in snapshot)
    assert not any("sym_file.py" in k for k in snapshot)


def test_detect_changes_create_modify_delete() -> None:
    old_snap = {
        "a.py": (100, 50),
        "b.py": (100, 60),
        "c.py": (100, 70),
    }
    new_snap = {
        "a.py": (100, 50),  # unchanged
        "b.py": (200, 65),  # modified
        "d.py": (150, 40),  # created
        # c.py deleted
    }
    created, modified, deleted = detect_changes(old_snap, new_snap)
    assert created == ["d.py"]
    assert modified == ["b.py"]
    assert deleted == ["c.py"]


class FakeProcessRunner(ProcessRunnerProtocol):
    """Fake runner with controllable lifecycle for deterministic watch testing."""

    def __init__(self, duration_steps: int = 1, returncode: int = 0) -> None:
        self.duration_steps = duration_steps
        self.current_step = 0
        self.returncode = returncode
        self.was_started = False
        self.was_cancelled = False
        self._cmd: Sequence[str] = ()

    def start(self, cmd: Sequence[str], cwd: Path, log_file: Path, timeout: float) -> None:
        self.was_started = True
        self._cmd = cmd

    def is_running(self) -> bool:
        if not self.was_started:
            return False
        if self.was_cancelled:
            return False
        self.current_step += 1
        return self.current_step <= self.duration_steps

    def poll(self) -> int | None:
        if self.is_running():
            return None
        return self.returncode if not self.was_cancelled else -1

    def wait(self, timeout: float | None = None) -> int:
        self.current_step = self.duration_steps
        return self.poll() or 0

    def cancel(self, grace_period: float = 2.0) -> None:
        self.was_cancelled = True

    def get_result(self, collect_only: bool, test_count: int, log_file: Path) -> ExecutionResult:
        if self.was_cancelled:
            return ExecutionResult(
                status=ExecutionStatus.CANCELLED,
                returncode=143,
                executed=True,
                collect_only=collect_only,
                test_count=test_count,
                log_file=str(log_file),
                summary="Cancelled",
            )
        status = ExecutionStatus.COLLECTION_PASSED if collect_only else ExecutionStatus.EXECUTION_PASSED
        return ExecutionResult(
            status=status,
            returncode=self.returncode,
            executed=True,
            collect_only=collect_only,
            test_count=test_count,
            log_file=str(log_file),
            summary="Success",
        )


def test_watch_debounce_and_events_streaming(tmp_path: Path) -> None:
    git_root = tmp_path / "repo"
    git_root.mkdir()
    addons_dir = git_root / "addons"
    addons_dir.mkdir()
    test_file = addons_dir / "test_crm.py"
    test_file.write_text("def test_one(): pass", encoding="utf-8")

    simulated_time = [100.0]

    def mock_clock() -> float:
        return simulated_time[0]

    def mock_sleep(duration: float) -> None:
        simulated_time[0] += duration

    snapshots_sequence = [
        {"addons/test_crm.py": (100, 10)},  # initial snapshot
        {"addons/test_crm.py": (100, 10)},  # cycle snapshot
        {"addons/test_crm.py": (105, 12)},  # change 1
        {"addons/test_crm.py": (110, 15)},  # burst change 2 (during debounce)
        {"addons/test_crm.py": (110, 15)},  # debounced settle
    ]
    snap_idx = [0]

    def mock_snapshot(repo: Path, paths: Sequence[Path], ignore: set[Path]) -> dict[str, tuple[int, int]]:
        idx = min(snap_idx[0], len(snapshots_sequence) - 1)
        snap_idx[0] += 1
        return snapshots_sequence[idx]

    output_lines: list[str] = []

    def mock_output(line: str) -> None:
        output_lines.append(line.strip())

    created_runners: list[FakeProcessRunner] = []
    modootest_config = tmp_path / "modootest.conf"
    modootest_config.write_text("[options]\n")

    def runner_factory() -> FakeProcessRunner:
        r = FakeProcessRunner(duration_steps=0, returncode=0)
        created_runners.append(r)
        return r

    supervisor = WatchSupervisor(
        git_root=git_root,
        addons_paths=["addons"],
        modootest_config=modootest_config,
        debounce_interval=0.3,
        poll_interval=0.1,
        output_format="json",
        clock_fn=mock_clock,
        sleep_fn=mock_sleep,
        snapshot_fn=mock_snapshot,
        runner_factory=runner_factory,
        output_fn=mock_output,
    )

    # Stop after debounce cycle
    def stopping_sleep(duration: float) -> None:
        mock_sleep(duration)
        if snap_idx[0] >= len(snapshots_sequence):
            supervisor.stop()

    supervisor._sleep_fn = stopping_sleep

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("modootest.watch.supervisor.discover_git_root", lambda p: git_root)
        mp.setattr("modootest.watch.supervisor.check_head_exists", lambda p: None)
        mp.setattr(
            "modootest.watch.supervisor.build_plan",
            lambda **kwargs: MagicMock(git_root=str(git_root), is_complete=True, diagnostics=()),
        )
        mp.setattr(
            "modootest.watch.supervisor.select_impacted_tests",
            lambda **kwargs: MagicMock(
                test_paths=("addons/test_crm.py",),
                is_complete=True,
            ),
        )

        exit_code = supervisor.run()
        assert exit_code == 0

    # Verify streaming events
    events = [json.loads(line) for line in output_lines if line]
    event_names = [e["event"] for e in events]

    assert "watch_started" in event_names
    assert "run_started" in event_names
    assert "change_detected" in event_names
    assert "watch_stopped" in event_names
    assert created_runners
    assert f"--modootest-config={modootest_config}" in created_runners[0]._cmd

    # Verify strictly monotonic sequences: 1, 2, 3...
    seqs = [e["sequence"] for e in events]
    assert seqs == list(range(1, len(events) + 1))


def test_watch_change_during_run_cancels_and_restarts(tmp_path: Path) -> None:
    git_root = tmp_path / "repo"
    git_root.mkdir()
    addons_dir = git_root / "addons"
    addons_dir.mkdir()

    simulated_time = [200.0]

    def mock_clock() -> float:
        return simulated_time[0]

    def mock_sleep(duration: float) -> None:
        simulated_time[0] += duration

    # The runner will stay running for 5 steps unless cancelled
    runners: list[FakeProcessRunner] = []

    def runner_factory() -> FakeProcessRunner:
        r = FakeProcessRunner(duration_steps=5, returncode=0)
        runners.append(r)
        return r

    # Snapshots: change occurs during runner step 1
    snapshots = [
        {"addons/a.py": (100, 10)},
        {"addons/a.py": (100, 10)},
        {"addons/a.py": (200, 20)},  # Change arrives during run!
        {"addons/a.py": (200, 20)},
    ]
    snap_call = [0]

    def mock_snapshot(repo: Path, paths: Sequence[Path], ignore: set[Path]) -> dict[str, tuple[int, int]]:
        idx = min(snap_call[0], len(snapshots) - 1)
        snap_call[0] += 1
        return snapshots[idx]

    output_lines: list[str] = []

    supervisor = WatchSupervisor(
        git_root=git_root,
        addons_paths=["addons"],
        debounce_interval=0.2,
        poll_interval=0.05,
        output_format="json",
        clock_fn=mock_clock,
        sleep_fn=mock_sleep,
        snapshot_fn=mock_snapshot,
        runner_factory=runner_factory,
        output_fn=lambda l: output_lines.append(l.strip()),
    )

    def sleep_and_stop(d: float) -> None:
        mock_sleep(d)
        if len(runners) >= 2:
            supervisor.stop()

    supervisor._sleep_fn = sleep_and_stop

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("modootest.watch.supervisor.discover_git_root", lambda p: git_root)
        mp.setattr("modootest.watch.supervisor.check_head_exists", lambda p: None)
        mp.setattr(
            "modootest.watch.supervisor.build_plan",
            lambda **kwargs: MagicMock(git_root=str(git_root), is_complete=True, diagnostics=()),
        )
        mp.setattr(
            "modootest.watch.supervisor.select_impacted_tests",
            lambda **kwargs: MagicMock(
                test_paths=("addons/test_crm.py",),
                is_complete=True,
            ),
        )

        supervisor.run()

    # The first runner should have been cancelled!
    assert len(runners) >= 2
    assert runners[0].was_cancelled is True

    events = [json.loads(line) for line in output_lines if line]
    event_names = [e["event"] for e in events]
    assert "change_detected" in event_names
    cancelled_index = next(
        index
        for index, event in enumerate(events)
        if event["event"] == "run_completed"
        and event["data"].get("status") == "cancelled"
    )
    change_index = event_names.index("change_detected")
    restarted_index = event_names.index("run_started", change_index + 1)
    assert change_index < cancelled_index < restarted_index
    assert events[cancelled_index]["data"]["executed"] is True
    assert events[cancelled_index]["data"]["returncode"] == 143


def test_watch_1200_restarts_no_stack_growth_or_overlapping_children(tmp_path: Path) -> None:
    """Prove that at least 1,200 simulated change-during-run restarts do not grow the stack or overlap children."""
    import inspect

    git_root = tmp_path / "repo"
    git_root.mkdir()
    addons_dir = git_root / "addons"
    addons_dir.mkdir()

    active_runners: set[FakeProcessRunner] = set()
    total_runners_created = 0
    stack_depths: list[int] = []

    class TrackedRunner(FakeProcessRunner):
        def start(self, cmd: Sequence[str], cwd: Path, log_file: Path, timeout: float) -> None:
            nonlocal total_runners_created
            # Record current call stack depth
            stack_depths.append(len(inspect.stack()))
            # Invariant: No more than 1 active child at any moment!
            assert len(active_runners) == 0, f"Overlapping runners detected! Active: {len(active_runners)}"
            active_runners.add(self)
            total_runners_created += 1
            super().start(cmd, cwd, log_file, timeout)

        def is_running(self) -> bool:
            return not self.was_cancelled

        def cancel(self, grace_period: float = 2.0) -> None:
            active_runners.discard(self)
            super().cancel(grace_period)

        def poll(self) -> int | None:
            return -1 if self.was_cancelled else 0

    restarts_target = 1250
    current_time = [1000.0]

    def mock_clock() -> float:
        return current_time[0]

    def mock_sleep(d: float) -> None:
        current_time[0] += d

    # Alternate snapshots only when runner is active to simulate incoming changes during runs
    call_count = [0]

    def mock_snapshot(repo: Path, paths: Sequence[Path], ignore: set[Path]) -> dict[str, tuple[int, int]]:
        if len(active_runners) > 0:
            call_count[0] += 1
        return {"addons/mod.py": (call_count[0], 100)}

    supervisor = WatchSupervisor(
        git_root=git_root,
        addons_paths=["addons"],
        debounce_interval=0.01,
        poll_interval=0.01,
        output_format="json",
        clock_fn=mock_clock,
        sleep_fn=mock_sleep,
        snapshot_fn=mock_snapshot,
        runner_factory=TrackedRunner,
        output_fn=lambda _: None,
    )

    def stopping_sleep(d: float) -> None:
        mock_sleep(d)
        if total_runners_created >= restarts_target:
            supervisor.stop()

    supervisor._sleep_fn = stopping_sleep

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("modootest.watch.supervisor.discover_git_root", lambda p: git_root)
        mp.setattr("modootest.watch.supervisor.check_head_exists", lambda p: None)
        mp.setattr(
            "modootest.watch.supervisor.build_plan",
            lambda **kwargs: MagicMock(git_root=str(git_root), is_complete=True, diagnostics=()),
        )
        mp.setattr(
            "modootest.watch.supervisor.select_impacted_tests",
            lambda **kwargs: MagicMock(
                test_paths=("addons/test_file.py",),
                is_complete=True,
            ),
        )

        exit_code = supervisor.run()
        assert exit_code == 0

    assert total_runners_created >= restarts_target
    assert len(stack_depths) >= restarts_target
    # Verify stack depth remained constant (no recursion!)
    min_depth = min(stack_depths)
    max_depth = max(stack_depths)
    assert max_depth - min_depth <= 2, f"Stack grew from {min_depth} to {max_depth}"
    # Verify no leaked active runners
    assert len(active_runners) == 0


def test_validate_watch_roots_fail_closed(tmp_path: Path) -> None:
    """Test that validate_watch_roots rejects escaping, missing, symlink, and non-directory roots."""
    from modootest.watch.snapshot import WatchSnapshotError, validate_watch_roots

    git_root = tmp_path / "repo"
    git_root.mkdir()
    (git_root / "addons").mkdir()
    (git_root / "regular_file.txt").write_text("hello", encoding="utf-8")

    # 1. Valid root passes
    valid = validate_watch_roots(git_root, ["addons"])
    assert valid == [git_root / "addons"]

    # 2. Escaping root with .. or absolute path
    with pytest.raises(WatchSnapshotError, match="escapes git repository root"):
        validate_watch_roots(git_root, ["../outside"])
    with pytest.raises(WatchSnapshotError, match="escapes git repository root"):
        validate_watch_roots(git_root, ["/tmp/outside"])

    # 3. Missing root
    with pytest.raises(WatchSnapshotError, match="does not exist"):
        validate_watch_roots(git_root, ["nonexistent_addons"])

    # 4. Non-directory root (e.g. regular file)
    with pytest.raises(WatchSnapshotError, match="not a directory"):
        validate_watch_roots(git_root, ["regular_file.txt"])

    # 5. Symlink root
    outside = tmp_path / "outside_addons"
    outside.mkdir()
    sym_root = git_root / "sym_addons"
    try:
        sym_root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(WatchSnapshotError, match="Unsupported symlink"):
            validate_watch_roots(git_root, ["sym_addons"])
    except OSError:
        pass


def test_watch_same_size_and_mtime_replacement_detected(tmp_path: Path) -> None:
    """Test that same-size and preserved-mtime replacement is reliably detected."""
    import os
    git_root = tmp_path / "repo"
    git_root.mkdir()
    watched = git_root / "addons"
    watched.mkdir()

    target = watched / "same.py"
    target.write_text("aaaa", encoding="utf-8")
    original_st = target.stat()

    before = take_snapshot(git_root, [watched])

    replacement = watched / "replacement.py"
    replacement.write_text("bbbb", encoding="utf-8")
    os.utime(replacement, ns=(original_st.st_atime_ns, original_st.st_mtime_ns))
    os.replace(replacement, target)

    after = take_snapshot(git_root, [watched])
    changes = detect_changes(before, after)
    assert bool(any(changes)) is True
    assert "addons/same.py" in changes[1]  # in modified


def test_watch_real_runner_timeout_and_cancel(tmp_path: Path) -> None:
    """Test that PytestProcessRunner enforces timeout and cancels child process group."""
    import sys
    from modootest.watch.runner import PytestProcessRunner

    runner = PytestProcessRunner()
    log_file = tmp_path / "child.log"
    # Child sleeps for 5 seconds; timeout is 0.05s
    runner.start(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        tmp_path,
        log_file,
        timeout=0.05,
    )
    assert runner.is_running() is True
    # Poll until timeout expires
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline and runner.is_running():
        time.sleep(0.01)

    assert runner.is_running() is False
    res = runner.get_result(collect_only=False, test_count=1, log_file=log_file)
    assert res.status == ExecutionStatus.TIMEOUT
    assert res.executed is True
    assert res.returncode == 124


def test_watch_invalid_pytest_args_exit_before_snapshot_or_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_root = tmp_path / "repo"
    addons = git_root / "addons"
    addons.mkdir(parents=True)
    snapshots = 0
    runners = 0
    output: list[str] = []

    def snapshot(*args: object, **kwargs: object) -> dict[str, tuple[int, int]]:
        nonlocal snapshots
        snapshots += 1
        return {}

    def runner_factory() -> FakeProcessRunner:
        nonlocal runners
        runners += 1
        return FakeProcessRunner()

    monkeypatch.setattr("modootest.watch.supervisor.discover_git_root", lambda path: git_root)
    monkeypatch.setattr("modootest.watch.supervisor.check_head_exists", lambda path: None)

    supervisor = WatchSupervisor(
        git_root=git_root,
        addons_paths=["addons"],
        pytest_args=["--database=forbidden"],
        output_format="json",
        snapshot_fn=snapshot,
        runner_factory=runner_factory,
        output_fn=output.append,
    )

    assert supervisor.run() == 2
    assert snapshots == 0
    assert runners == 0
    assert len(output) == 1
    document = json.loads(output[0])
    assert document["command"] == "watch"
    assert document["diagnostics"][0]["code"] == "invalid_pytest_arguments"


@pytest.mark.parametrize("snapshot_fails", [False, True])
def test_watch_restores_signal_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_fails: bool,
) -> None:
    git_root = tmp_path / "repo"
    addons = git_root / "addons"
    addons.mkdir(parents=True)
    originals = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    current = dict(originals)

    def fake_signal(signum: int, handler: object) -> object:
        previous = current[signum]
        current[signum] = handler
        return previous

    monkeypatch.setattr("modootest.watch.supervisor.discover_git_root", lambda path: git_root)
    monkeypatch.setattr("modootest.watch.supervisor.check_head_exists", lambda path: None)
    monkeypatch.setattr("modootest.watch.supervisor.signal.signal", fake_signal)

    supervisor: WatchSupervisor

    def snapshot(*args: object, **kwargs: object) -> dict[str, tuple[int, int]]:
        if snapshot_fails:
            raise OSError("snapshot unavailable")
        return {}

    def stop_after_first_sleep(duration: float) -> None:
        supervisor.stop()

    supervisor = WatchSupervisor(
        git_root=git_root,
        addons_paths=["addons"],
        snapshot_fn=snapshot,
        sleep_fn=stop_after_first_sleep,
        output_fn=lambda output: None,
    )
    monkeypatch.setattr(
        "modootest.watch.supervisor.build_plan",
        lambda **kwargs: MagicMock(git_root=str(git_root), is_complete=True, diagnostics=()),
    )
    monkeypatch.setattr(
        "modootest.watch.supervisor.select_impacted_tests",
        lambda **kwargs: MagicMock(test_paths=(), is_complete=True),
    )

    expected_exit = 2 if snapshot_fails else 0
    assert supervisor.run() == expected_exit
    assert current == originals


def test_watch_event_redacts_sensitive_data() -> None:
    from modootest.agent.serializers import serialize_watch_event

    event = serialize_watch_event(
        "planning_error",
        1,
        {
            "error": "rejected --db_password=DO_NOT_EXPOSE",
            "auth_token": "DO_NOT_EXPOSE_EITHER",
        },
    )
    encoded = json.dumps(event)
    assert "DO_NOT_EXPOSE" not in encoded
    assert event["data"]["auth_token"] == "[REDACTED]"


def test_watch_shutdown_cancels_run_before_stopped_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_root = tmp_path / "repo"
    addons = git_root / "addons"
    addons.mkdir(parents=True)
    runner = FakeProcessRunner(duration_steps=100)
    output: list[str] = []
    supervisor: WatchSupervisor

    def stop_during_run(duration: float) -> None:
        supervisor.stop()

    monkeypatch.setattr("modootest.watch.supervisor.discover_git_root", lambda path: git_root)
    monkeypatch.setattr("modootest.watch.supervisor.check_head_exists", lambda path: None)
    monkeypatch.setattr(
        "modootest.watch.supervisor.build_plan",
        lambda **kwargs: MagicMock(git_root=str(git_root), is_complete=True, diagnostics=()),
    )
    monkeypatch.setattr(
        "modootest.watch.supervisor.select_impacted_tests",
        lambda **kwargs: MagicMock(test_paths=("addons/test_file.py",), is_complete=True),
    )

    supervisor = WatchSupervisor(
        git_root=git_root,
        addons_paths=["addons"],
        output_format="json",
        sleep_fn=stop_during_run,
        snapshot_fn=lambda *args, **kwargs: {},
        runner_factory=lambda: runner,
        output_fn=output.append,
    )

    assert supervisor.run() == 0
    events = [json.loads(line) for line in output]
    completed = next(event for event in events if event["event"] == "run_completed")
    assert completed["data"]["status"] == "cancelled"
    assert completed["data"]["returncode"] == 143
    assert [event["event"] for event in events][-1] == "watch_stopped"
