"""Odoo 19 transaction adapter implementation."""
from collections import deque
from copy import deepcopy
import os
import re
import stat
import sys

_SHARD_RE = re.compile(r"^[0-9a-f]{2}$")
_FILENAME_RE = re.compile(r"^[0-9a-f]{40}$")


def validate_filestore_checklist(filestore_dir, checklist_dir):
    """Validate complete checklist tree and filestore structure before core GC.

    Enforces trusted store boundaries: no symlink directories/files, strict shard/name format.
    This is a trusted exclusive store, not an adversarial concurrent filesystem.
    """
    def path_mode(path):
        try:
            return os.lstat(path).st_mode
        except FileNotFoundError:
            return None

    if stat.S_ISLNK(path_mode(filestore_dir) or 0):
        raise ValueError(f"Filestore root is a symlink: {filestore_dir}")
    checklist_mode = path_mode(checklist_dir)
    if stat.S_ISLNK(checklist_mode or 0):
        raise ValueError(f"Checklist root is a symlink: {checklist_dir}")
    if checklist_mode is None:
        return {}
    if not stat.S_ISDIR(checklist_mode):
        raise ValueError(f"Checklist root is not a directory: {checklist_dir}")

    checklist = {}

    def _on_walk_error(err):
        raise err

    for dirpath, dirnames, filenames in os.walk(checklist_dir, onerror=_on_walk_error):
        if os.path.islink(dirpath):
            raise ValueError(f"Checklist directory is a symlink: {dirpath}")

        for d in dirnames:
            sub_dir = os.path.join(dirpath, d)
            if os.path.islink(sub_dir):
                raise ValueError(f"Checklist sub-directory is a symlink: {sub_dir}")

        for filename in filenames:
            file_path = os.path.join(dirpath, filename)
            if os.path.islink(file_path):
                raise ValueError(f"Checklist entry is a symlink: {file_path}")
            if not stat.S_ISREG(path_mode(file_path) or 0):
                raise ValueError(f"Checklist entry is not a regular file: {file_path}")

            rel_path = os.path.relpath(file_path, checklist_dir).replace("\\", "/")
            parts = rel_path.split("/")
            if len(parts) != 2:
                raise ValueError(f"Invalid checklist relative path structure: {rel_path}")

            shard, fname_part = parts
            if not _SHARD_RE.fullmatch(shard):
                raise ValueError(f"Invalid shard format in checklist path: {shard}")
            if not _FILENAME_RE.fullmatch(fname_part):
                raise ValueError(f"Invalid filename format in checklist path: {fname_part}")
            if not fname_part.startswith(shard):
                raise ValueError(f"Filename prefix mismatch with shard: {fname_part} vs {shard}")

            blob_shard_dir = os.path.join(filestore_dir, shard)
            if stat.S_ISLNK(path_mode(blob_shard_dir) or 0):
                raise ValueError(f"Filestore shard directory is a symlink: {blob_shard_dir}")

            blob_file = os.path.join(filestore_dir, shard, fname_part)
            if stat.S_ISLNK(path_mode(blob_file) or 0):
                raise ValueError(f"Filestore blob is a symlink: {blob_file}")

            checklist[rel_path] = file_path

    return checklist


def _forbidden_commit(*args, **kwargs):
    raise RuntimeError("Direct commit() call on test cursor is blocked by modootest TestTransaction guard.")


def _forbidden_rollback(*args, **kwargs):
    raise RuntimeError("Direct rollback() call on test cursor is blocked by modootest TestTransaction guard.")


def _forbidden_close(*args, **kwargs):
    raise RuntimeError("Direct close() call on test cursor is blocked by modootest TestTransaction guard.")


class Odoo19Adapter:
    """Supplies Odoo 19 specific primitives for TestTransaction."""

    def __init__(self, registry=None):
        self.registry = registry
        self._patches = {}

    def acquire_cursor(self, registry):
        reg = registry or self.registry
        return reg.cursor()

    def patch_cursor(self, cr):
        attrs = ["commit", "rollback", "close"]
        forbidden_map = {
            "commit": _forbidden_commit,
            "rollback": _forbidden_rollback,
            "close": _forbidden_close,
        }
        patched = []
        self._patches[id(cr)] = patched
        try:
            for attr in attrs:
                had_instance = attr in cr.__dict__
                orig_val = getattr(cr, attr)
                patched.append((attr, had_instance, orig_val))
                setattr(cr, attr, forbidden_map[attr])
        except BaseException as patch_error:
            try:
                self.unpatch_cursor(cr)
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "Cursor patching and restoration failed", [patch_error, cleanup_error]
                ) from None
            raise

    def unpatch_cursor(self, cr):
        patched = self._patches.pop(id(cr), None)
        if not patched:
            return
        errors = []
        for attr, had_instance, orig_val in reversed(patched):
            try:
                if had_instance:
                    setattr(cr, attr, orig_val)
                else:
                    if attr in cr.__dict__:
                        delattr(cr, attr)
            except BaseException as e:
                errors.append(e)
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise BaseExceptionGroup("Failed to unpatch cursor", errors)

    def create_environment(self, cr):
        from odoo import api
        return api.Environment(cr, api.SUPERUSER_ID, {})

    def snapshot_callbacks(self, cr):
        snapshots = []
        for cb in [cr.precommit, cr.postcommit, cr.prerollback, cr.postrollback]:
            snapshots.append((cb, deque(cb._funcs), deepcopy(cb.data)))
        return snapshots

    def restore_callbacks(self, snapshots):
        if not snapshots:
            return
        errors = []
        for cb, funcs, data in snapshots:
            try:
                cb._funcs = funcs
                cb.data = data
            except BaseException as e:
                errors.append(e)
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise BaseExceptionGroup("Failed to restore callbacks", errors)

    def snapshot_environments(self, env):
        if env and hasattr(env, "transaction") and env.transaction:
            return list(env.transaction.envs)
        return []

    def flush_all(self, env):
        if env:
            env.flush_all()

    def create_savepoint(self, cr):
        from odoo.sql_db import Savepoint
        return Savepoint(cr)

    def rollback_savepoint(self, savepoint):
        if savepoint:
            savepoint.close(rollback=True)

    def clear_orm_and_restore_envs(self, env, snapshot_envs):
        if not (env and hasattr(env, "transaction") and env.transaction):
            return

        envs = env.transaction.envs
        # Environment.clear also resets properties on the individual environment.
        # Sharing a transaction does not make baseline environments interchangeable.
        all_envs = list(snapshot_envs)
        seen_envs = {id(e) for e in all_envs}
        for current in list(envs):
            if id(current) not in seen_envs:
                all_envs.append(current)
                seen_envs.add(id(current))

        errors = []

        try:
            for e in all_envs:
                try:
                    e.clear()
                except BaseException as exc:
                    errors.append(exc)
        finally:
            try:
                envs.clear()
                if snapshot_envs:
                    envs.update(snapshot_envs)
            except BaseException as exc:
                errors.append(exc)

        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise BaseExceptionGroup("ORM clear and environment restoration failed", errors)

    def clear_caches(self, registry):
        reg = registry or self.registry
        if reg and hasattr(reg, "clear_all_caches"):
            reg.clear_all_caches()

    def close_cursor(self, cr):
        if cr:
            cr.close()

    def filestore_cleanup(self, cr, registry):
        if not getattr(cr, "closed", False):
            raise RuntimeError(
                "Refusing filestore garbage collection because managed transaction cursor remains open."
            )

        reg = registry or self.registry
        if not reg:
            return

        gc_cr = reg.cursor()
        try:
            # Lock ir_attachment IN SHARE MODE with SET LOCAL lock_timeout = '5s'
            gc_cr.execute("SET LOCAL lock_timeout TO '5s'")
            gc_cr.execute("LOCK ir_attachment IN SHARE MODE")

            from odoo import api
            from odoo.tools import split_every

            env = api.Environment(gc_cr, api.SUPERUSER_ID, {})
            attachment_model = env["ir.attachment"]

            filestore_dir = attachment_model._filestore()
            checklist_dir = attachment_model._full_path("checklist")

            checklist = validate_filestore_checklist(filestore_dir, checklist_dir)
            if not checklist:
                return

            orphan_fnames = []
            for names in split_every(gc_cr.IN_MAX, checklist):
                gc_cr.execute(
                    "SELECT store_fname FROM ir_attachment WHERE store_fname IN %s",
                    [names],
                )
                whitelist = set(row[0] for row in gc_cr.fetchall())
                for fname in names:
                    if fname not in whitelist:
                        orphan_fnames.append(fname)

            # Invoke Odoo's unsafe GC
            attachment_model._gc_file_store_unsafe()

            # Verify that snapshot orphan files are absent using lstat
            remaining_orphans = []
            for fname in orphan_fnames:
                full_path = attachment_model._full_path(fname)
                try:
                    os.lstat(full_path)
                    remaining_orphans.append(fname)
                except FileNotFoundError:
                    pass
                except OSError:
                    remaining_orphans.append(fname)

            if remaining_orphans:
                # Re-mark remaining orphans for GC using core _mark_for_gc
                for fname in remaining_orphans:
                    attachment_model._mark_for_gc(fname)
                raise RuntimeError(
                    f"Filestore GC failed to remove orphan files: {remaining_orphans}"
                )
        finally:
            primary_error = sys.exception()
            try:
                gc_cr.close()
            except BaseException as close_error:
                if primary_error is not None:
                    raise BaseExceptionGroup(
                        "Filestore cleanup and cursor release failed",
                        [primary_error, close_error],
                    ) from None
                raise
