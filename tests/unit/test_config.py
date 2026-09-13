"""Unit tests for configuration validation and plugin behavior without Odoo."""
from pathlib import Path
import traceback
import pytest
from modootest.adapter.v19.bootstrap import init_odoo_config, parse_ini_config


def test_plugin_inactivity_without_odoo(pytester, monkeypatch):
    """Verify child pytest process with plugin loaded but without --modootest-config does not import odoo."""
    pytester.makepyfile(
        """
        import sys
        def test_dummy():
            assert "odoo" not in sys.modules
        """
    )
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    result = pytester.runpytest_subprocess("-p", "modootest.pytest_plugin.plugin")
    result.assert_outcomes(passed=1)


def test_missing_config_file():
    """Verify init_odoo_config raises pytest.UsageError if config file does not exist."""
    with pytest.raises(pytest.UsageError, match="does not exist"):
        init_odoo_config("/nonexistent/path/to/modootest.conf")


def test_renamed_plugin_config_reaches_registry(pytester, monkeypatch):
    """Exercise plugin bootstrap and fixture state together without an Odoo server."""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    import os
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((
        str(Path(__file__).resolve().parents[2] / "src"), str(pytester.path),
    )))
    config_path = pytester.path / "modootest.conf"
    config_path.write_text("[options]\ndb_name = rename_testdb\ndata_dir = /tmp/data\n")
    pytester.makepyfile(bootstrap_stub="""
        from modootest.adapter.v19 import bootstrap, registry
        bootstrap.init_odoo_config = bootstrap.parse_ini_config
        registry.get_odoo_registry = lambda db_name: {"database": db_name}
        """)
    pytester.makepyfile(
        """
        import sys
        def test_registry(odoo_registry, pytestconfig):
            assert odoo_registry == {"database": "rename_testdb"}
            assert pytestconfig._modootest_params["db_name"] == "rename_testdb"
            assert "odoo" not in sys.modules
        """
    )
    result = pytester.runpytest_subprocess(
        "-p", "bootstrap_stub", "-p", "modootest.pytest_plugin.plugin",
        f"--modootest-config={config_path}",
    )
    result.assert_outcomes(passed=1)


def test_invalid_config_cases(tmp_path):
    """Verify INI config validation rules without touching database."""
    # Case 1: Missing db_name
    conf1 = tmp_path / "no_db.conf"
    conf1.write_text("[options]\ndata_dir = /tmp/data\n")
    with pytest.raises(pytest.UsageError, match="db_name specified"):
        parse_ini_config(str(conf1))

    # Case 2: Multiple db_names
    conf2 = tmp_path / "multi_db.conf"
    conf2.write_text("[options]\ndb_name = db1,db2\ndata_dir = /tmp/data\n")
    with pytest.raises(pytest.UsageError, match="Multiple databases"):
        parse_ini_config(str(conf2))

    # Case 3: Missing data_dir
    conf3 = tmp_path / "no_data_dir.conf"
    conf3.write_text("[options]\ndb_name = testdb\n")
    with pytest.raises(pytest.UsageError, match="data_dir specified"):
        parse_ini_config(str(conf3))

    # Case 4: Relative data_dir
    conf4 = tmp_path / "rel_data_dir.conf"
    conf4.write_text("[options]\ndb_name = testdb\ndata_dir = relative/path\n")
    with pytest.raises(pytest.UsageError, match="must be an absolute path"):
        parse_ini_config(str(conf4))

    # Case 5: Mutation flag init=base
    conf5 = tmp_path / "mutation_init.conf"
    conf5.write_text("[options]\ndb_name = testdb\ndata_dir = /tmp/data\ninit = base\n")
    with pytest.raises(pytest.UsageError, match="forbids database mutation flag 'init'"):
        parse_ini_config(str(conf5))


def test_config_password_with_percent(tmp_path):
    """Verify password containing '%' does not trigger ConfigParser interpolation error."""
    conf = tmp_path / "percent_pass.conf"
    conf.write_text("[options]\ndb_name = testdb\ndata_dir = /tmp/data\ndb_password = secret%123%pass\n")
    params = parse_ini_config(str(conf))
    assert params["db_name"] == "testdb"


def test_malformed_config_hides_secret_sentinel(tmp_path):
    """Verify malformed lines containing a secret sentinel do not leak into exception message."""
    conf = tmp_path / "malformed.conf"
    conf.write_text("[options]\nSECRET_SENTINEL_TOKEN_9999\n")
    with pytest.raises(pytest.UsageError) as exc_info:
        parse_ini_config(str(conf))
    assert "SECRET_SENTINEL_TOKEN_9999" not in str(exc_info.value)
    assert "SECRET_SENTINEL_TOKEN_9999" not in "".join(traceback.format_exception(exc_info.value))
    assert "Invalid INI syntax" in str(exc_info.value)


def test_missing_options_section(tmp_path):
    """Verify config missing [options] section raises UsageError."""
    conf = tmp_path / "no_options.conf"
    conf.write_text("[global]\ndb_name = testdb\ndata_dir = /tmp/data\n")
    with pytest.raises(pytest.UsageError, match="must contain an \\[options\\] section"):
        parse_ini_config(str(conf))


def test_fixture_requested_without_config(pytester, monkeypatch):
    """Verify requesting odoo_env fixture without --modootest-config gives setup ERROR."""
    pytester.makepyfile(
        """
        def test_requires_env(odoo_env):
            pass
        """
    )
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    result = pytester.runpytest("-p", "modootest.pytest_plugin.plugin")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*--mod*config*option was not provided*"])
