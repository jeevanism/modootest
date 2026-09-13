"""Resolve configured Odoo addons before pytest imports their packages."""

from importlib.machinery import PathFinder
from pathlib import Path
import sys

import pytest
import _pytest.pathlib


def install_addon_imports(config, addon_paths):
    """Scope pytest's package-name resolver to this configured test session.

    Pytest uses this resolver for test modules, package setup and conftests in
    all three import modes. Mapping names here retains its assertion rewriting
    and collectors instead of importing tests through a separate loader.
    """
    roots = tuple(dict.fromkeys(Path(path).resolve() for path in addon_paths))
    if not roots:
        return
    original = _pytest.pathlib.resolve_pkg_root_and_module_name

    def resolve(path, *, consider_namespace_packages=False):
        resolved = Path(path).resolve()
        addon = next(
            (parent for parent in resolved.parents
             if (parent / "__manifest__.py").is_file()
             and (parent / "__init__.py").is_file()),
            None,
        )
        if addon is None:
            return original(path, consider_namespace_packages=consider_namespace_packages)
        if addon.parent not in roots:
            raise pytest.UsageError(
                f"Addon '{addon.name}' is outside Odoo's configured addons_path: {addon}. "
                "Add its parent directory to addons_path in --modootest-config."
            )

        # Honor Odoo's import precedence, including addons already loaded by Odoo.
        # Otherwise importlib mode can silently collect a shadowed second copy.
        name = f"odoo.addons.{addon.name}"
        loaded = sys.modules.get(name)
        spec = PathFinder.find_spec(name, [str(root) for root in roots])
        origin = getattr(loaded, "__file__", None) if loaded else getattr(spec, "origin", None)
        if not origin or Path(origin).resolve() != addon / "__init__.py":
            raise pytest.UsageError(
                f"Addon '{addon.name}' at {addon} conflicts with Odoo's import path. "
                "Select the addon copy used by Odoo or correct addons_path precedence."
            )
        parts = list(resolved.relative_to(addon.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if not all(part.isidentifier() for part in parts):
            raise pytest.UsageError(f"Addon Python path contains an invalid module name: {resolved}")
        return addon.parent, "odoo.addons." + ".".join(parts)

    patch = pytest.MonkeyPatch()
    patch.setattr(_pytest.pathlib, "resolve_pkg_root_and_module_name", resolve)
    config.add_cleanup(patch.undo)
