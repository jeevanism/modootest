"""Integration tests for modootest 0.2 Developer API against real Odoo 19 database."""
import pytest
from odoo.exceptions import AccessError

from modootest.developer import (
    OdooFactory,
    RecordFactory,
    Sequence,
    as_company,
    as_user,
    assert_max_queries,
    query_count,
)


def test_as_user_with_xml_id_and_record(odoo_env, as_user):
    """Verify as_user switches uid, enforces su=False, and accepts both xml_id and recordset."""
    admin_user = odoo_env.ref("base.user_admin")

    with as_user("base.user_admin") as admin_env:
        assert admin_env.uid == admin_user.id
        assert admin_env.su is False
        assert admin_env.cr is odoo_env.cr

    with as_user(admin_user) as admin_env2:
        assert admin_env2.uid == admin_user.id
        assert admin_env2.su is False


def test_as_user_enforces_security_access_rights(odoo_env, as_user, odoo_factory):
    """Verify that as_user derives environment with su=False, enforcing real AccessError."""
    # Create an unprivileged user (Portal or Public user without settings/admin rights)
    group_portal = odoo_env.ref("base.group_portal")
    limited_user = odoo_factory.create(
        "res.users",
        name="Limited User",
        login="limited_test_user",
        group_ids=[(6, 0, [group_portal.id])],
    )

    # In SUPERUSER mode (odoo_env), modifying res.groups or res.users is permitted
    assert odoo_env.su is True

    # Under as_user with the limited user, su=False is enforced, so admin operations raise AccessError
    with as_user(limited_user) as user_env:
        assert user_env.su is False
        assert user_env.uid == limited_user.id
        with pytest.raises(AccessError):
            # A portal user cannot create another user or alter system groups
            user_env["res.users"].create({
                "name": "Unauthorized User",
                "login": "unauth_login",
            })


def test_as_company_protocol(odoo_env, as_company, odoo_factory):
    """Verify as_company updates allowed_company_ids placing target company at index 0."""
    main_company = odoo_env.ref("base.main_company")

    # Create a secondary test company
    branch_company = odoo_factory.create("res.company", name="Branch Company Ltd")

    with as_company(branch_company) as comp_env:
        assert comp_env.company.id == branch_company.id
        allowed = comp_env.context.get("allowed_company_ids")
        assert allowed is not None
        assert allowed[0] == branch_company.id

    # Now switch back to main_company via XML-ID
    with as_company("base.main_company") as comp_env2:
        assert comp_env2.company.id == main_company.id
        assert comp_env2.context["allowed_company_ids"][0] == main_company.id


def test_query_count_and_budget_assertion(odoo_env, query_count, assert_max_queries):
    """Verify query counting and assert_max_queries enforcement against real PostgreSQL cursor."""
    # 1. query_count measures real queries
    with query_count() as qc:
        odoo_env["res.partner"].search([("id", "=", 1)])
    assert qc.count > 0

    # 2. assert_max_queries passes when budget is respected
    with assert_max_queries(qc.count + 5):
        odoo_env["res.partner"].search([("id", "=", 1)])

    # 3. assert_max_queries raises AssertionError when budget is exceeded
    with pytest.raises(AssertionError, match="Query budget exceeded"):
        with assert_max_queries(0):
            odoo_env["res.partner"].search([("id", "=", 1)])


def test_odoo_factory_crud_and_sequences(odoo_env, odoo_factory):
    """Verify OdooFactory creates records with sequences, blueprints, and batch operations."""
    class CustomerFactory(RecordFactory):
        _model = "res.partner"
        name = Sequence("Customer #{seq}")
        is_company = True
        email = Sequence(lambda n: f"customer{n}@example.com")

    # 1. Create single record via RecordFactory class
    cust1 = odoo_factory.create(CustomerFactory)
    assert cust1.id > 0
    assert cust1.name == "Customer #1"
    assert cust1.email == "customer1@example.com"
    assert cust1.is_company is True

    # 2. Create single record with override
    cust2 = odoo_factory.create(CustomerFactory, is_company=False)
    assert cust2.id > 0
    assert cust2.name == "Customer #2"
    assert cust2.is_company is False

    # 3. Batch creation
    batch = odoo_factory.create_batch(CustomerFactory, 3)
    assert len(batch) == 3
    assert [c.name for c in batch] == ["Customer #3", "Customer #4", "Customer #5"]

    # 4. In-memory draft (new)
    draft = odoo_factory.new(CustomerFactory, name="Virtual Draft")
    assert not draft.id  # NewId without DB integer ID
    assert draft.name == "Virtual Draft"


def test_developer_api_transaction_rollback(odoo_registry):
    """Verify that all records created via developer API are strictly rolled back."""
    from modootest.isolation.transaction import TestTransaction

    created_id = None
    with TestTransaction(odoo_registry) as tx:
        factory = OdooFactory(tx.env)
        partner = factory.create("res.partner", name="Rollback Check Partner")
        created_id = partner.id
        assert created_id > 0

    # Outside transaction, ensure record was rolled back
    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute("SELECT id FROM res_partner WHERE id = %s", (created_id,))
        assert fresh_cr.fetchone() is None, "Factory created record must be rolled back"


def test_nested_factory_real_odoo_strategies_and_persistence(odoo_registry):
    """Verify in real Odoo:
    1. build() with nested RecordFactory produces nested dict without ORM writes.
    2. new() with nested RecordFactory produces draft in-memory records without ORM writes.
    3. create() with nested RecordFactory persists related records in DB within transaction.
    4. Upon transaction exit, both records are safely rolled back (verified with fresh cursor).
    """
    from modootest.isolation.transaction import TestTransaction

    class CompanyFactory(RecordFactory):
        _model = "res.partner"
        name = Sequence("Parent Corp {seq}")
        is_company = True

    class EmployeeFactory(RecordFactory):
        _model = "res.partner"
        name = Sequence("Employee {seq}")
        parent_id = CompanyFactory

    emp_id = None
    parent_id = None

    with TestTransaction(odoo_registry) as tx:
        factory = OdooFactory(tx.env)

        # 1. build(): returns dict with nested dict; zero rows in DB
        built_data = factory.build(EmployeeFactory)
        assert isinstance(built_data["parent_id"], dict)
        assert built_data["parent_id"]["is_company"] is True
        assert not tx.env["res.partner"].search([("name", "=", built_data["name"])])
        assert not tx.env["res.partner"].search([("name", "=", built_data["parent_id"]["name"])])

        # 2. new(): returns in-memory draft with draft relation; zero rows in DB
        draft_emp = factory.new(EmployeeFactory)
        assert not draft_emp.id
        assert not draft_emp.parent_id.id
        assert draft_emp.parent_id.is_company is True
        assert not tx.env["res.partner"].search([("name", "=", draft_emp.name)])
        assert not tx.env["res.partner"].search([("name", "=", draft_emp.parent_id.name)])

        # 3. create(): persists inside active transaction
        real_emp = factory.create(EmployeeFactory)
        emp_id = real_emp.id
        parent_id = real_emp.parent_id.id
        assert emp_id > 0
        assert parent_id > 0
        assert real_emp.parent_id.is_company is True
        assert tx.env["res.partner"].search_count([("id", "in", [emp_id, parent_id])]) == 2

    # 4. Outside transaction, verify with a fresh cursor that both records rolled back
    with odoo_registry.cursor() as fresh_cr:
        fresh_cr.execute("SELECT id FROM res_partner WHERE id IN (%s, %s)", (emp_id, parent_id))
        assert len(fresh_cr.fetchall()) == 0, "Both nested records must be rolled back"
