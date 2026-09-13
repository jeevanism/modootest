"""Unit tests for modootest 0.2 Developer API (isolated from real Odoo runtime)."""
from unittest.mock import MagicMock

import pytest

from modootest.developer import (
    OdooFactory,
    QueryCounter,
    RecordFactory,
    Sequence,
    as_company,
    as_user,
    assert_max_queries,
    query_count,
)


class DummyRecord:
    def __init__(self, record_id: int):
        self.id = record_id


class DummyCursor:
    def __init__(self):
        self.sql_log_count = 0
        self.flushed = False

    def execute(self, query, params=None):
        self.sql_log_count += 1

    def flush(self):
        self.flushed = True


class DummyEnvironment:
    def __init__(self, user=None, context=None, su=True):
        self.uid = 1 if user is None else (user.id if hasattr(user, "id") else user)
        self.context = context or {}
        self.su = su
        self.cr = DummyCursor()
        self.flushed = False
        self.cleared = False
        self._refs = {}
        self._models = {}

    def flush_all(self):
        self.flushed = True

    def clear(self):
        self.cleared = True

    def ref(self, xml_id: str):
        if xml_id not in self._refs:
            raise ValueError(f"External ID not found: {xml_id}")
        return self._refs[xml_id]

    def __call__(self, user=None, context=None, su=None):
        new_su = self.su if su is None else su
        new_user = self.uid if user is None else user
        new_ctx = self.context if context is None else context
        return DummyEnvironment(user=new_user, context=new_ctx, su=new_su)

    def __getitem__(self, model_name: str):
        if model_name not in self._models:
            self._models[model_name] = MagicMock(name=f"Model_{model_name}")
        return self._models[model_name]


# ============================================================================
# as_user Tests
# ============================================================================


def test_as_user_with_integer_id():
    env = DummyEnvironment(su=True)
    with as_user(env, 42) as user_env:
        assert user_env.uid == 42
        assert user_env.su is False
    assert not user_env.cleared


def test_as_user_with_recordset():
    env = DummyEnvironment(su=True)
    user_record = DummyRecord(99)
    with as_user(env, user_record) as user_env:
        assert user_env.uid == 99
        assert user_env.su is False
    assert not user_env.cleared


def test_as_user_with_xml_id():
    env = DummyEnvironment(su=True)
    env._refs["base.user_demo"] = DummyRecord(5)
    with as_user(env, "base.user_demo") as user_env:
        assert user_env.uid == 5
        assert user_env.su is False


def test_as_user_invalid_type_raises():
    env = DummyEnvironment()
    with pytest.raises(TypeError, match="as_user expects"):
        with as_user(env, 3.14):
            pass


# ============================================================================
# as_company Tests
# ============================================================================


def test_as_company_with_integer_id():
    env = DummyEnvironment(context={"allowed_company_ids": [1, 2]})
    with as_company(env, 3) as comp_env:
        assert comp_env.context["allowed_company_ids"] == [3, 1, 2]
        assert comp_env.context["company_id"] == 3


def test_as_company_with_existing_id_moves_to_front():
    env = DummyEnvironment(context={"allowed_company_ids": [1, 2, 3]})
    with as_company(env, 2) as comp_env:
        assert comp_env.context["allowed_company_ids"] == [2, 1, 3]
        assert comp_env.context["company_id"] == 2


def test_as_company_with_recordset():
    env = DummyEnvironment()
    comp_rec = DummyRecord(10)
    with as_company(env, comp_rec) as comp_env:
        assert comp_env.context["allowed_company_ids"] == [10]
        assert comp_env.context["company_id"] == 10


def test_as_company_with_xml_id():
    env = DummyEnvironment()
    env._refs["base.main_company"] = DummyRecord(1)
    with as_company(env, "base.main_company") as comp_env:
        assert comp_env.context["allowed_company_ids"] == [1]
        assert comp_env.context["company_id"] == 1


def test_as_company_invalid_type_raises():
    env = DummyEnvironment()
    with pytest.raises(TypeError, match="as_company expects"):
        with as_company(env, [1, 2]):
            pass


# ============================================================================
# query_count and assert_max_queries Tests
# ============================================================================


def test_query_count_basic():
    env = DummyEnvironment()
    with query_count(env) as qc:
        assert isinstance(qc, QueryCounter)
        env.cr.execute("SELECT 1")
        env.cr.execute("SELECT 2")
    assert qc.count == 2
    assert env.flushed is True
    assert env.cr.flushed is True


def test_query_count_without_flush():
    env = DummyEnvironment()
    with query_count(env, flush=False) as qc:
        env.cr.execute("SELECT 1")
    assert qc.count == 1
    assert env.flushed is False


def test_assert_max_queries_within_budget():
    env = DummyEnvironment()
    with assert_max_queries(env, 3) as qc:
        env.cr.execute("SELECT 1")
        env.cr.execute("SELECT 2")
    assert qc.count == 2


def test_assert_max_queries_exceeded_raises():
    env = DummyEnvironment()
    with pytest.raises(AssertionError, match="Query budget exceeded: executed 3 SQL queries, but maximum allowed was 2"):
        with assert_max_queries(env, 2):
            env.cr.execute("SELECT 1")
            env.cr.execute("SELECT 2")
            env.cr.execute("SELECT 3")


def test_assert_max_queries_invalid_limit():
    env = DummyEnvironment()
    with pytest.raises(ValueError, match="limit must be a non-negative integer"):
        with assert_max_queries(env, -1):
            pass


# ============================================================================
# Sequence & OdooFactory Tests
# ============================================================================


def test_sequence_default():
    seq = Sequence()
    assert seq() == 1
    assert seq() == 2
    seq.reset()
    assert seq() == 1


def test_sequence_with_callable():
    seq = Sequence(lambda n: f"test_{n}@example.com")
    assert seq() == "test_1@example.com"
    assert seq() == "test_2@example.com"


def test_sequence_with_template():
    seq1 = Sequence("item_{seq}")
    assert seq1() == "item_1"
    assert seq1() == "item_2"

    seq2 = Sequence("Partner {}")
    assert seq2() == "Partner 1"


def test_odoo_factory_build_plain():
    factory = OdooFactory()
    factory.register("res.partner", name=Sequence("Partner {}"), is_company=True)

    vals1 = factory.build("res.partner")
    assert vals1 == {"name": "Partner 1", "is_company": True}

    vals2 = factory.build("res.partner", is_company=False, email="test@test.com")
    assert vals2 == {"name": "Partner 2", "is_company": False, "email": "test@test.com"}


def test_odoo_factory_with_record_factory_class():
    class UserFactory(RecordFactory):
        _model = "res.users"
        login = Sequence("user_{seq}")
        active = True

    factory = OdooFactory()
    vals = factory.build(UserFactory, active=False)
    assert vals == {"login": "user_1", "active": False}


def test_odoo_factory_create():
    env = DummyEnvironment()
    created_mock = DummyRecord(101)
    env["res.partner"].create.return_value = created_mock

    factory = OdooFactory(env)
    factory.register("res.partner", name=Sequence("Partner {}"))

    record = factory.create("res.partner", is_company=True)
    assert record.id == 101
    env["res.partner"].create.assert_called_once_with({"name": "Partner 1", "is_company": True})


def test_odoo_factory_create_batch():
    env = DummyEnvironment()
    batch_mock = [DummyRecord(1), DummyRecord(2), DummyRecord(3)]
    env["res.partner"].create.return_value = batch_mock

    factory = OdooFactory(env)
    records = factory.create_batch("res.partner", 3, name=lambda n: f"Batch {n}")
    assert len(records) == 3
    env["res.partner"].create.assert_called_once_with([
        {"name": "Batch 1"},
        {"name": "Batch 2"},
        {"name": "Batch 3"},
    ])


def test_odoo_factory_create_batch_zero():
    env = DummyEnvironment()
    empty_mock = MagicMock()
    env["res.partner"].browse.return_value = empty_mock

    factory = OdooFactory(env)
    records = factory.create_batch("res.partner", 0)
    assert records == empty_mock
    env["res.partner"].browse.assert_called_once_with()


def test_odoo_factory_new():
    env = DummyEnvironment()
    draft_mock = DummyRecord(0)
    env["res.partner"].new.return_value = draft_mock

    factory = OdooFactory(env)
    record = factory.new("res.partner", name="Draft")
    assert record == draft_mock
    env["res.partner"].new.assert_called_once_with({"name": "Draft"})


def test_odoo_factory_without_env_raises_on_create():
    factory = OdooFactory()
    with pytest.raises(RuntimeError, match="Cannot create Odoo record without an active environment"):
        factory.create("res.partner", name="No Env")


# ============================================================================
# Regression Tests for Review Findings 1-4 (Strategies, Sequences, MRO, Containers)
# ============================================================================


def test_nested_factory_build_produces_nested_dicts_without_orm_calls():
    class AddressFactory(RecordFactory):
        _model = "res.partner"
        city = "Brussels"

    class PartnerFactory(RecordFactory):
        _model = "res.partner"
        name = "Parent Partner"
        address = AddressFactory

    env = DummyEnvironment()
    factory = OdooFactory(env)
    built = factory.build(PartnerFactory)

    assert built == {
        "name": "Parent Partner",
        "address": {"city": "Brussels"},
    }
    # Crucial: neither create nor new must be called during build
    assert env["res.partner"].create.call_count == 0
    assert env["res.partner"].new.call_count == 0


def test_nested_factory_new_calls_new_without_create():
    class AddressFactory(RecordFactory):
        _model = "res.partner"
        city = "Ghent"

    class PartnerFactory(RecordFactory):
        _model = "res.partner"
        name = "Draft Partner"
        address = AddressFactory

    env = DummyEnvironment()
    draft_address = DummyRecord(0)
    draft_partner = DummyRecord(0)
    env["res.partner"].new.side_effect = [draft_address, draft_partner]

    factory = OdooFactory(env)
    res = factory.new(PartnerFactory)

    assert res == draft_partner
    # Verify new was called for nested and parent, and create was never called
    assert env["res.partner"].new.call_count == 2
    assert env["res.partner"].create.call_count == 0


def test_nested_factory_create_persists_nested_record():
    class AddressFactory(RecordFactory):
        _model = "res.partner"
        city = "Antwerp"

    class PartnerFactory(RecordFactory):
        _model = "res.partner"
        name = "Real Partner"
        address = AddressFactory

    env = DummyEnvironment()
    persisted_addr = DummyRecord(50)
    persisted_partner = DummyRecord(51)
    env["res.partner"].create.side_effect = [persisted_addr, persisted_partner]

    factory = OdooFactory(env)
    res = factory.create(PartnerFactory)

    assert res == persisted_partner
    assert env["res.partner"].create.call_count == 2


def test_override_avoids_evaluating_overridden_default():
    mock_default = MagicMock(return_value="default_eval")
    factory = OdooFactory()
    factory.register("res.partner", note=mock_default)

    result = factory.build("res.partner", note="explicit_override")
    assert result["note"] == "explicit_override"
    mock_default.assert_not_called()


def test_batch_sequence_advances_monotonically_and_continues():
    env = DummyEnvironment()
    env["res.partner"].create.side_effect = lambda vals_list: [DummyRecord(i) for i in range(len(vals_list))]

    seq = Sequence("Client #{seq}")
    factory = OdooFactory(env)

    # First batch: 3 records
    factory.create_batch("res.partner", 3, ref=seq)
    first_call_vals = env["res.partner"].create.call_args.args[0]
    assert [v["ref"] for v in first_call_vals] == ["Client #1", "Client #2", "Client #3"]

    # Next call with same sequence must continue at 4
    assert seq() == "Client #4"


def test_zero_batch_does_not_advance_sequence():
    env = DummyEnvironment()
    seq = Sequence("Seq {}")
    factory = OdooFactory(env)

    factory.create_batch("res.partner", 0, code=seq)
    assert seq() == "Seq 1"


def test_inherited_factory_defaults_mro_precedence():
    class GrandParent(RecordFactory):
        _model = "res.partner"
        role = "gp"
        country = "BE"

    class Parent(GrandParent):
        role = "p"
        city = "Leuven"

    class Child(Parent):
        role = "c"

    factory = OdooFactory()
    vals = factory.build(Child)
    assert vals == {
        "role": "c",        # Child overrides Parent and GrandParent
        "city": "Leuven",   # Parent default inherited
        "country": "BE",    # GrandParent default inherited
    }


def test_diamond_inheritance_factory_defaults():
    class A(RecordFactory):
        _model = "res.partner"
        attr = "from_A"
        base_val = 100

    class B(A):
        attr = "from_B"

    class C(A):
        attr = "from_C"
        c_val = 200

    class D(B, C):
        pass

    factory = OdooFactory()
    vals = factory.build(D)
    assert vals["attr"] == "from_B"  # B precedes C in D's MRO
    assert vals["base_val"] == 100
    assert vals["c_val"] == 200


def test_blueprint_precedes_class_defaults_and_override_wins():
    class Partner(RecordFactory):
        _model = "res.partner"
        name = "Class Name"
        tier = "Gold"

    factory = OdooFactory()
    # Blueprint provides base defaults
    factory.register("res.partner", name="Blueprint Name", region="EMEA")

    # Class defaults override blueprint defaults, but blueprint provides missing fields
    vals = factory.build(Partner, tier="Platinum")
    assert vals == {
        "name": "Class Name",       # Class default wins over blueprint
        "region": "EMEA",           # Blueprint provides base
        "tier": "Platinum",         # Explicit override wins over class
    }


def test_property_descriptor_not_invoked_during_field_discovery():
    property_getter_called = False

    class DangerousPropertyFactory(RecordFactory):
        _model = "res.partner"
        safe_field = "ok"

        @property
        def side_effect(self):
            nonlocal property_getter_called
            property_getter_called = True
            raise RuntimeError("Property getter should never be called during discovery!")

        def my_method(self):
            return "method_result"

    factory = OdooFactory()
    vals = factory.build(DangerousPropertyFactory)
    assert property_getter_called is False
    assert vals == {"safe_field": "ok"}
    assert "side_effect" not in vals
    assert "my_method" not in vals


def test_mutable_containers_in_defaults_and_overrides_are_isolated():
    factory = OdooFactory()
    factory.register("res.partner", lines=[(0, 0, {"qty": 5})])

    res1 = factory.build("res.partner")
    res1["lines"][0][2]["qty"] = 99

    res2 = factory.build("res.partner")
    assert res2["lines"][0][2]["qty"] == 5


def test_batch_rows_receive_independent_mutable_containers():
    env = DummyEnvironment()
    env["res.partner"].create.side_effect = lambda vals: vals

    factory = OdooFactory(env)
    records = factory.create_batch("res.partner", 2, tags=["alpha"])
    records[0]["tags"].append("beta")
    assert records[1]["tags"] == ["alpha"]


def test_callable_raising_exception_propagates_without_retry():
    call_count = 0

    def bad_func():
        nonlocal call_count
        call_count += 1
        raise TypeError("Custom user body error")

    factory = OdooFactory()
    factory.register("res.partner", item=bad_func)

    with pytest.raises(TypeError, match="Custom user body error"):
        factory.build("res.partner")

    assert call_count == 1


def test_sequence_mutable_results_are_independent_permanent():
    shared_template = {"keys": ["init"]}
    factory = OdooFactory()
    factory.register("res.partner", data=Sequence(lambda n: shared_template))

    rec1 = factory.build("res.partner")
    rec2 = factory.build("res.partner")

    rec1["data"]["keys"].append("mutated")
    assert rec2["data"] == {"keys": ["init"]}
    assert shared_template == {"keys": ["init"]}
