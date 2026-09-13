"""Generic isolation orchestrator for modootest transaction lifecycle."""


class TestTransaction:
    """One-shot transaction owner for single test isolation."""

    __test__ = False

    def __init__(self, target, adapter=None):
        """
        :param target: Registry object or database reference
        :param adapter: Adapter supplying transaction primitives.
                        If None, dynamically loads Odoo 19 adapter.
        """
        self.target = target
        if adapter is None:
            from modootest.adapter.v19.transaction import Odoo19Adapter
            self.adapter = Odoo19Adapter(target)
        else:
            self.adapter = adapter

        self.cr = None
        self.env = None
        self.savepoint = None
        self._state = "UNUSED"
        self._cleanup_stack = []

    def __enter__(self):
        if self._state != "UNUSED":
            raise RuntimeError("TestTransaction is one-shot and cannot be re-entered.")
        self._state = "ACTIVE"

        try:
            # 1. Acquire cursor (cleanup: filestore_cleanup, close_cursor)
            self.cr = self.adapter.acquire_cursor(self.target)
            self._cleanup_stack.append(("filestore_cleanup", (self.cr, self.target)))
            self._cleanup_stack.append(("close_cursor", self.cr))

            # 2. Patch cursor forbidden methods (cleanup: unpatch_cursor)
            self.adapter.patch_cursor(self.cr)
            self._cleanup_stack.append(("unpatch_cursor", self.cr))

            # 3. Create Environment & snapshot envs (cleanup: clear_orm_and_restore_envs)
            self.env = self.adapter.create_environment(self.cr)
            env_snapshot = self.adapter.snapshot_environments(self.env)
            self._cleanup_stack.append(("clear_orm_and_restore_envs", (self.env, env_snapshot)))

            # Register clear_caches
            self._cleanup_stack.append(("clear_caches", self.target))

            # 4. Snapshot callbacks (cleanup: restore_callbacks)
            callback_snapshots = self.adapter.snapshot_callbacks(self.cr)
            self._cleanup_stack.append(("restore_callbacks", callback_snapshots))

            # 5. Flush before savepoint
            self.adapter.flush_all(self.env)

            # 6. Construct Savepoint (cleanup: rollback_savepoint)
            self.savepoint = self.adapter.create_savepoint(self.cr)
            self._cleanup_stack.append(("rollback_savepoint", self.savepoint))

            return self
        except BaseException as setup_exc:
            self._state = "FINISHED"
            self._run_cleanup_and_raise(setup_exc)

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._state = "FINISHED"
        body_exc = exc_val
        self._run_cleanup_and_raise(body_exc)
        return False

    def _run_cleanup_and_raise(self, primary_exc=None):
        cleanup_errors = []

        while self._cleanup_stack:
            action_type, payload = self._cleanup_stack.pop()
            try:
                if action_type == "clear_caches":
                    self.adapter.clear_caches(payload)
                elif action_type == "rollback_savepoint":
                    self.adapter.rollback_savepoint(payload)
                elif action_type == "clear_orm_and_restore_envs":
                    env, snapshot = payload
                    self.adapter.clear_orm_and_restore_envs(env, snapshot)
                elif action_type == "restore_callbacks":
                    self.adapter.restore_callbacks(payload)
                elif action_type == "unpatch_cursor":
                    self.adapter.unpatch_cursor(payload)
                elif action_type == "close_cursor":
                    self.adapter.close_cursor(payload)
                elif action_type == "filestore_cleanup":
                    cr, target = payload
                    self.adapter.filestore_cleanup(cr, target)
            except BaseException as e:
                cleanup_errors.append(e)

        if primary_exc is not None and cleanup_errors:
            all_excs = [primary_exc] + cleanup_errors
            raise BaseExceptionGroup("Transaction execution and cleanup failed", all_excs)
        elif primary_exc is not None:
            raise primary_exc
        elif len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        elif len(cleanup_errors) > 1:
            raise BaseExceptionGroup("Transaction cleanup failed", cleanup_errors)

