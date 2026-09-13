"""Lifecycle contract tests for pytest outcomes, teardown failures, computed fields, and registry caches."""
import os
from pathlib import Path
import uuid
import pytest
from modootest.isolation.transaction import TestTransaction


def test_subprocess_pytest_outcomes_and_teardown_isolation(pytester, pytestconfig, monkeypatch):
    """
    Test pytest outcome handling (skip in body, xfail in body, strict xfail, fixture skip),
    and cleanup-stage failure via pytester.runpytest_subprocess.
    """
    project_root = Path(__file__).resolve().parents[2]

    # Resolve config_path as absolute relative to project_root
    raw_config = pytestconfig.getoption("--modootest-config")
    assert raw_config, "--modootest-config option was not provided"
    config_path = Path(raw_config)
    if not config_path.is_absolute():
        config_path = (project_root / config_path).resolve()

    # Construct absolute PYTHONPATH
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

    # Create child conftest with observer wrapping Odoo19Adapter.close_cursor
    pytester.makeconftest(
        """
import pytest
from modootest.adapter.v19.transaction import Odoo19Adapter

_observed_closes = []

@pytest.fixture
def observed_closes():
    return _observed_closes

@pytest.fixture(autouse=True)
def _observe_tx_isolation(monkeypatch, odoo_registry):
    recorded_ids = []

    orig_close = Odoo19Adapter.close_cursor

    def delegating_close_cursor(adapter_self, cr):
        # 1. Delegate actual close_cursor first
        orig_close(adapter_self, cr)

        # 2. Observer asserts real cursor is closed
        assert cr.closed is True, "Transaction cursor must be closed after transaction exit"

        # 3. Observer asserts row absent with fresh cursor after outcome
        for record_id in recorded_ids:
            with odoo_registry.cursor() as fresh_cr:
                fresh_cr.execute("SELECT id FROM res_partner WHERE id = %s", (record_id,))
                assert fresh_cr.fetchone() is None, f"Record {record_id} must be absent from database"

        # 4. Track successful observation after verifying rows absent and cursor closed
        assert len(recorded_ids) > 0, "Recorded ID list must be nonempty for observed close"
        _observed_closes.append(list(recorded_ids))

    monkeypatch.setattr(Odoo19Adapter, "close_cursor", delegating_close_cursor)
    yield recorded_ids
"""
    )

    # Create child test file exercising skip, xfail, strict xfail, fixture skip, cleanup error, and sentinel
    pytester.makepyfile(
        test_child="""
import pytest

@pytest.fixture
def fixture_writing_and_skipping(_observe_tx_isolation, odoo_env):
    partner = odoo_env["res.partner"].create({"name": "Fixture Writing Skip Partner"})
    _observe_tx_isolation.append(partner.id)
    pytest.skip("Skipping inside dependent fixture setup after record write")

def test_skip_in_body(_observe_tx_isolation, odoo_env):
    partner = odoo_env["res.partner"].create({"name": "Body Skip Partner"})
    _observe_tx_isolation.append(partner.id)
    pytest.skip("Skipping inside test body after record write")

def test_xfail_in_body(_observe_tx_isolation, odoo_env):
    partner = odoo_env["res.partner"].create({"name": "Body Xfail Partner"})
    _observe_tx_isolation.append(partner.id)
    pytest.xfail("Xfailing inside test body after record write")

@pytest.mark.xfail(strict=True, reason="Strict xfail expecting assertion failure")
def test_strict_xfail_assertion_failure(_observe_tx_isolation, odoo_env):
    partner = odoo_env["res.partner"].create({"name": "Strict Xfail Assertion Partner"})
    _observe_tx_isolation.append(partner.id)
    assert partner.name == "Nonexistent Expected Name", "Intentional assertion failure for strict xfail"

def test_dependent_fixture_skip(fixture_writing_and_skipping):
    pass

def test_cleanup_stage_failure(monkeypatch, _observe_tx_isolation, odoo_env):
    partner = odoo_env["res.partner"].create({"name": "Cleanup Failure Partner"})
    _observe_tx_isolation.append(partner.id)

    from modootest.adapter.v19.transaction import Odoo19Adapter
    orig_clear_caches = Odoo19Adapter.clear_caches

    def failing_clear_caches(adapter_self, registry):
        orig_clear_caches(adapter_self, registry)
        raise RuntimeError("Injected clear_caches cleanup failure")

    monkeypatch.setattr(Odoo19Adapter, "clear_caches", failing_clear_caches)

def test_sentinel_observed_closes(observed_closes):
    assert len(observed_closes) == 5, f"Expected 5 observed closes, got {len(observed_closes)}"
    for rec_ids in observed_closes:
        assert len(rec_ids) > 0, "Recorded ID list must be nonempty"
"""
    )

    # Run pytester child subprocess
    result = pytester.runpytest_subprocess(
        "-p",
        "no:modootest",
        "-p",
        "modootest.pytest_plugin.plugin",
        f"--modootest-config={config_path}",
    )

    # Require exact child pass/skip/xfail/error counts in parent test
    result.assert_outcomes(passed=2, skipped=2, xfailed=2, errors=1)
    assert result.ret == 1
    assert "Injected clear_caches cleanup failure" in result.stdout.str()


def test_computed_fields_invalidation_and_restoration(odoo_registry):
    """
    Verify computed fields (complete_name, display_name) invalidation in-tx
    and full restoration upon transaction exit for both flush_all and unflushed variants.
    """
    target_id = None
    with odoo_registry.cursor() as setup_cr:
        setup_cr.execute("SELECT id FROM res_partner ORDER BY id LIMIT 1")
        row = setup_cr.fetchone()
        assert row is not None, "Database must contain at least one res_partner row"
        target_id = row[0]

    # Baseline inspection
    with TestTransaction(odoo_registry) as tx_base:
        partner_base = tx_base.env["res.partner"].browse(target_id)
        base_name = partner_base.name
        base_complete = partner_base.complete_name
        base_display = partner_base.display_name

    # Flushed variant (flush_all)
    flushed_name = f"Flushed Partner {uuid.uuid4().hex[:6]}"
    with TestTransaction(odoo_registry) as tx_flushed:
        partner_flushed = tx_flushed.env["res.partner"].browse(target_id)
        partner_flushed.write({"name": flushed_name})
        tx_flushed.env.flush_all()

        assert partner_flushed.name == flushed_name
        assert flushed_name in partner_flushed.complete_name
        assert flushed_name in partner_flushed.display_name

    # Verify restoration after flushed transaction exit
    with TestTransaction(odoo_registry) as tx_check1:
        partner_check1 = tx_check1.env["res.partner"].browse(target_id)
        assert partner_check1.name == base_name
        assert partner_check1.complete_name == base_complete
        assert partner_check1.display_name == base_display

    # Unflushed pending recomputation variant
    unflushed_name = f"Unflushed Partner {uuid.uuid4().hex[:6]}"
    with TestTransaction(odoo_registry) as tx_unflushed:
        partner_unflushed = tx_unflushed.env["res.partner"].browse(target_id)
        partner_unflushed.name = unflushed_name
        # Do NOT call flush_all()

        assert partner_unflushed.name == unflushed_name
        assert unflushed_name in partner_unflushed.complete_name
        assert unflushed_name in partner_unflushed.display_name

        # Write a second distinct name and DO NOT read computed values before exit
        second_unflushed_name = f"Unflushed Partner 2 {uuid.uuid4().hex[:6]}"
        partner_unflushed.name = second_unflushed_name

    # Verify restoration after unflushed transaction exit
    with TestTransaction(odoo_registry) as tx_check2:
        partner_check2 = tx_check2.env["res.partner"].browse(target_id)
        assert partner_check2.name == base_name
        assert partner_check2.complete_name == base_complete
        assert partner_check2.display_name == base_display


def test_registry_cache_xmlid_isolation(odoo_registry):
    """
    Verify ir.model.data XML ID lookup cache resolution in-tx and eviction upon exit.
    """
    xml_module = "modootest_test_module"
    xml_name = f"test_xmlid_{uuid.uuid4().hex[:8]}"
    full_xml_id = f"{xml_module}.{xml_name}"

    with TestTransaction(odoo_registry) as tx1:
        partner = tx1.env["res.partner"].create({"name": "Registry Cache Partner"})
        tx1.env["ir.model.data"].create(
            {
                "module": xml_module,
                "name": xml_name,
                "model": "res.partner",
                "res_id": partner.id,
            }
        )

        # Call env.ref to populate the real lookup cache inside transaction
        resolved = tx1.env.ref(full_xml_id)
        assert resolved.id == partner.id
        assert resolved == partner

    # Exit tx1. Verify fresh transaction's env.ref cannot resolve it and no SQL row remains.
    with TestTransaction(odoo_registry) as tx2:
        # raise_if_not_found=False variant must return None
        unresolved = tx2.env.ref(full_xml_id, raise_if_not_found=False)
        assert unresolved is None

        # raise_if_not_found=True variant raises ValueError
        with pytest.raises(ValueError):
            tx2.env.ref(full_xml_id, raise_if_not_found=True)

        # Assert no corresponding ir_model_data SQL row remains
        tx2.cr.execute(
            "SELECT id FROM ir_model_data WHERE module = %s AND name = %s",
            (xml_module, xml_name),
        )
        assert tx2.cr.fetchone() is None
