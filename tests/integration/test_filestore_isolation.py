"""Integration tests for attachment filestore correctness, cleanup, seed preservation, and failure injection."""
import os
import uuid
import pytest
from modootest.isolation.transaction import TestTransaction


def test_unique_binary_attachment_filestore_cleanup(odoo_registry):
    """
    Verify real unique binary attachment creation (octet-stream),
    store_fname existence before tx exit, and complete row & file absence after exit.
    Tests both normal tx exit and body error variants.
    """
    raw_content = f"unique_filestore_blob_{uuid.uuid4().hex}".encode("utf-8")
    store_fname = None
    attachment_id = None
    full_file_path = None

    # 1. Normal tx exit
    with TestTransaction(odoo_registry) as tx:
        attachment = tx.env["ir.attachment"].create(
            {
                "name": "test_unique_attachment.bin",
                "raw": raw_content,
                "mimetype": "application/octet-stream",
            }
        )
        tx.env.flush_all()
        attachment_id = attachment.id
        store_fname = attachment.store_fname
        assert store_fname, "store_fname must be generated for file storage attachment"
        full_file_path = tx.env["ir.attachment"]._full_path(store_fname)
        assert os.path.isfile(full_file_path), "Filestore blob file must exist before tx exit"
        assert open(full_file_path, "rb").read() == raw_content

    # After exit: verify row and file are absent
    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute("SELECT id FROM ir_attachment WHERE id = %s", (attachment_id,))
        assert fresh_cr.fetchone() is None, "Attachment SQL row must be absent after tx exit"

    assert not os.path.exists(full_file_path), "Filestore blob file must be absent after tx exit"

    # 2. Body exception variant
    raw_content_err = f"unique_filestore_blob_err_{uuid.uuid4().hex}".encode("utf-8")
    store_fname_err = None
    attachment_id_err = None
    full_file_path_err = None

    with pytest.raises(ValueError, match="Intentional body error"):
        with TestTransaction(odoo_registry) as tx_err:
            attachment_err = tx_err.env["ir.attachment"].create(
                {
                    "name": "test_unique_attachment_err.bin",
                    "raw": raw_content_err,
                    "mimetype": "application/octet-stream",
                }
            )
            tx_err.env.flush_all()
            attachment_id_err = attachment_err.id
            store_fname_err = attachment_err.store_fname
            full_file_path_err = tx_err.env["ir.attachment"]._full_path(store_fname_err)
            assert os.path.isfile(full_file_path_err)
            raise ValueError("Intentional body error")

    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute("SELECT id FROM ir_attachment WHERE id = %s", (attachment_id_err,))
        assert fresh_cr.fetchone() is None

    assert not os.path.exists(full_file_path_err)


def test_native_odoo_env_fixture_teardown_path(pytester, pytestconfig, monkeypatch):
    """
    Verify native odoo_env fixture teardown path using observer fixture/hook after tx closes.
    Includes a child test case with injected blob deletion failure, proving teardown ERROR in pytest,
    followed by restoration of patch and successful retry cleanup.
    """
    project_root = pytestconfig.rootpath
    raw_config = pytestconfig.getoption("--modootest-config")
    assert raw_config, "--modootest-config option was not provided"

    abs_src = str((project_root / "src").resolve())
    raw_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath_entries = [abs_src]
    if raw_pythonpath:
        for entry in raw_pythonpath.split(os.pathsep):
            if entry and entry not in pythonpath_entries:
                pythonpath_entries.append(entry)

    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(pythonpath_entries))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    pytester.makeconftest(
        """
import os
import pytest
from modootest.adapter.v19.transaction import Odoo19Adapter

_observed_attachments = []
_injected_fail_path = []

@pytest.fixture
def attachment_observer():
    return _observed_attachments

@pytest.fixture
def injected_fail_path():
    return _injected_fail_path

@pytest.fixture(autouse=True)
def _observe_filestore_teardown(monkeypatch, odoo_registry):
    recorded_records = []
    orig_unlink = os.unlink

    def selective_unlink(path, *args, **kwargs):
        if _injected_fail_path and os.path.abspath(path) == os.path.abspath(_injected_fail_path[0]):
            raise OSError("Injected teardown unlink failure")
        return orig_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", selective_unlink)

    orig_filestore_cleanup = Odoo19Adapter.filestore_cleanup

    def delegating_filestore_cleanup(adapter_self, cr, registry):
        orig_filestore_cleanup(adapter_self, cr, registry)
        for rec in recorded_records:
            att_id = rec["id"]
            file_path = rec["file_path"]
            with odoo_registry.cursor() as fresh_cr:
                fresh_cr.execute("SELECT id FROM ir_attachment WHERE id = %s", (att_id,))
                assert fresh_cr.fetchone() is None, f"Attachment row {att_id} must be absent"
            assert not os.path.exists(file_path), f"Filestore file {file_path} must be absent"
            _observed_attachments.append(att_id)

    monkeypatch.setattr(Odoo19Adapter, "filestore_cleanup", delegating_filestore_cleanup)
    yield recorded_records
"""
    )

    pytester.makepyfile(
        test_child="""
import os
import uuid
import pytest
from modootest.adapter.v19.transaction import Odoo19Adapter

def test_native_fixture_attachment_cleanup(_observe_filestore_teardown, odoo_env):
    raw_bytes = f"native_fixture_blob_{uuid.uuid4().hex}".encode("utf-8")
    attachment = odoo_env["ir.attachment"].create(
        {
            "name": "fixture_test.bin",
            "raw": raw_bytes,
            "mimetype": "application/octet-stream",
        }
    )
    odoo_env.flush_all()
    file_path = odoo_env["ir.attachment"]._full_path(attachment.store_fname)
    assert os.path.isfile(file_path)
    _observe_filestore_teardown.append({"id": attachment.id, "file_path": file_path})

def test_native_fixture_attachment_cleanup_failure(injected_fail_path, odoo_env):
    raw_bytes = f"native_fixture_fail_blob_{uuid.uuid4().hex}".encode("utf-8")
    attachment = odoo_env["ir.attachment"].create(
        {
            "name": "fixture_fail_test.bin",
            "raw": raw_bytes,
            "mimetype": "application/octet-stream",
        }
    )
    odoo_env.flush_all()
    file_path = odoo_env["ir.attachment"]._full_path(attachment.store_fname)
    assert os.path.isfile(file_path)
    injected_fail_path.append(file_path)

def test_verify_observation_and_retry_cleanup(attachment_observer, injected_fail_path, odoo_registry):
    assert len(attachment_observer) == 1, "Observer must have recorded 1 successful filestore teardown"
    assert len(injected_fail_path) == 1
    failed_path = injected_fail_path.pop()
    assert os.path.exists(failed_path), "Failed path must still exist on disk"
    adapter = Odoo19Adapter(odoo_registry)
    with odoo_registry.cursor() as closed_cr:
        pass
    adapter.filestore_cleanup(closed_cr, odoo_registry)
    assert not os.path.exists(failed_path), "Failed path must be cleaned up on retry"
"""
    )

    result = pytester.runpytest_subprocess(
        "-p",
        "no:modootest",
        "-p",
        "modootest.pytest_plugin.plugin",
        f"--modootest-config={raw_config}",
    )

    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines(["*Filestore GC failed to remove orphan files*"])


def test_seed_attachment_referenced_bytes_preserved(odoo_registry):
    """
    Dedicated temporary committed attachment seed via framework-independent cursor.
    Within test tx, update seed to distinct content, unlink seed, and recreate attachment with original seed content.
    Rollback must preserve committed seed row and original bytes unchanged,
    while removing any newly created unreferenced replacement/updated bytes.
    Seed cleanup try/finally covers acquisition and cleanup unconditionally.
    """
    seed_content = f"seed_blob_{uuid.uuid4().hex}".encode("utf-8")
    updated_content = f"seed_updated_blob_{uuid.uuid4().hex}".encode("utf-8")
    seed_name = f"seed_{uuid.uuid4().hex[:8]}.bin"

    seed_id = None
    seed_store_fname = None
    seed_file_path = None
    updated_file_path = None

    try:
        # Step 1: Create seed via framework-independent cursor and commit
        with odoo_registry.cursor() as seed_cr:
            from odoo import api
            env = api.Environment(seed_cr, api.SUPERUSER_ID, {})
            seed_att = env["ir.attachment"].create(
                {
                    "name": seed_name,
                    "raw": seed_content,
                    "mimetype": "application/octet-stream",
                }
            )
            env.flush_all()
            seed_id = seed_att.id
            seed_store_fname = seed_att.store_fname
            seed_file_path = env["ir.attachment"]._full_path(seed_store_fname)
            seed_cr.commit()

        assert os.path.isfile(seed_file_path)

        # Step 2: Inside TestTransaction:
        # a) Update seed content to distinct content (creates new blob, marks old for GC)
        # b) Unlink seed (marks updated blob for GC)
        # c) Recreate attachment with original seed content (uses original shared filename)
        new_att_id = None
        with TestTransaction(odoo_registry) as tx:
            seed_in_tx = tx.env["ir.attachment"].browse(seed_id)
            seed_in_tx.write({"raw": updated_content})
            tx.env.flush_all()
            updated_store_fname = seed_in_tx.store_fname
            updated_file_path = tx.env["ir.attachment"]._full_path(updated_store_fname)

            assert os.path.isfile(updated_file_path)
            assert updated_store_fname != seed_store_fname

            seed_in_tx.unlink()
            tx.env.flush_all()

            new_att = tx.env["ir.attachment"].create(
                {
                    "name": "replacement.bin",
                    "raw": seed_content,
                    "mimetype": "application/octet-stream",
                }
            )
            tx.env.flush_all()
            new_att_id = new_att.id
            assert new_att.store_fname == seed_store_fname

        # Step 3: Verify state after rollback and filestore cleanup:
        # Seed row and original file preserved
        with odoo_registry.cursor() as check_cr:
            check_cr.execute("SELECT id, store_fname FROM ir_attachment WHERE id = %s", (seed_id,))
            row = check_cr.fetchone()
            assert row is not None, "Committed seed row must be preserved after tx rollback"
            assert row[1] == seed_store_fname

            check_cr.execute("SELECT id FROM ir_attachment WHERE id = %s", (new_att_id,))
            assert check_cr.fetchone() is None, "Replacement attachment row must be absent"

        assert os.path.isfile(seed_file_path), "Committed seed filestore blob must be preserved"
        assert open(seed_file_path, "rb").read() == seed_content

        # Updated distinct blob must be unlinked/removed
        if updated_file_path:
            assert not os.path.exists(updated_file_path), "Unreferenced updated blob must be removed"

    finally:
        # Unconditional final cleanup of seed
        if seed_id:
            with odoo_registry.cursor() as cleanup_cr:
                from odoo import api
                env = api.Environment(cleanup_cr, api.SUPERUSER_ID, {})
                att = env["ir.attachment"].browse(seed_id)
                if att.exists():
                    att.unlink()
                    env.flush_all()
                    cleanup_cr.commit()
                    env["ir.attachment"]._gc_file_store_unsafe()
                    cleanup_cr.commit()


def test_failure_injection_re_marks_and_raises(odoo_registry, monkeypatch):
    """
    Patch os.unlink for unique test blob only (delegate all other paths).
    Core suppression must lead modootest to raise cleanup error directly (RuntimeError),
    cursor still closed, retry marker remains, SQL row gone.
    Wrap injection in try/finally, restore patch, retry cleanup with retained tx.cr in finally.
    """
    raw_content = f"failure_injection_blob_{uuid.uuid4().hex}".encode("utf-8")
    store_fname = None
    full_file_path = None
    attachment_id = None
    retained_cr = None

    orig_unlink = os.unlink

    def selective_failing_unlink(path, *args, **kwargs):
        if full_file_path and os.path.abspath(path) == os.path.abspath(full_file_path):
            raise OSError("Injected os.unlink failure for target blob")
        return orig_unlink(path, *args, **kwargs)

    try:
        monkeypatch.setattr(os, "unlink", selective_failing_unlink)

        with pytest.raises(RuntimeError, match="Filestore GC failed to remove orphan files"):
            with TestTransaction(odoo_registry) as tx:
                retained_cr = tx.cr
                attachment = tx.env["ir.attachment"].create(
                    {
                        "name": "failure_injection.bin",
                        "raw": raw_content,
                        "mimetype": "application/octet-stream",
                    }
                )
                tx.env.flush_all()
                attachment_id = attachment.id
                store_fname = attachment.store_fname
                full_file_path = tx.env["ir.attachment"]._full_path(store_fname)
                assert os.path.isfile(full_file_path)

        assert retained_cr is not None
        assert retained_cr.closed is True, "Managed transaction cursor must be closed"

        # Verify SQL row is gone
        with odoo_registry.cursor() as fresh_cr:
            fresh_cr.execute("SELECT id FROM ir_attachment WHERE id = %s", (attachment_id,))
            assert fresh_cr.fetchone() is None, "Attachment SQL row must be rolled back / gone"

        # Verify target file still exists on disk
        assert os.path.isfile(full_file_path), "Target file must remain on disk when unlink fails"

        # Verify checklist retry marker exists
        with odoo_registry.cursor() as fresh_cr:
            from odoo import api
            env = api.Environment(fresh_cr, api.SUPERUSER_ID, {})
            checklist_marker = env["ir.attachment"]._full_path(os.path.join("checklist", store_fname))
            assert os.path.isfile(checklist_marker), "Checklist retry marker must be present"

    finally:
        # Restore patch unconditionally
        monkeypatch.undo()

        if retained_cr and store_fname:
            from modootest.adapter.v19.transaction import Odoo19Adapter
            adapter = Odoo19Adapter(odoo_registry)
            # Retry filestore cleanup using actual retained tx.cr
            adapter.filestore_cleanup(retained_cr, odoo_registry)

            # Verify target file is now removed and filestore is clean
            assert not os.path.exists(full_file_path), "File must be removed after retrying filestore_cleanup"

