"""Unit tests for TestTransaction state restoration and exception handling using an injected fake adapter."""
from collections import deque
from copy import deepcopy
import pytest
from modootest.adapter.v19.transaction import Odoo19Adapter, validate_filestore_checklist
from modootest.isolation.transaction import TestTransaction


class FakeCallback:
    def __init__(self):
        self._funcs = deque([lambda: None])
        self.data = {"key": "val"}


class FakeCursor:
    def __init__(self, adapter=None):
        self.adapter = adapter
        self.precommit = FakeCallback()
        self.postcommit = FakeCallback()
        self.prerollback = FakeCallback()
        self.postrollback = FakeCallback()
        self.closed = False
        self.rollback_called_before_close = False
        self.callback_restored_before_close = False

    def commit(self):
        pass

    def rollback(self):
        self.rollback_called_before_close = True

    def close(self):
        if not self.closed:
            if len(self.precommit._funcs) == 1 and "new_key" not in self.precommit.data:
                self.callback_restored_before_close = True
            self.rollback()
            self.closed = True


class FakeAdapter:
    def __init__(self):
        self.cursor = None
        self.patched = False
        self.unpatched = False
        self.savepoint_rolled_back = False
        self.cleared_caches = False
        self.setup_fail_at = None
        self.cleanup_fail_at = None
        self.teardown_order = []

    def acquire_cursor(self, target):
        if self.setup_fail_at == "acquire_cursor":
            raise RuntimeError("Setup cursor failure")
        self.cursor = FakeCursor(self)
        return self.cursor

    def patch_cursor(self, cr):
        if self.setup_fail_at == "patch_cursor":
            raise RuntimeError("Setup patch failure")
        self.patched = True

    def unpatch_cursor(self, cr):
        self.teardown_order.append("unpatch_cursor")
        if self.cleanup_fail_at == "unpatch_cursor":
            raise RuntimeError("Cleanup unpatch failure")
        self.unpatched = True

    def create_environment(self, cr):
        if self.setup_fail_at == "create_environment":
            raise RuntimeError("Setup env failure")

        class FakeEnv:
            def __init__(self):
                self.transaction = type("Tx", (), {"envs": set()})()

        env = FakeEnv()
        env.transaction.envs.add(env)
        return env

    def snapshot_callbacks(self, cr):
        return [
            (cb, deque(cb._funcs), deepcopy(cb.data))
            for cb in [cr.precommit, cr.postcommit, cr.prerollback, cr.postrollback]
        ]

    def restore_callbacks(self, snapshots):
        self.teardown_order.append("restore_callbacks")
        if self.cleanup_fail_at == "restore_callbacks":
            raise RuntimeError("Cleanup restore callbacks failure")
        for cb, funcs, data in snapshots:
            cb._funcs = funcs
            cb.data = data

    def snapshot_environments(self, env):
        return list(env.transaction.envs)

    def flush_all(self, env):
        if self.setup_fail_at == "flush_all":
            raise RuntimeError("Setup flush failure")

    def create_savepoint(self, cr):
        if self.setup_fail_at == "create_savepoint":
            raise RuntimeError("Setup savepoint failure")
        return "fake_savepoint"

    def rollback_savepoint(self, savepoint):
        self.teardown_order.append("rollback_savepoint")
        if self.cleanup_fail_at == "rollback_savepoint":
            raise RuntimeError("Cleanup savepoint failure")
        self.savepoint_rolled_back = True

    def clear_orm_and_restore_envs(self, env, snapshot_envs):
        self.teardown_order.append("clear_orm_and_restore_envs")
        if self.cleanup_fail_at == "clear_orm_and_restore_envs":
            raise RuntimeError("Cleanup clear_orm failure")

    def clear_caches(self, target):
        self.teardown_order.append("clear_caches")
        if self.cleanup_fail_at == "clear_caches":
            raise RuntimeError("Cleanup clear_caches failure")
        self.cleared_caches = True

    def close_cursor(self, cr):
        self.teardown_order.append("close_cursor")
        cr.close()

    def filestore_cleanup(self, cr, target):
        self.teardown_order.append("filestore_cleanup")
        if not getattr(cr, "closed", False):
            raise RuntimeError("Refusing filestore GC because cursor remains open")
        if self.cleanup_fail_at == "filestore_cleanup":
            raise RuntimeError("Cleanup filestore failure")


def test_callback_restoration_before_close():
    """Verify callback deep restoration happens BEFORE cursor close (which calls rollback)."""
    adapter = FakeAdapter()
    with TestTransaction("fake_target", adapter=adapter) as tx:
        tx.cr.precommit._funcs.append(lambda: "mutated")
        tx.cr.precommit.data["new_key"] = "mutated_val"

    assert adapter.cursor.closed is True
    assert adapter.cursor.callback_restored_before_close is True
    assert len(adapter.cursor.precommit._funcs) == 1
    assert "new_key" not in adapter.cursor.precommit.data


def test_teardown_execution_order():
    """Verify teardown steps execute in the exact order required."""
    adapter = FakeAdapter()
    with TestTransaction("fake_target", adapter=adapter):
        pass

    assert adapter.teardown_order == [
        "rollback_savepoint",
        "restore_callbacks",
        "clear_caches",
        "clear_orm_and_restore_envs",
        "unpatch_cursor",
        "close_cursor",
        "filestore_cleanup",
    ]



def test_one_shot_reentry_rejected():
    """Verify entering TestTransaction a second time raises RuntimeError."""
    adapter = FakeAdapter()
    tx = TestTransaction("fake_target", adapter=adapter)
    with tx:
        with pytest.raises(RuntimeError, match="one-shot and cannot be re-entered"):
            with tx:
                pass

    with pytest.raises(RuntimeError, match="one-shot and cannot be re-entered"):
        with tx:
            pass


def test_reentry_after_body_failure_rejected():
    """Verify entering TestTransaction after a body failure is rejected."""
    adapter = FakeAdapter()
    tx = TestTransaction("fake_target", adapter=adapter)
    with pytest.raises(ValueError, match="Body failure"):
        with tx:
            raise ValueError("Body failure")

    with pytest.raises(RuntimeError, match="one-shot and cannot be re-entered"):
        with tx:
            pass


def test_failed_setup_resource_release():
    """Verify resources acquired during setup are released if setup fails halfway."""
    adapter = FakeAdapter()
    adapter.setup_fail_at = "create_savepoint"

    with pytest.raises(RuntimeError, match="Setup savepoint failure"):
        with TestTransaction("fake_target", adapter=adapter):
            pass

    assert adapter.cursor.closed is True


def test_setup_and_cleanup_failures_aggregated():
    """Verify setup failure combined with cleanup failure produces BaseExceptionGroup."""
    adapter = FakeAdapter()
    adapter.setup_fail_at = "create_savepoint"
    adapter.cleanup_fail_at = "clear_caches"

    with pytest.raises(BaseExceptionGroup) as exc_info:
        with TestTransaction("fake_target", adapter=adapter):
            pass

    group = exc_info.value
    assert "Transaction execution and cleanup failed" in str(group)
    excs = group.exceptions
    assert any(isinstance(e, RuntimeError) and "Setup savepoint failure" in str(e) for e in excs)
    assert any(isinstance(e, RuntimeError) and "Cleanup clear_caches failure" in str(e) for e in excs)


def test_failed_cleanup_attempts_remaining_steps():
    """Verify if one cleanup action fails, all remaining cleanup actions are attempted."""
    adapter = FakeAdapter()
    adapter.cleanup_fail_at = "clear_caches"

    with pytest.raises(RuntimeError, match="Cleanup clear_caches failure"):
        with TestTransaction("fake_target", adapter=adapter):
            pass

    assert adapter.savepoint_rolled_back is True
    assert adapter.unpatched is True
    assert adapter.cursor.closed is True


def test_grouped_body_and_cleanup_failures():
    """Verify body exception and cleanup exception are aggregated with BaseExceptionGroup."""
    adapter = FakeAdapter()
    adapter.cleanup_fail_at = "clear_caches"

    with pytest.raises(BaseExceptionGroup) as exc_info:
        with TestTransaction("fake_target", adapter=adapter):
            raise ValueError("Test body failure")

    group = exc_info.value
    assert "Transaction execution and cleanup failed" in str(group)
    excs = group.exceptions
    assert any(isinstance(e, ValueError) and "Test body failure" in str(e) for e in excs)
    assert any(isinstance(e, RuntimeError) and "Cleanup clear_caches failure" in str(e) for e in excs)


def test_cancellation_base_exception_cleanup():
    """Verify cancellation-style BaseException (e.g. KeyboardInterrupt) cleans up fully."""
    adapter = FakeAdapter()
    with pytest.raises(KeyboardInterrupt):
        with TestTransaction("fake_target", adapter=adapter):
            raise KeyboardInterrupt("Simulated SIGINT")

    assert adapter.cursor.closed is True
    assert adapter.savepoint_rolled_back is True
    assert adapter.unpatched is True


def test_adapter_clear_orm_and_restore_envs():
    """Verify Odoo19Adapter.clear_orm_and_restore_envs clears envs, restores membership, and handles failures."""
    class FakeTransaction:
        def __init__(self):
            self.envs = set()

    class FakeEnv:
        def __init__(self, tx, fail_clear=False):
            self.transaction = tx
            self.fail_clear = fail_clear
            self.clear_count = 0

        def clear(self):
            self.clear_count += 1
            if self.fail_clear:
                raise RuntimeError("Env clear failed")

    adapter = Odoo19Adapter()
    tx = FakeTransaction()

    baseline_env = FakeEnv(tx, fail_clear=True)
    derived_env = FakeEnv(tx)

    tx.envs.add(baseline_env)
    snapshot = adapter.snapshot_environments(baseline_env)

    # Test code removes baseline_env and adds derived_env
    tx.envs.remove(baseline_env)
    tx.envs.add(derived_env)

    # Calling clear_orm_and_restore_envs should attempt clearing both envs,
    # restore baseline_env to tx.envs even though baseline_env.clear() fails,
    # and raise the aggregated exception.
    with pytest.raises(RuntimeError, match="Env clear failed"):
        adapter.clear_orm_and_restore_envs(derived_env, snapshot)

    assert baseline_env in tx.envs
    assert derived_env not in tx.envs
    assert baseline_env.clear_count == 1
    assert derived_env.clear_count == 1


def test_adapter_clear_orm_and_restore_envs_shares_transaction():
    """Sharing a transaction must not skip an environment's property cleanup."""
    class FakeTransaction:
        def __init__(self):
            self.envs = set()

    class FakeEnv:
        def __init__(self, tx):
            self.transaction = tx
            self.clear_count = 0

        def clear(self):
            self.clear_count += 1

    adapter = Odoo19Adapter()
    tx = FakeTransaction()
    env1 = FakeEnv(tx)
    env2 = FakeEnv(tx)

    tx.envs.add(env1)
    tx.envs.add(env2)

    adapter.clear_orm_and_restore_envs(env1, [env1])

    assert env1.clear_count == 1
    assert env2.clear_count == 1
    assert env1 in tx.envs
    assert env2 not in tx.envs


def test_adapter_patch_and_unpatch_cursor():
    """Verify Odoo19Adapter.patch_cursor and unpatch_cursor store originals on adapter and restore instance namespace."""
    adapter = Odoo19Adapter()
    cr = FakeCursor()

    assert "commit" not in cr.__dict__
    assert "rollback" not in cr.__dict__
    assert "close" not in cr.__dict__

    adapter.patch_cursor(cr)

    assert "commit" in cr.__dict__
    assert "rollback" in cr.__dict__
    assert "close" in cr.__dict__
    assert id(cr) in adapter._patches

    # Attempting forbidden operations raises RuntimeError
    with pytest.raises(RuntimeError, match="blocked by modootest TestTransaction guard"):
        cr.commit()

    adapter.unpatch_cursor(cr)

    # Instance namespace is restored (attributes removed from cr.__dict__)
    assert "commit" not in cr.__dict__
    assert "rollback" not in cr.__dict__
    assert "close" not in cr.__dict__
    assert id(cr) not in adapter._patches

    # Calling close_cursor now invokes real close() which calls real rollback()
    adapter.close_cursor(cr)
    assert cr.closed is True
    assert cr.rollback_called_before_close is True


def test_adapter_patch_cursor_partial_failure():
    """Verify partial failure during patch_cursor reverts already patched attributes."""
    class FailingCursor(FakeCursor):
        def __setattr__(self, name, value):
            if name == "rollback":
                raise RuntimeError("Simulated patch failure")
            super().__setattr__(name, value)

    cr = FailingCursor()
    adapter = Odoo19Adapter()
    with pytest.raises(RuntimeError, match="Simulated patch failure"):
        adapter.patch_cursor(cr)

    assert "commit" not in cr.__dict__
    assert id(cr) not in adapter._patches
    adapter.close_cursor(cr)
    assert cr.closed and cr.rollback_called_before_close


def test_actual_adapter_restores_nested_callback_data():
    adapter = Odoo19Adapter()
    cr = FakeCursor()
    callbacks = [cr.precommit, cr.postcommit, cr.prerollback, cr.postrollback]
    for cb in callbacks:
        cb.data = {"nested": ["baseline"]}
    original_funcs = [tuple(cb._funcs) for cb in callbacks]
    snapshots = adapter.snapshot_callbacks(cr)
    for cb in callbacks:
        cb.data["nested"].append("mutation")
        cb._funcs.clear()
    adapter.restore_callbacks(snapshots)
    for cb, funcs in zip(callbacks, original_funcs):
        assert tuple(cb._funcs) == funcs
        assert cb.data == {"nested": ["baseline"]}


def test_filestore_cleanup_refuses_when_cursor_unclosed():
    """Verify filestore_cleanup refuses destructive GC when managed cursor is open."""
    adapter = FakeAdapter()

    class NonClosingAdapter(FakeAdapter):
        def close_cursor(self, cr):
            self.teardown_order.append("close_cursor")
            # Intentionally fail to close cursor or raise exception
            raise RuntimeError("Failed to close cursor")

    adapter = NonClosingAdapter()
    with pytest.raises(BaseExceptionGroup) as exc_info:
        with TestTransaction("fake_target", adapter=adapter):
            pass

    group = exc_info.value
    excs = group.exceptions
    assert any(isinstance(e, RuntimeError) and "Failed to close cursor" in str(e) for e in excs)
    assert any(
        isinstance(e, RuntimeError) and "Refusing filestore GC because cursor remains open" in str(e)
        for e in excs
    )


def test_filestore_cleanup_failure_propagated_and_aggregated():
    """Verify filestore cleanup failure propagates as a teardown error and aggregates with body error."""
    adapter = FakeAdapter()
    adapter.cleanup_fail_at = "filestore_cleanup"

    with pytest.raises(BaseExceptionGroup) as exc_info:
        with TestTransaction("fake_target", adapter=adapter):
            raise ValueError("Body error")

    group = exc_info.value
    excs = group.exceptions
    assert any(isinstance(e, ValueError) and "Body error" in str(e) for e in excs)
    assert any(isinstance(e, RuntimeError) and "Cleanup filestore failure" in str(e) for e in excs)


def test_validate_filestore_checklist_valid(tmp_path):
    filestore = tmp_path / "filestore"
    checklist = filestore / "checklist"
    shard = "ab"
    filename = "ab12345678901234567890123456789012345678"
    entry_dir = checklist / shard
    entry_dir.mkdir(parents=True)
    entry_file = entry_dir / filename
    entry_file.write_bytes(b"")

    res = validate_filestore_checklist(str(filestore), str(checklist))
    rel_key = f"{shard}/{filename}"
    assert rel_key in res
    assert res[rel_key] == str(entry_file)


def test_validate_filestore_checklist_symlink_refused(tmp_path):
    import os

    filestore = tmp_path / "filestore"
    filestore.mkdir()
    checklist = filestore / "checklist"
    target_file = tmp_path / "target.txt"
    target_file.write_bytes(b"data")

    # 1. Symlink checklist root
    os.symlink(target_file, checklist)
    with pytest.raises(ValueError, match="Checklist root is a symlink"):
        validate_filestore_checklist(str(filestore), str(checklist))
    os.unlink(checklist)

    # 2. Symlink entry file
    entry_dir = checklist / "ab"
    entry_dir.mkdir(parents=True)
    entry_link = entry_dir / "ab12345678901234567890123456789012345678"
    os.symlink(target_file, entry_link)
    with pytest.raises(ValueError, match="Checklist entry is a symlink"):
        validate_filestore_checklist(str(filestore), str(checklist))


def test_validate_filestore_checklist_invalid_formats(tmp_path):
    filestore = tmp_path / "filestore"
    checklist = filestore / "checklist"

    # Shard mismatch
    entry_dir = checklist / "ab"
    entry_dir.mkdir(parents=True)
    bad_file = entry_dir / "cd12345678901234567890123456789012345678"
    bad_file.write_bytes(b"")
    with pytest.raises(ValueError, match="Filename prefix mismatch with shard"):
        validate_filestore_checklist(str(filestore), str(checklist))
    bad_file.unlink()

    # Invalid filename length / characters
    bad_name_file = entry_dir / "ab_invalid_name"
    bad_name_file.write_bytes(b"")
    with pytest.raises(ValueError, match="Invalid filename format"):
        validate_filestore_checklist(str(filestore), str(checklist))


@pytest.mark.parametrize("close_fails", [False, True])
def test_filestore_cleanup_closes_cursor_on_failure(tmp_path, monkeypatch, close_fails):
    """Verify filestore_cleanup closes its fresh registry GC cursor even if validation or core GC raises."""
    import sys
    import os

    filestore = tmp_path / "filestore"
    checklist = filestore / "checklist"
    entry_dir = checklist / "ab"
    entry_dir.mkdir(parents=True)
    target_file = tmp_path / "target.txt"
    target_file.write_bytes(b"")
    os.symlink(target_file, entry_dir / "ab12345678901234567890123456789012345678")

    closed_cr = FakeCursor()
    closed_cr.closed = True

    gc_cr = FakeCursor()
    gc_cr.execute = lambda *args: None
    if close_fails:
        def failing_close():
            gc_cr.closed = True
            raise RuntimeError("GC cursor release failed")
        gc_cr.close = failing_close

    class FakeRegistry:
        def cursor(self):
            return gc_cr

    adapter = Odoo19Adapter()

    class FakeAttachmentModel:
        def _storage(self):
            return "file"

        def _filestore(self):
            return str(filestore)

        def _full_path(self, path):
            if path == "checklist":
                return str(checklist)
            return str(filestore / path)

    class FakeEnv(dict):
        def __getitem__(self, key):
            if key == "ir.attachment":
                return FakeAttachmentModel()
            return super().__getitem__(key)

    from types import SimpleNamespace
    fake_api = SimpleNamespace(Environment=lambda cr, uid, ctx: FakeEnv(), SUPERUSER_ID=1)
    monkeypatch.setitem(sys.modules, "odoo", type("FakeOdoo", (), {"api": fake_api})())
    monkeypatch.setitem(
        sys.modules,
        "odoo.tools",
        type("FakeOdooTools", (), {"split_every": lambda n, d: [d]})(),
    )

    if close_fails:
        with pytest.raises(BaseExceptionGroup) as caught:
            adapter.filestore_cleanup(closed_cr, FakeRegistry())
        assert isinstance(caught.value.exceptions[0], ValueError)
        assert "Checklist entry is a symlink" in str(caught.value.exceptions[0])
        assert "GC cursor release failed" in str(caught.value.exceptions[1])
    else:
        with pytest.raises(ValueError, match="Checklist entry is a symlink"):
            adapter.filestore_cleanup(closed_cr, FakeRegistry())

    assert gc_cr.closed is True, "GC cursor must be closed in finally block upon validation failure"


@pytest.mark.parametrize("link_kind", ["checklist", "shard", "blob"])
def test_dangling_filestore_links_are_rejected(tmp_path, link_kind):
    filestore = tmp_path / "filestore"
    checklist = filestore / "checklist"
    filestore.mkdir()
    name = "ab" + "1" * 38
    if link_kind == "checklist":
        checklist.symlink_to(tmp_path / "missing", target_is_directory=True)
    else:
        (checklist / "ab").mkdir(parents=True)
        (checklist / "ab" / name).touch()
        if link_kind == "shard":
            (filestore / "ab").symlink_to(tmp_path / "missing", target_is_directory=True)
        else:
            (filestore / "ab").mkdir()
            (filestore / "ab" / name).symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="symlink"):
        validate_filestore_checklist(str(filestore), str(checklist))


def test_real_adapter_refuses_gc_before_opening_cursor():
    from types import SimpleNamespace

    class Registry:
        def cursor(self):
            pytest.fail("Must not acquire GC cursor while managed cursor is open")

    with pytest.raises(RuntimeError, match="managed transaction cursor remains open"):
        Odoo19Adapter().filestore_cleanup(SimpleNamespace(closed=False), Registry())
