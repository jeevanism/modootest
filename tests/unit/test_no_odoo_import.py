"""Unit test verifying that no Odoo modules are imported during agent, plan, selection, or watch discovery."""

from __future__ import annotations

import sys


def test_no_odoo_import_in_modootest_agent_and_watch() -> None:
    # Ensure odoo is not imported before
    assert "odoo" not in sys.modules

    import modootest.agent
    import modootest.agent.schemas
    import modootest.agent.serializers
    import modootest.cli.main
    import modootest.intelligence.classifier
    import modootest.intelligence.git
    import modootest.intelligence.manifest
    import modootest.intelligence.planner
    import modootest.intelligence.selector
    import modootest.watch.runner
    import modootest.watch.snapshot
    import modootest.watch.supervisor

    # Verify odoo was never imported
    assert "odoo" not in sys.modules
    assert not any(mod.startswith("odoo.") for mod in sys.modules)
