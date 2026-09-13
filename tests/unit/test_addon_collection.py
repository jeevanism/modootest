"""Canonical addon collection without a database or an installed Odoo server."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import _pytest.pathlib

from modootest.pytest_plugin.collection import install_addon_imports


def make_addon(root, name):
    addon = root / name
    (addon / "models").mkdir(parents=True)
    (addon / "tests").mkdir()
    (addon / "__manifest__.py").write_text("{'name': 'Collection regression'}")
    (addon / "__init__.py").write_text("from . import models\n")
    (addon / "models" / "__init__.py").write_text("from . import sale_order\n")
    (addon / "models" / "sale_order.py").write_text('''
import odoo
assert __name__.startswith("odoo.addons."), __name__
odoo.model_imports[__name__] = odoo.model_imports.get(__name__, 0) + 1
class SaleOrder:
    pass
''')
    (addon / "tests" / "__init__.py").write_text("")
    (addon / "tests" / "conftest.py").write_text('''
import pytest
from ..models.sale_order import SaleOrder
assert __name__.startswith("odoo.addons."), __name__
@pytest.fixture
def model_class():
    return SaleOrder
''')
    (addon / "tests" / "test_sale.py").write_text(f'''
import importlib
import sys
import odoo
from ..models.sale_order import SaleOrder

def test_identity(model_class, pytestconfig):
    canonical = importlib.import_module("odoo.addons.{name}.models.sale_order")
    assert SaleOrder is canonical.SaleOrder is model_class
    assert __name__ == "odoo.addons.{name}.tests.test_sale"
    assert odoo.model_imports[canonical.__name__] == 1
    assert not any(n == "{name}" or n.startswith("{name}.") for n in sys.modules)
    assert pytestconfig._modootest_params["db_name"] == "collection_testdb"
    assert odoo.bootstrap_calls == 1
''')
    return addon


@pytest.fixture
def configured_addons(pytester, monkeypatch):
    root = pytester.path / "custom-addons"
    first = make_addon(root, "sales_one")
    second = make_addon(root, "sales_two")
    conf = pytester.path / "modootest.conf"
    conf.write_text("[options]\ndb_name = collection_testdb\ndata_dir = /tmp/data\n")
    pytester.makepyfile(bootstrap_stub=f'''
import sys
from types import ModuleType
from modootest.adapter.v19 import bootstrap

def initialize(path):
    params = bootstrap.parse_ini_config(path)
    odoo = ModuleType("odoo")
    odoo.__path__ = []
    odoo.addons = ModuleType("odoo.addons")
    odoo.addons.__path__ = [{str(root)!r}]
    odoo.model_imports = {{}}
    odoo.bootstrap_calls = 1
    sys.modules["odoo"] = odoo
    sys.modules["odoo.addons"] = odoo.addons
    params["addons_paths"] = odoo.addons.__path__
    return params

bootstrap.init_odoo_config = initialize
''')
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((
        str(Path(__file__).resolve().parents[2] / "src"), str(pytester.path),
    )))
    return first, second, conf


@pytest.mark.parametrize("mode", ["prepend", "append", "importlib"])
@pytest.mark.parametrize("namespace", [False, True])
def test_addon_collection_modes(pytester, configured_addons, mode, namespace):
    first, second, conf = configured_addons
    ordinary = pytester.makepyfile(test_plain='''
def test_plain():
    assert __name__ == "test_plain"
''')
    result = pytester.runpytest_subprocess(
        "-p", "bootstrap_stub", "-p", "modootest.pytest_plugin.plugin",
        f"--modootest-config={conf}", f"--import-mode={mode}",
        "-o", f"consider_namespace_packages={namespace}",
        str(first / "tests" / "test_sale.py"), str(second), str(ordinary),
    )
    result.assert_outcomes(passed=3)


def test_collection_without_tests_init(pytester, configured_addons):
    first, _, conf = configured_addons
    (first / "tests" / "__init__.py").unlink()
    result = pytester.runpytest_subprocess(
        "-p", "bootstrap_stub", "-p", "modootest.pytest_plugin.plugin",
        f"--modootest-config={conf}", str(first),
    )
    result.assert_outcomes(passed=1)


def test_resolver_restored_after_session(tmp_path):
    addon = make_addon(tmp_path, "sales_one")
    cleanups = []
    config = SimpleNamespace(add_cleanup=cleanups.append)
    original = _pytest.pathlib.resolve_pkg_root_and_module_name
    try:
        install_addon_imports(config, [tmp_path])
        assert _pytest.pathlib.resolve_pkg_root_and_module_name(
            addon / "tests" / "test_sale.py"
        )[1] == "odoo.addons.sales_one.tests.test_sale"
    finally:
        for cleanup in reversed(cleanups):
            cleanup()
    assert _pytest.pathlib.resolve_pkg_root_and_module_name is original


@pytest.mark.parametrize("mode", ["prepend", "append", "importlib"])
def test_shadowed_addon_rejected(pytester, configured_addons, mode):
    _, _, conf = configured_addons
    shadow = make_addon(pytester.path / "other-addons", "sales_one")
    stub = pytester.path / "bootstrap_stub.py"
    stub.write_text(stub.read_text().replace(
        'params["addons_paths"] = odoo.addons.__path__',
        f'odoo.addons.__path__.append({str(shadow.parent)!r})\n'
        '    params["addons_paths"] = odoo.addons.__path__',
    ))
    result = pytester.runpytest_subprocess(
        "-p", "bootstrap_stub", "-p", "modootest.pytest_plugin.plugin",
        f"--modootest-config={conf}", f"--import-mode={mode}", str(shadow),
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "conflicts with Odoo's import path" in result.stderr.str()


def test_unconfigured_addon_has_actionable_error(pytester, configured_addons):
    _, _, conf = configured_addons
    outside = make_addon(pytester.path / "outside", "outside_sale")
    result = pytester.runpytest_subprocess(
        "-p", "bootstrap_stub", "-p", "modootest.pytest_plugin.plugin",
        f"--modootest-config={conf}", str(outside),
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "outside Odoo's configured addons_path" in result.stderr.str()
