# modootest

**modootest (Modern Odoo Test Framework)** is a pytest framework designed for
Odoo 19 custom development. It helps developers test backend behavior safely,
repeatably, and without manually rebuilding the entire Odoo test workflow for
every change.

## Why use modootest?

Custom Odoo development combines Python models, ORM behavior, permissions,
record rules, users, companies, and database transactions. Traditional Odoo
testing can require sharing a database, restarting the Odoo server, or upgrading
modules between changes. `modootest` makes this work more productive with a
focused RED → GREEN workflow and automatic transaction cleanup after each test.

## What it provides

- Isolated Odoo transactions and managed cursors.
- Odoo environment, record factory, user, and company fixtures.
- Permission, record-rule, and multi-company test support.
- Query counting and query-budget assertions.
- HTTP request mocking and frozen time helpers.
- Change planning and impacted-test selection.
- Pytest-compatible execution and deterministic JSON output for tooling.

## Typical use

Write a focused test for a model method, computed field, constraint, workflow,
permission rule, multi-company requirement, or backend integration. Run it to
confirm the expected RED result, implement the addon change, and run it again for
GREEN. `modootest` rolls back database changes after each test so tests remain
safe and repeatable.

## Installation

Install `modootest` into the same Python environment that imports Odoo and runs
pytest:

```bash
python -m pip install modootest
```

Supported environment: Odoo 19 with Python 3.10–3.13. Odoo itself is not
installed as a dependency; use your existing Odoo checkout and test database.

For complete setup instructions, fixtures, configuration, and test execution,
read the [GitHub README](https://github.com/jeevanism/modootest/blob/main/README.md).

`modootest` does not replace browser, JavaScript, Owl, CSS, visual UI, or browser
tour tests. Those should use Odoo's frontend testing tools.

Licensed under the [Mozilla Public License 2.0](https://www.mozilla.org/en-US/MPL/2.0/).
