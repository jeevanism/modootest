# modootest

**modootest (Modern Odoo Test Framework)** is a pytest framework designed for
Odoo 19 custom development. It gives Odoo developers isolated transactions,
Odoo fixtures, user and company contexts, query-budget checks, HTTP mocking,
frozen time, and tools for planning changes and selecting tests from affected
addons and their dependencies.

## Why use modootest?

Custom Odoo development combines Python models, ORM behavior, permissions,
record rules, and multiple companies. `modootest` makes these backend
requirements easier to test in a repeatable RED → GREEN workflow.

- **Less repeated manual setup:** fixtures create records and user/company
  contexts, while managed transactions roll back test changes afterward.
- **Focused feedback on business logic:** each ordinary test run loads current
  Python code in a fresh process. With a separate test database, your development
  web server can keep running while you test Python-method changes.
- **Repeatable regression checks:** verify permissions, calculations, workflows,
  and ORM behavior with assertions instead of repeating browser interactions.
- **Guidance on what to test and update:** AST-based change analysis recommends
  lifecycle actions; impacted-test selection works at addon/test-file level and
  can include downstream dependencies. Safe mode broadens selection when the
  analysis is uncertain.

Modootest complements Odoo's module lifecycle. Python-only method edits normally
need no addon upgrade, but database-backed changes still do. It does not reload
the browser server or replace frontend testing.

## Installation

Available on [PyPI](https://pypi.org/project/modootest/), including
[version 1.0.0](https://pypi.org/project/modootest/1.0.0/). Install into the same
Python environment that imports Odoo:

```bash
/path/to/odoo-venv/bin/python -m pip install modootest
```

Or use `uv`:

```bash
uv pip install --python /path/to/odoo-venv/bin/python modootest
```

The package targets Odoo 19 and declares Python 3.10 through 3.13 compatibility,
including Python 3.12. Odoo itself is not installed as a dependency; use your
existing Odoo environment.

## Developer documentation

For environment setup, database choices and tradeoffs, addon installation, and
a complete sales-discount RED → GREEN example, see the
[developer workflow documentation](developer_workflow_tutorial.md).

## License

modootest is distributed under the [Mozilla Public License 2.0](https://www.mozilla.org/en-US/MPL/2.0/).

## Developer fixtures

| Fixture | Purpose |
| --- | --- |
| `odoo_env` | Function-scoped Odoo environment with transaction cleanup. |
| `odoo_cr` | Managed cursor that blocks unsafe direct commit and rollback operations. |
| `odoo_factory` | Build, draft, or create Odoo records with reusable factories. |
| `as_user` | Exercise permissions as an Odoo user with `su=False`. |
| `as_company` | Run business logic with an explicit active company. |
| `query_count` | Measure SQL queries within a block. |
| `assert_max_queries` | Fail when a block exceeds its query budget. |
| `mock_http` | Mock outbound HTTP requests and assert request details. |
| `freeze_time` | Freeze Python and Odoo date/time helpers within a block. |

## Scope and boundaries

`modootest` is intended for model methods, computed fields, constraints,
workflows, permissions, record rules, multi-company behavior, ORM operations,
and backend integrations. It does not replace browser, JavaScript, Owl, CSS,
visual UI, or browser-tour testing.

Database schema changes and addon installation or upgrades remain the
responsibility of the normal Odoo setup process. Direct database connections,
independent cursors, commits outside the managed cursor, and external side
effects bypass the isolation guarantees; use the supplied fixtures and mocks.

Tests run sequentially. Use disposable data and avoid concurrent server/test
access to the test database. A separate test database is recommended for smooth
development; the tutorial explains the optional shared-database setup.
