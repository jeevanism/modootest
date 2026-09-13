"""Odoo 19 bootstrap configuration and initialization."""
import configparser
import os
import pytest


def parse_ini_config(config_path: str) -> dict:
    """Validate explicit INI configuration BEFORE Odoo defaults are applied."""
    if not os.path.exists(config_path):
        raise pytest.UsageError(f"Odoo configuration file does not exist: {config_path}")

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            parser.read_file(f)
    except OSError:
        raise pytest.UsageError(f"Odoo configuration file does not exist or is unreadable: {config_path}") from None
    except (configparser.Error, UnicodeError):
        raise pytest.UsageError(f"Failed to parse Odoo configuration file {config_path}: Invalid INI syntax.") from None

    if not parser.has_section("options"):
        raise pytest.UsageError("Odoo configuration file must contain an [options] section.")

    db_name = parser.get("options", "db_name", fallback="").strip()
    if not db_name:
        raise pytest.UsageError("No explicit db_name specified in --modootest-config file.")
    if "," in db_name or len(db_name.split()) > 1:
        raise pytest.UsageError("Multiple databases specified in db_name; modootest requires exactly one database.")

    data_dir = parser.get("options", "data_dir", fallback="").strip()
    if not data_dir:
        raise pytest.UsageError("No explicit data_dir specified in --modootest-config file.")
    if not os.path.isabs(data_dir):
        raise pytest.UsageError(f"data_dir in --modootest-config must be an absolute path, got: {data_dir}")

    for flag in ("init", "update", "uninstall"):
        val = parser.get("options", flag, fallback="").strip()
        if val and val.lower() != "false" and val != "{}":
            raise pytest.UsageError(f"modootest forbids database mutation flag '{flag}' in config.")

    return {"db_name": db_name, "data_dir": data_dir}


def init_odoo_config(config_path: str) -> dict:
    """Parse Odoo configuration file and validate runtime parameters."""
    ini_params = parse_ini_config(config_path)

    try:
        from odoo import release
        from odoo.tools import config
    except ImportError as e:
        raise pytest.UsageError(f"Failed to import Odoo framework: {e}")

    if getattr(release, "version_info", None):
        major_version = release.version_info[0]
        if major_version != 19:
            raise pytest.UsageError(
                f"modootest requires Odoo 19, found Odoo major version {major_version}"
            )
    else:
        raise pytest.UsageError("Unable to determine Odoo release version.")

    try:
        config.parse_config(["-c", config_path])
    except (Exception, SystemExit):
        raise pytest.UsageError("Odoo could not load the specified configuration.") from None

    raw_db = config["db_name"]
    if isinstance(raw_db, list):
        if len(raw_db) != 1:
            raise pytest.UsageError("Effective db_name must specify exactly one database.")
        eff_db = raw_db[0]
    elif isinstance(raw_db, str) and raw_db:
        if "," in raw_db or len(raw_db.split()) > 1:
            raise pytest.UsageError("Effective db_name must specify exactly one database.")
        eff_db = raw_db
    else:
        raise pytest.UsageError("Effective db_name missing or invalid.")

    if eff_db != ini_params["db_name"]:
        raise pytest.UsageError(
            f"Effective db_name '{eff_db}' does not match configuration value '{ini_params['db_name']}'."
        )

    eff_data_dir = config["data_dir"]
    if not eff_data_dir or eff_data_dir != ini_params["data_dir"]:
        raise pytest.UsageError(
            f"Effective data_dir '{eff_data_dir}' does not match configuration value '{ini_params['data_dir']}'."
        )

    for flag in ("init", "update", "uninstall"):
        try:
            eff_val = config[flag]
        except KeyError:
            eff_val = None

        if eff_val:
            if isinstance(eff_val, dict):
                if any(eff_val.values()):
                    raise pytest.UsageError(f"modootest forbids database mutation flag '{flag}' in effective config.")
            elif isinstance(eff_val, (list, set, tuple)):
                if len(eff_val) > 0:
                    raise pytest.UsageError(f"modootest forbids database mutation flag '{flag}' in effective config.")
            elif isinstance(eff_val, str):
                if eff_val.strip() and eff_val.strip().lower() != "false" and eff_val.strip() != "{}":
                    raise pytest.UsageError(f"modootest forbids database mutation flag '{flag}' in effective config.")
            elif bool(eff_val) is True:
                raise pytest.UsageError(f"modootest forbids database mutation flag '{flag}' in effective config.")

    # parse_config initializes Odoo's namespace, including core and custom roots.
    import odoo.addons

    return {
        "db_name": eff_db,
        "data_dir": eff_data_dir,
        "addons_paths": tuple(odoo.addons.__path__),
    }
