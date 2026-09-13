"""Real database contract and behavioral integration tests for modootest."""
import pytest
from modootest.isolation.transaction import TestTransaction


def test_primary_native_example_create(odoo_env):
    """Primary native example: create res.partner and observe record value."""
    partner = odoo_env["res.partner"].create({"name": "modootest Example Partner"})
    assert partner.name == "modootest Example Partner"
    assert partner.id > 0


def test_derived_environment_shares_transaction_and_is_removed(odoo_registry):
    with TestTransaction(odoo_registry) as tx:
        original_envs = {id(env) for env in tx.env.transaction.envs}
        derived = tx.env(context={"modootest_derived": True})
        assert derived is not tx.env
        assert derived.cr is tx.cr
        assert derived.transaction is tx.env.transaction
        assert id(derived) not in original_envs
    assert tx.cr.closed
    assert {id(env) for env in tx.env.transaction.envs} == original_envs


def test_direct_transaction_create_rollback(odoo_registry):
    """Directly enter TestTransaction, create record, exit, and verify with fresh cursor."""
    created_id = None
    with TestTransaction(odoo_registry) as tx:
        partner = tx.env["res.partner"].create({"name": "Direct Tx Create Partner"})
        created_id = partner.id
        assert partner.id > 0

    assert created_id is not None
    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute("SELECT id FROM res_partner WHERE id = %s", (created_id,))
        assert fresh_cr.fetchone() is None, "Created record must be rolled back after transaction exit"


def test_direct_transaction_write_rollback(odoo_registry):
    """Directly enter TestTransaction, write to existing partner, exit, verify with fresh cursor."""
    target_id = None
    original_name = None
    with odoo_registry.cursor() as setup_cr:
        setup_cr.execute("SELECT id, name FROM res_partner ORDER BY id LIMIT 1")
        row = setup_cr.fetchone()
        assert row is not None, "Database must contain at least one res_partner row"
        target_id, original_name = row

    with TestTransaction(odoo_registry) as tx:
        partner = tx.env["res.partner"].browse(target_id)
        partner.write({"name": "Mutated Name In Tx"})
        assert partner.name == "Mutated Name In Tx"

    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute("SELECT name FROM res_partner WHERE id = %s", (target_id,))
        assert fresh_cr.fetchone()[0] == original_name, "Write mutation must be rolled back"


def test_direct_transaction_unlink_rollback(odoo_registry):
    """Directly enter TestTransaction, unlink existing country AQ, exit, verify with fresh cursor."""
    target_id = None
    with odoo_registry.cursor() as setup_cr:
        setup_cr.execute("SELECT id FROM res_country WHERE code = %s", ("AQ",))
        row = setup_cr.fetchone()
        assert row is not None, "Base country AQ must exist in test database"
        target_id = row[0]

    with TestTransaction(odoo_registry) as tx:
        country = tx.env["res.country"].browse(target_id)
        country.unlink()
        assert not tx.env["res.country"].browse(target_id).exists()

    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute("SELECT id FROM res_country WHERE id = %s", (target_id,))
        assert fresh_cr.fetchone() is not None, "Unlinked record must be restored after rollback"


def test_direct_transaction_raw_sql_rollback(odoo_registry):
    """Directly enter TestTransaction, execute raw SQL INSERT, exit, verify with fresh cursor."""
    created_id = None
    with TestTransaction(odoo_registry) as tx:
        tx.cr.execute("INSERT INTO res_partner (name, active) VALUES ('SQL Direct Tx', true) RETURNING id")
        created_id = tx.cr.fetchone()[0]

    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute("SELECT id FROM res_partner WHERE id = %s", (created_id,))
        assert fresh_cr.fetchone() is None, "Raw SQL INSERT must be rolled back"


def test_direct_transaction_cursor_closed_on_exit(odoo_registry):
    """Cursor is closed after normal direct TestTransaction exit."""
    tx_cr = None
    with TestTransaction(odoo_registry) as tx:
        tx_cr = tx.cr
        assert tx_cr.closed is False
    assert tx_cr.closed is True


def test_body_exception_rollback_and_cursor_closed(odoo_registry):
    """Exception in transaction body rolls back created record and closes transaction cursor."""
    created_id = None
    tx_cr = None
    with pytest.raises(ValueError, match="Simulated body failure"):
        with TestTransaction(odoo_registry) as tx:
            tx_cr = tx.cr
            partner = tx.env["res.partner"].create({"name": "Failing Body Partner"})
            created_id = partner.id
            raise ValueError("Simulated body failure")

    assert tx_cr is not None and tx_cr.closed is True
    assert created_id is not None
    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute("SELECT id FROM res_partner WHERE id = %s", (created_id,))
        assert fresh_cr.fetchone() is None


def test_cursor_guards_block_commit(odoo_cr):
    """Calling commit() raises RuntimeError on test cursor."""
    with pytest.raises(RuntimeError, match="blocked by modootest TestTransaction guard"):
        odoo_cr.commit()


def test_cursor_guards_block_rollback(odoo_cr):
    """Calling rollback() raises RuntimeError on test cursor."""
    with pytest.raises(RuntimeError, match="blocked by modootest TestTransaction guard"):
        odoo_cr.rollback()


def test_cursor_guards_block_close(odoo_cr):
    """Calling close() raises RuntimeError on test cursor."""
    with pytest.raises(RuntimeError, match="blocked by modootest TestTransaction guard"):
        odoo_cr.close()


def test_fixture_request_order_cr_env(odoo_cr, odoo_env):
    """odoo_cr is odoo_env.cr when requested as (odoo_cr, odoo_env)."""
    assert odoo_cr is odoo_env.cr
    assert odoo_cr.closed is False


def test_fixture_request_order_env_cr(odoo_env, odoo_cr):
    """odoo_cr is odoo_env.cr when requested as (odoo_env, odoo_cr)."""
    assert odoo_cr is odoo_env.cr
    assert odoo_cr.closed is False


def test_cr_only_access(odoo_cr):
    """Requesting odoo_cr alone works and receives owned cleanup."""
    odoo_cr.execute("SELECT 1")
    assert odoo_cr.fetchone()[0] == 1


@pytest.mark.parametrize("param_val", ["ParamScenarioA", "ParamScenarioB", "ParamScenarioC"])
def test_parametrized_cases_isolation(odoo_env, odoo_registry, param_val):
    """Parametrized test invocations prove no previous case's row remains."""
    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute("SELECT id FROM res_partner WHERE name LIKE 'Shared Marker %'")
        assert fresh_cr.fetchall() == [], "No leftover row from previous parametrized run should exist"

    partner = odoo_env["res.partner"].create({"name": f"Shared Marker {param_val}"})
    assert partner.name == f"Shared Marker {param_val}"


def test_invalid_sql_teardown_recovery(odoo_cr):
    """Invalid SQL query raises psycopg2.errors.UndefinedTable but allows clean transaction teardown."""
    import psycopg2.errors
    with pytest.raises(psycopg2.errors.UndefinedTable):
        odoo_cr.execute("SELECT * FROM non_existent_table_modootest_test")


@pytest.fixture
def custom_partner_helper(odoo_env):
    def _create(name):
        return odoo_env["res.partner"].create({"name": name})
    return _create


def test_nested_fixture_composition(custom_partner_helper):
    """Nested fixture composition utilizing odoo_env operates correctly."""
    partner = custom_partner_helper("Nested Helper Partner")
    assert partner.name == "Nested Helper Partner"
