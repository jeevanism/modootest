"""pytest plugin registration and CLI options for modootest."""
import pytest
from modootest.pytest_plugin.fixtures import (
    _odoo_tx,
    as_company,
    as_user,
    assert_max_queries,
    freeze_time,
    mock_http,
    odoo_cr,
    odoo_env,
    odoo_factory,
    odoo_registry,
    query_count,
)


def pytest_addoption(parser):
    """Add --modootest-config option to pytest CLI."""
    group = parser.getgroup("modootest", "modootest Odoo testing framework")
    group.addoption(
        "--modootest-config",
        action="store",
        default=None,
        help="Path to Odoo configuration file for modootest activation.",
    )


def _initialize_odoo(config, config_path):
    if hasattr(config, "_modootest_params"):
        return
    from modootest.adapter.v19.bootstrap import init_odoo_config
    from modootest.pytest_plugin.collection import install_addon_imports

    params = init_odoo_config(config_path)
    install_addon_imports(config, params.get("addons_paths", ()))
    config._modootest_params = params


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config):
    """Prepare canonical imports before pytest loads addon-local conftests."""
    config_path = getattr(early_config.known_args_namespace, "modootest_config", None)
    if config_path:
        _initialize_odoo(early_config, config_path)


def pytest_configure(config):
    """Also support explicit plugin registration after initial conftest loading."""
    config_path = config.getoption("--modootest-config")
    if config_path:
        _initialize_odoo(config, config_path)
