# modootest

**modootest (Modern Odoo Test Framework)** is a pytest framework designed for
Odoo 19 custom development. It gives Odoo developers isolated transactions,
Odoo fixtures, user and company contexts, query-budget checks, HTTP mocking,
frozen time, and tools for planning and running only the tests affected by a
code change.

## Why use modootest?

Custom Odoo development combines Python models, ORM behavior, permissions,
record rules, and multiple companies. `modootest` makes these backend
requirements easier to test in a repeatable RED → GREEN workflow and rolls back
each test's database changes.

This improves productivity compared with traditional Odoo test workflows that
can require sharing a database, restarting the Odoo server, or upgrading a
module between changes. `modootest` is designed for model methods, computed
fields, constraints, workflows, permissions, multi-company rules, and backend
integrations. It does not replace browser, JavaScript, or visual UI tests.

## Installation

Install it into the same Python environment that imports Odoo and runs pytest:

```bash
/path/to/odoo-venv/bin/python -m pip install \
  "git+https://github.com/jeevanism/modootest.git@v1.0.1"
```

`modootest` supports Odoo 19 with Python 3.10 through 3.13. Odoo itself is not
installed as a dependency; use your existing Odoo environment.

## Developer documentation

For installation details, dedicated test-database setup, fixtures, writing your
first test, and focused test execution, see the [developer workflow documentation](https://github.com/jeevanism/modootest/blob/main/developer_workflow_tutorial.md).

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

## Typical workflow

1. Create or select a dedicated Odoo test database and install the addon under test.
2. Write one focused pytest function describing the required backend behavior.
3. Run the test and confirm the intentional RED result.
4. Implement the addon change and rerun for GREEN.
5. Use `modootest plan` and `modootest impacted` to understand and select tests affected by later changes.

Each test runs sequentially with transaction cleanup. Do not share the database
with another test process or use `pytest-xdist` workers against the same database.

## Scope and boundaries

`modootest` is intended for model methods, computed fields, constraints,
workflows, permissions, record rules, multi-company behavior, ORM operations,
and backend integrations. It does not replace browser, JavaScript, Owl, CSS,
visual UI, or browser-tour testing.

Database schema changes and addon installation or upgrades remain the
responsibility of the normal Odoo setup process. Direct database connections,
independent cursors, commits outside the managed cursor, and external side
effects bypass the isolation guarantees; use the supplied fixtures and mocks.

For complete configuration examples, troubleshooting, agent JSON output, and
the full RED → GREEN tutorial, see the [developer workflow documentation](https://github.com/jeevanism/modootest/blob/main/developer_workflow_tutorial.md).
