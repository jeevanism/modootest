"""Tests for conservative change classification and AST analysis."""

import pytest
from modootest.intelligence.classifier import (
    ChangeCategory,
    _compare_python_ast,
    classify_file_change,
    is_documentation_path,
    is_framework_build_path,
)
from modootest.intelligence.git import GitChangeRecord
from modootest.intelligence.ownership import OwnedChangeRecord


def test_documentation_allowlist():
    assert is_documentation_path("README.md")
    assert is_documentation_path("docs/planning_policy.md")
    assert is_documentation_path("README")
    assert is_documentation_path("LICENSE")
    assert not is_documentation_path("LICENSE.txt")
    assert not is_documentation_path(".gitignore")
    assert not is_documentation_path("models/sale.py")
    assert not is_documentation_path("views/sale.xml")
    assert not is_documentation_path("readme.py")
    assert not is_documentation_path("random.unknown")


def test_framework_build_paths():
    assert is_framework_build_path("modootest.conf")
    assert is_framework_build_path("pyproject.toml")
    assert is_framework_build_path("setup.py")
    assert is_framework_build_path("requirements.txt")
    assert is_framework_build_path(".github/workflows/ci.yml")
    assert not is_framework_build_path("addons/sale/models/sale.py")


def test_method_only_python_change():
    old_src = """
from odoo import models, fields, api

class SaleOrder(models.Model):
    _name = 'sale.order'
    _description = 'Sales Order'

    name = fields.Char('Reference')
    amount = fields.Float('Amount')

    @api.model
    def action_confirm(self):
        self.state = 'done'
        return True
"""
    new_src = """
from odoo import models, fields, api

class SaleOrder(models.Model):
    _name = 'sale.order'
    _description = 'Sales Order'

    name = fields.Char('Reference')
    amount = fields.Float('Amount')

    @api.model
    def action_confirm(self):
        # Updated internal logic only!
        if self.amount < 0:
            raise ValueError('Invalid amount')
        self.state = 'done'
        return True
"""
    cat, fp, mu, reason = _compare_python_ast(old_src, new_src)
    assert cat == ChangeCategory.METHOD_ONLY
    assert fp is True
    assert mu is False
    assert "Proven method-only" in reason


def test_orm_field_added_removed_or_changed():
    base_src = """
from odoo import models, fields

class Partner(models.Model):
    _name = 'res.partner'
    name = fields.Char('Name')
"""
    # 1. Field added
    added_src = """
from odoo import models, fields

class Partner(models.Model):
    _name = 'res.partner'
    name = fields.Char('Name')
    is_vip = fields.Boolean('VIP Customer')
"""
    cat, fp, mu, _ = _compare_python_ast(base_src, added_src)
    assert cat == ChangeCategory.FIELD_DECLARATION
    assert fp is True
    assert mu is True

    # 2. Field removed
    removed_src = """
from odoo import models, fields

class Partner(models.Model):
    _name = 'res.partner'
"""
    cat, fp, mu, _ = _compare_python_ast(base_src, removed_src)
    assert cat == ChangeCategory.FIELD_DECLARATION
    assert fp is True
    assert mu is True

    # 3. Field parameters modified
    modified_src = """
from odoo import models, fields

class Partner(models.Model):
    _name = 'res.partner'
    name = fields.Char('Name', required=True)
"""
    cat, fp, mu, _ = _compare_python_ast(base_src, modified_src)
    assert cat == ChangeCategory.FIELD_DECLARATION
    assert fp is True
    assert mu is True


def test_structural_class_and_model_identity_changes():
    base_src = """
from odoo import models

class MyModel(models.Model):
    _name = 'my.model'
    _inherit = ['mail.thread']

    def foo(self):
        return 1
"""
    # 1. Change _name
    name_src = base_src.replace("my.model", "my.new.model")
    cat, fp, mu, reason = _compare_python_ast(base_src, name_src)
    assert cat == ChangeCategory.MODEL_CLASS
    assert fp is True
    assert mu is True
    assert "_name" in reason

    # 2. Change _inherit
    inherit_src = base_src.replace("['mail.thread']", "['mail.thread', 'mail.activity.mixin']")
    cat, fp, mu, reason = _compare_python_ast(base_src, inherit_src)
    assert cat == ChangeCategory.MODEL_CLASS
    assert fp is True
    assert mu is True
    assert "_inherit" in reason

    # 3. Change class bases
    base_class_src = base_src.replace("models.Model", "models.TransientModel")
    cat, fp, mu, reason = _compare_python_ast(base_src, base_class_src)
    assert cat == ChangeCategory.MODEL_CLASS
    assert fp is True
    assert mu is True
    assert "inheritance or bases" in reason


def test_method_decorators_and_signature_changes():
    base_src = """
from odoo import models, api

class TestModel(models.Model):
    _name = 'test.model'

    @api.model
    def my_method(self, x):
        return x
"""
    # 1. Decorator added/changed
    decor_src = """
from odoo import models, api

class TestModel(models.Model):
    _name = 'test.model'

    @api.model
    @api.depends('some_field')
    def my_method(self, x):
        return x
"""
    cat, fp, mu, reason = _compare_python_ast(base_src, decor_src)
    assert cat == ChangeCategory.MODEL_CLASS
    assert fp is True
    assert mu is True
    assert "Decorators modified" in reason

    # 2. Signature changed
    sig_src = """
from odoo import models, api

class TestModel(models.Model):
    _name = 'test.model'

    @api.model
    def my_method(self, x, y=10):
        return x + y
"""
    cat, fp, mu, reason = _compare_python_ast(base_src, sig_src)
    assert cat == ChangeCategory.MODEL_CLASS
    assert fp is True
    assert mu is True
    assert "Signature modified" in reason


def test_imports_and_syntax_error():
    base_src = """
from odoo import models

class Simple(models.Model):
    _name = 'simple'
"""
    import_src = """
import logging
from odoo import models

class Simple(models.Model):
    _name = 'simple'
"""
    cat, fp, mu, reason = _compare_python_ast(base_src, import_src)
    assert cat == ChangeCategory.MODEL_CLASS
    assert fp is True
    assert mu is True
    assert "imports" in reason

    # Syntax error
    cat, fp, mu, reason = _compare_python_ast(base_src, "def bad(")
    assert cat == ChangeCategory.UNKNOWN_UNCERTAIN
    assert fp is True
    assert mu is None
    assert "Syntax error" in reason


def test_classify_file_change_various_types():
    def make_owned(path, status="M"):
        rec = GitChangeRecord(
            status=status,
            old_path=path if status != "A" else None,
            new_path=path if status != "D" else None,
            old_mode="100644",
            new_mode="100644",
            old_sha="a" * 40,
            new_sha="b" * 40,
            status_field=status,
        )
        return OwnedChangeRecord(record=rec, old_owner="sale", new_owner="sale")

    # Tests directory
    fc_test = classify_file_change(
        make_owned("addons/sale/tests/test_order.py"),
        lambda p: b"",
        lambda p: b"",
        set(),
    )
    assert fc_test.category == ChangeCategory.TESTS_ONLY
    assert fc_test.fresh_process is True
    assert fc_test.module_update is False

    # XML file
    fc_xml = classify_file_change(
        make_owned("addons/sale/views/sale_order.xml"),
        lambda p: b"",
        lambda p: b"",
        set(),
    )
    assert fc_xml.category == ChangeCategory.XML_DATA
    assert fc_xml.fresh_process is True
    assert fc_xml.module_update is True

    # Security CSV
    fc_csv = classify_file_change(
        make_owned("addons/sale/security/ir.model.access.csv"),
        lambda p: b"",
        lambda p: b"",
        set(),
    )
    assert fc_csv.category == ChangeCategory.XML_DATA
    assert fc_csv.fresh_process is True
    assert fc_csv.module_update is True

    # Manifest change
    fc_man = classify_file_change(
        make_owned("addons/sale/__manifest__.py"),
        lambda p: b"",
        lambda p: b"",
        set(),
    )
    assert fc_man.category == ChangeCategory.MANIFEST_CHANGE
    assert fc_man.fresh_process is True
    assert fc_man.module_update is True

    # Manifest-listed data file with unconventional name
    fc_unconv = classify_file_change(
        make_owned("addons/sale/data/config.txt"),
        lambda p: b"",
        lambda p: b"",
        manifest_data_files={"addons/sale/data/config.txt"},
    )
    assert fc_unconv.category == ChangeCategory.XML_DATA
    assert fc_unconv.fresh_process is True
    assert fc_unconv.module_update is True


def test_contract_a_ast_masker_preserves_multiplicity_and_order():
    old = """class Model:
    if enabled:
        value = fields.Char()
    if other:
        label = fields.Char()
"""
    new = old.replace("value = fields.Char()", "value = fields.Integer()")
    cat, fp, mu, reason = _compare_python_ast(old, new)
    assert mu is True
    assert cat in (ChangeCategory.MODEL_CLASS, ChangeCategory.FIELD_DECLARATION)

    # Order permutation must be detected
    permuted = """class Model:
    if other:
        label = fields.Char()
    if enabled:
        value = fields.Char()
"""
    cat2, fp2, mu2, _ = _compare_python_ast(old, permuted)
    assert mu2 is not False


def test_contract_a_class_and_method_decorators():
    base = """class Model:
    def method(self):
        return 1
"""
    # Class decorator added
    decorated_class = """@some_decorator
class Model:
    def method(self):
        return 1
"""
    cat, fp, mu, _ = _compare_python_ast(base, decorated_class)
    assert mu is True
    assert cat == ChangeCategory.MODEL_CLASS

    # Method decorator added
    decorated_method = """class Model:
    @api.model
    def method(self):
        return 1
"""
    cat2, fp2, mu2, _ = _compare_python_ast(base, decorated_method)
    assert mu2 is True
    assert cat2 == ChangeCategory.MODEL_CLASS


def test_contract_b_rename_monotonic_role_merging():
    # Source renamed into tests: old was model source, new is tests
    rec = GitChangeRecord("R", "addons/a/models.py", "addons/a/tests/test_models.py", "100644", "100644", "a" * 40, "b" * 40, "R")
    owned = OwnedChangeRecord(rec, "a", "a")
    res = classify_file_change(owned, lambda p: b"x = 1", lambda p: b"x = 2", set())
    assert res.module_update is True
    assert res.fresh_process is True

    # Tests renamed into source: old was tests, new is model source
    rec2 = GitChangeRecord("R", "addons/a/tests/test_models.py", "addons/a/models.py", "100644", "100644", "a" * 40, "b" * 40, "R")
    owned2 = OwnedChangeRecord(rec2, "a", "a")
    res2 = classify_file_change(owned2, lambda p: b"x = 1", lambda p: b"x = 2", set())
    assert res2.module_update is True
    assert res2.fresh_process is True


def test_contract_c_strict_documentation_and_config_allowlists():
    # Documentation allowlist: only .md, .rst, or exact case-insensitive extensionless
    assert is_documentation_path("README.md")
    assert is_documentation_path("docs/index.rst")
    assert is_documentation_path("README")
    assert is_documentation_path("LICENSE")
    assert not is_documentation_path("readme.py")
    assert not is_documentation_path("doc.py")
    assert not is_documentation_path("license.sh")
    assert not is_documentation_path("docs/script.py")

    # Framework build/config allowlist
    assert is_framework_build_path(".gitignore")
    assert is_framework_build_path("pyproject.toml")
    assert is_framework_build_path("setup.py")
    assert is_framework_build_path("setup.cfg")
    assert is_framework_build_path("tox.ini")
    assert is_framework_build_path("pytest.ini")
    assert not is_framework_build_path("outside.py")


def test_contract_d_test_syntax_validation_and_unowned_files():
    # Syntax error in test file flags UNKNOWN_UNCERTAIN
    rec = GitChangeRecord("M", "addons/a/tests/test_x.py", "addons/a/tests/test_x.py", "100644", "100644", "a" * 40, "b" * 40, "M")
    owned = OwnedChangeRecord(rec, "a", "a")
    res = classify_file_change(owned, lambda p: b"def valid(): pass", lambda p: b"def broken(", set())
    assert res.category == ChangeCategory.UNKNOWN_UNCERTAIN
    assert res.is_uncertain is True
    assert "Syntax error" in res.reason

    # Unowned runtime Python file outside any addon
    rec_outside = GitChangeRecord("M", "outside.py", "outside.py", "100644", "100644", "a" * 40, "b" * 40, "M")
    owned_outside = OwnedChangeRecord(rec_outside, None, None)
    res_outside = classify_file_change(owned_outside, lambda p: b"x = 1", lambda p: b"x = 2", set())
    assert res_outside.category == ChangeCategory.UNKNOWN_UNCERTAIN
    assert res_outside.is_uncertain is True
    assert res_outside.module_update is None


def test_r3_toplevel_sync_and_async_functions_not_masked():
    # Sync top-level function changed
    old_sync = """
def helper(x):
    return x + 1
"""
    new_sync = """
def helper(x):
    return x + 2
"""
    cat, fp, mu, reason = _compare_python_ast(old_sync, new_sync)
    assert mu is True
    assert fp is True

    # Async top-level function changed
    old_async = """
async def async_helper(x):
    return x + 1
"""
    new_async = """
async def async_helper(x):
    return x + 2
"""
    cat, fp, mu, reason = _compare_python_ast(old_async, new_async)
    assert mu is True
    assert fp is True


def test_r3_dynamic_model_factory_and_nested_class_detection():
    # Dynamic model factory at top-level
    old_factory = """
def make_model():
    return type('DynamicPartner', (models.Model,), {'_name': 'res.partner', 'x': fields.Char()})
"""
    new_factory = """
def make_model():
    return type('DynamicPartner', (models.Model,), {'_name': 'res.partner', 'x': fields.Integer()})
"""
    cat, fp, mu, reason = _compare_python_ast(old_factory, new_factory)
    assert mu is True
    assert fp is True

    # Nested class / type inside a class method
    old_method_dynamic = """
class MyModel(models.Model):
    _name = 'my.model'
    def my_method(self):
        sub_model = type('SubModel', (models.Model,), {})
        return 1
"""
    new_method_dynamic = """
class MyModel(models.Model):
    _name = 'my.model'
    def my_method(self):
        sub_model = type('SubModel', (models.Model,), {})
        return 2
"""
    cat, fp, mu, reason = _compare_python_ast(old_method_dynamic, new_method_dynamic)
    assert mu is True
    assert fp is True
    assert cat == ChangeCategory.MODEL_CLASS
    assert "nested classes or dynamic models" in reason


def test_r3_rename_monotonic_uncertainty_preservation():
    # 1. Unowned source -> owned source
    rec_unowned_to_owned = GitChangeRecord(
        "R", "scripts/generator.py", "addons/a/models.py", "100644", "100644", "a" * 40, "b" * 40, "R"
    )
    owned1 = OwnedChangeRecord(rec_unowned_to_owned, None, "a")
    res1 = classify_file_change(owned1, lambda p: b"x = 1", lambda p: b"x = 2", set())
    assert res1.module_update is True
    assert res1.is_uncertain is True
    assert res1.category == ChangeCategory.UNKNOWN_UNCERTAIN

    # 2. Owned source -> unowned source
    rec_owned_to_unowned = GitChangeRecord(
        "R", "addons/a/models.py", "scripts/generator.py", "100644", "100644", "a" * 40, "b" * 40, "R"
    )
    owned2 = OwnedChangeRecord(rec_owned_to_unowned, "a", None)
    res2 = classify_file_change(owned2, lambda p: b"x = 1", lambda p: b"x = 2", set())
    assert res2.is_uncertain is True
    assert res2.category == ChangeCategory.UNKNOWN_UNCERTAIN

    # 3. Syntax error on one side with definite update on the other
    rec_syntax_err = GitChangeRecord(
        "R", "addons/a/old_model.py", "addons/a/new_model.py", "100644", "100644", "a" * 40, "b" * 40, "R"
    )
    owned3 = OwnedChangeRecord(rec_syntax_err, "a", "a")
    res3 = classify_file_change(
        owned3,
        lambda p: b"class broken(\n" if "old" in p else b"class New(models.Model):\n    _name = 'new.model'\n",
        lambda p: b"class New(models.Model):\n    _name = 'new.model'\n" if "new" in p else b"class broken(\n",
        set(),
    )
    assert res3.module_update is True
    assert res3.is_uncertain is True
    assert res3.category == ChangeCategory.UNKNOWN_UNCERTAIN

    # 4. Known-to-known rename retaining existing behavior (certain)
    rec_known = GitChangeRecord(
        "R", "addons/a/old_model.py", "addons/a/new_model.py", "100644", "100644", "a" * 40, "b" * 40, "R"
    )
    owned4 = OwnedChangeRecord(rec_known, "a", "a")
    res4 = classify_file_change(
        owned4,
        lambda p: b"class Old(models.Model):\n    _name = 'old'\n",
        lambda p: b"class New(models.Model):\n    _name = 'new'\n",
        set(),
    )
    assert res4.module_update is True
    assert res4.is_uncertain is False
    assert res4.category == ChangeCategory.MODEL_CLASS


def test_r3_unreadable_file_handling_across_lifecycle():
    # 1. Unreadable modified test
    rec_mod_test = GitChangeRecord(
        "M", "addons/a/tests/test_x.py", "addons/a/tests/test_x.py", "100644", "100644", "a" * 40, "b" * 40, "M"
    )
    owned_mod_test = OwnedChangeRecord(rec_mod_test, "a", "a")
    res_mod_test = classify_file_change(owned_mod_test, lambda p: None, lambda p: None, set())
    assert res_mod_test.category == ChangeCategory.UNKNOWN_UNCERTAIN
    assert res_mod_test.is_uncertain is True
    assert "Could not read test file contents" in res_mod_test.reason

    # 2. Unreadable added test
    rec_add_test = GitChangeRecord(
        "A", None, "addons/a/tests/test_new.py", "000000", "100644", "0" * 40, "b" * 40, "A"
    )
    owned_add_test = OwnedChangeRecord(rec_add_test, None, "a")
    res_add_test = classify_file_change(owned_add_test, lambda p: None, lambda p: None, set())
    assert res_add_test.category == ChangeCategory.UNKNOWN_UNCERTAIN
    assert res_add_test.is_uncertain is True

    # 3. Unreadable deleted test
    rec_del_test = GitChangeRecord(
        "D", "addons/a/tests/test_old.py", None, "100644", "000000", "a" * 40, "0" * 40, "D"
    )
    owned_del_test = OwnedChangeRecord(rec_del_test, "a", None)
    res_del_test = classify_file_change(owned_del_test, lambda p: None, lambda p: None, set())
    assert res_del_test.category == ChangeCategory.UNKNOWN_UNCERTAIN
    assert res_del_test.is_uncertain is True

    # 4. Unreadable ordinary Python (modified, added, deleted)
    rec_mod_py = GitChangeRecord(
        "M", "addons/a/models.py", "addons/a/models.py", "100644", "100644", "a" * 40, "b" * 40, "M"
    )
    owned_mod_py = OwnedChangeRecord(rec_mod_py, "a", "a")
    res_mod_py = classify_file_change(owned_mod_py, lambda p: None, lambda p: b"x = 1", set())
    assert res_mod_py.category == ChangeCategory.UNKNOWN_UNCERTAIN
    assert res_mod_py.is_uncertain is True

    # 5. Valid empty Python file
    rec_empty = GitChangeRecord(
        "M", "addons/a/__init__.py", "addons/a/__init__.py", "100644", "100644", "a" * 40, "b" * 40, "M"
    )
    owned_empty = OwnedChangeRecord(rec_empty, "a", "a")
    res_empty = classify_file_change(owned_empty, lambda p: b"", lambda p: b"", set())
    assert res_empty.category == ChangeCategory.METHOD_ONLY
    assert res_empty.is_uncertain is False
    assert res_empty.module_update is False

    # 6. Invalid encoding in Python file
    rec_enc = GitChangeRecord(
        "M", "addons/a/models.py", "addons/a/models.py", "100644", "100644", "a" * 40, "b" * 40, "M"
    )
    owned_enc = OwnedChangeRecord(rec_enc, "a", "a")
    res_enc = classify_file_change(owned_enc, lambda p: b"x = 1", lambda p: b"\xff\xfe\x00\x00invalid", set())
    assert res_enc.category == ChangeCategory.UNKNOWN_UNCERTAIN
    assert res_enc.is_uncertain is True
    assert "Could not decode" in res_enc.reason
