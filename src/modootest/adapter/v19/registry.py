"""Odoo 19 registry access adapter."""


def get_odoo_registry(db_name: str):
    """Retrieve or initialize session-scoped Odoo Registry for db_name."""
    if not isinstance(db_name, str) or not db_name:
        raise TypeError("db_name must be a non-empty string")
    from odoo.modules.registry import Registry

    registry = Registry(db_name)
    if not registry.ready:
        registry = Registry.new(db_name)
    return registry
