"""Integration test suite proving legacy Odoo TransactionCase compatibility with modootest."""
import os
from pathlib import Path
import pytest


def test_transactioncase_compatibility_suite(pytester, pytestconfig, monkeypatch):
    """
    Verify legacy Odoo TransactionCase lifecycle alongside native modootest tests,
    proving cursor management, savepoint isolation across test methods, rollback on
    assertion failure, rollback on skip, class cleanup execution, and post-legacy
    native transaction isolation via pytester.runpytest_subprocess.
    """
    project_root = Path(__file__).resolve().parents[2]

    raw_config = pytestconfig.getoption("--modootest-config")
    assert raw_config, "--modootest-config option was not provided"
    config_path = Path(raw_config)
    if not config_path.is_absolute():
        config_path = (project_root / config_path).resolve()

    abs_src = str((project_root / "src").resolve())
    raw_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath_entries = []
    if raw_pythonpath:
        for entry in raw_pythonpath.split(os.pathsep):
            if not entry:
                continue
            p = Path(entry)
            if not p.is_absolute():
                p = (project_root / p).resolve()
            pythonpath_entries.append(str(p))

    if abs_src not in pythonpath_entries:
        pythonpath_entries.insert(0, abs_src)

    pythonpath = os.pathsep.join(pythonpath_entries)
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    pytester.makepyfile(
        child_helper="""
native_before_id = None
native_before_cr = None

shared_partner_id = None
class_cr = None
class_cleanup_ran = 0

m1_id = None
m3_id = None
m5_id = None

subsequent_native_id = None
subsequent_native_cr = None
""",
        legacy_addon_src="""
from odoo.tests import common
import child_helper

class TestLegacyTransaction(common.TransactionCase):
    @classmethod
    def setUpClass(cls):
        def observer():
            assert cls.cr.closed is True, "Class cursor must be closed when class cleanup runs"
            child_helper.class_cleanup_ran += 1

        cls.addClassCleanup(observer)
        super().setUpClass()

        assert child_helper.native_before_cr.closed is True, "Native before cursor must be closed"
        row = cls.env['res.partner'].browse(child_helper.native_before_id).exists()
        assert not row, "Native before record must be absent in legacy class setup"

        cls.shared_partner = cls.env['res.partner'].create({'name': 'Shared Class Partner'})
        child_helper.shared_partner_id = cls.shared_partner.id
        child_helper.class_cr = cls.cr

    def test_01_first_method(self):
        assert self.cr is child_helper.class_cr, "Class cursor must be shared"
        self.shared_partner.write({'name': 'Modified Shared Partner Name'})
        m1 = self.env['res.partner'].create({'name': 'Method 1 Local Partner'})
        child_helper.m1_id = m1.id
        assert self.shared_partner.name == 'Modified Shared Partner Name'

    def test_02_second_method(self):
        assert self.cr is child_helper.class_cr, "Class cursor must be shared"
        assert self.shared_partner.name == 'Shared Class Partner', "Savepoint rollback must restore shared partner"
        m1_row = self.env['res.partner'].browse(child_helper.m1_id).exists()
        assert not m1_row, "Method 1 local record must be absent in method 2"

    def test_03_failing_method(self):
        assert self.cr is child_helper.class_cr, "Class cursor must be shared"
        m3 = self.env['res.partner'].create({'name': 'Failing Method Partner'})
        self.shared_partner.write({'name': 'Modified By Failing Method'})
        child_helper.m3_id = m3.id
        assert False, "Intentional assertion failure in test_03"

    def test_04_verify_failing_rollback(self):
        assert self.cr is child_helper.class_cr, "Class cursor must be shared"
        assert self.shared_partner.name == 'Shared Class Partner', "Failing method modifications must be rolled back"
        m3_row = self.env['res.partner'].browse(child_helper.m3_id).exists()
        assert not m3_row, "Failing method record must be absent after rollback"

    def test_05_skipping_method(self):
        assert self.cr is child_helper.class_cr, "Class cursor must be shared"
        m5 = self.env['res.partner'].create({'name': 'Skipping Method Partner'})
        self.shared_partner.write({'name': 'Modified By Skipping Method'})
        child_helper.m5_id = m5.id
        self.skipTest("Intentional skip in test_05")

    def test_06_verify_skipping_rollback(self):
        assert self.cr is child_helper.class_cr, "Class cursor must be shared"
        assert self.shared_partner.name == 'Shared Class Partner', "Skipped method modifications must be rolled back"
        m5_row = self.env['res.partner'].browse(child_helper.m5_id).exists()
        assert not m5_row, "Skipped method record must be absent after rollback"
""",
        test_child_suite="""
import importlib.util
import sys
from pathlib import Path
import pytest
import child_helper


def test_00_native_before_class(odoo_env):
    partner = odoo_env["res.partner"].create({"name": "Native Before Legacy Partner"})
    child_helper.native_before_id = partner.id
    child_helper.native_before_cr = odoo_env.cr


addon_src_path = Path(__file__).parent / "legacy_addon_src.py"
spec = importlib.util.spec_from_file_location("odoo.addons.fake_addon.tests.test_legacy", addon_src_path)
mod = importlib.util.module_from_spec(spec)
sys.modules["odoo.addons.fake_addon.tests.test_legacy"] = mod
spec.loader.exec_module(mod)

assert mod.TestLegacyTransaction.__module__.startswith("odoo.addons.")
assert mod.TestLegacyTransaction.test_module == "fake_addon"
assert mod.TestLegacyTransaction.test_tags == {"standard", "at_install"}

TestLegacyTransaction = mod.TestLegacyTransaction


def test_07_subsequent_native_after_class(odoo_env, odoo_registry):
    assert child_helper.class_cleanup_ran == 1, "Class cleanup must run exactly once"
    assert child_helper.class_cr.closed is True, "Class cursor must be closed"

    for operation in (odoo_env.cr.commit, odoo_env.cr.rollback, odoo_env.cr.close):
        with pytest.raises(RuntimeError, match="blocked by modootest TestTransaction guard"):
            operation()

    with odoo_registry.cursor() as fresh_cr:
        for rec_id in [
            child_helper.shared_partner_id,
            child_helper.m1_id,
            child_helper.m3_id,
            child_helper.m5_id,
        ]:
            assert rec_id is not None
            fresh_cr.execute("SELECT id FROM res_partner WHERE id = %s", (rec_id,))
            assert fresh_cr.fetchone() is None, f"Record {rec_id} must be absent from DB"

    sub_partner = odoo_env["res.partner"].create({"name": "Subsequent Native Partner"})
    child_helper.subsequent_native_id = sub_partner.id
    child_helper.subsequent_native_cr = odoo_env.cr


def test_08_final_native_verifier(odoo_registry):
    assert child_helper.subsequent_native_cr.closed is True, "Subsequent native cursor must be closed"
    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute("SELECT id FROM res_partner WHERE id = %s", (child_helper.subsequent_native_id,))
        assert fresh_cr.fetchone() is None, "Subsequent native record must be absent after tx exit"
""",
    )

    result = pytester.runpytest_subprocess(
        "-p",
        "no:modootest",
        "-p",
        "modootest.pytest_plugin.plugin",
        f"--modootest-config={config_path}",
        "test_child_suite.py",
    )

    result.assert_outcomes(passed=7, failed=1, skipped=1)
    assert result.ret == 1
    assert "Intentional assertion failure in test_03" in result.stdout.str()


def test_transactioncase_setupclass_failure_recovery(pytester, pytestconfig, monkeypatch):
    """
    Verify legacy TransactionCase setUpClass failure recovery, confirming class cleanup
    releases cursor, rolls back records created before failure, and subsequent native test succeeds.
    """
    project_root = Path(__file__).resolve().parents[2]

    raw_config = pytestconfig.getoption("--modootest-config")
    assert raw_config, "--modootest-config option was not provided"
    config_path = Path(raw_config)
    if not config_path.is_absolute():
        config_path = (project_root / config_path).resolve()

    abs_src = str((project_root / "src").resolve())
    raw_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath_entries = []
    if raw_pythonpath:
        for entry in raw_pythonpath.split(os.pathsep):
            if not entry:
                continue
            p = Path(entry)
            if not p.is_absolute():
                p = (project_root / p).resolve()
            pythonpath_entries.append(str(p))

    if abs_src not in pythonpath_entries:
        pythonpath_entries.insert(0, abs_src)

    pythonpath = os.pathsep.join(pythonpath_entries)
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    pytester.makepyfile(
        child_helper_fail="""
failing_setup_id = None
failing_setup_cr = None
failing_setup_cleanup_ran = 0
""",
        failing_setup_src="""
from odoo.tests import common
import child_helper_fail

class TestFailingSetUpClass(common.TransactionCase):
    @classmethod
    def setUpClass(cls):
        def observer():
            assert cls.cr.closed is True, "Class cursor must be closed during class cleanup"
            child_helper_fail.failing_setup_cleanup_ran += 1

        cls.addClassCleanup(observer)
        super().setUpClass()

        rec = cls.env['res.partner'].create({'name': 'Failing SetUpClass Partner'})
        child_helper_fail.failing_setup_id = rec.id
        child_helper_fail.failing_setup_cr = cls.cr

        raise RuntimeError("Intentional error in setUpClass after write")

    def test_should_not_run(self):
        pass
""",
        test_child_setup_fail="""
import importlib.util
import sys
from pathlib import Path
import child_helper_fail

addon_src_path = Path(__file__).parent / "failing_setup_src.py"
spec = importlib.util.spec_from_file_location("odoo.addons.fail_addon.tests.test_fail", addon_src_path)
mod = importlib.util.module_from_spec(spec)
sys.modules["odoo.addons.fail_addon.tests.test_fail"] = mod
spec.loader.exec_module(mod)

TestFailingSetUpClass = mod.TestFailingSetUpClass


def test_native_after_failed_setup(odoo_env, odoo_registry):
    assert child_helper_fail.failing_setup_cleanup_ran == 1, "Class cleanup must run on setUpClass failure"
    assert child_helper_fail.failing_setup_cr.closed is True, "Class cursor must be closed"

    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute(
            "SELECT id FROM res_partner WHERE id = %s",
            (child_helper_fail.failing_setup_id,),
        )
        assert fresh_cr.fetchone() is None, "Record created before setUpClass failure must be rolled back"

    p = odoo_env["res.partner"].create({"name": "Native After Failed Setup Partner"})
    assert p.id is not None
""",
    )

    result = pytester.runpytest_subprocess(
        "-p",
        "no:modootest",
        "-p",
        "modootest.pytest_plugin.plugin",
        f"--modootest-config={config_path}",
        "test_child_setup_fail.py",
    )

    result.assert_outcomes(passed=1, errors=1)
    assert result.ret == 1
    assert "Intentional error in setUpClass after write" in result.stdout.str()
