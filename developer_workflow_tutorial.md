# Developer workflow: testing Odoo 19 customisations with `modootest`

This guide explains when to use `modootest`, how to prepare Odoo safely, and how
to develop one backend behaviour at a time with a RED → GREEN → refactor cycle.

`modootest` runs pytest tests against an Odoo 19 registry and one explicitly
configured database. **Use separate development and test databases for the
smoothest everyday workflow:** test Python business-logic changes without
stopping or restarting your development Odoo web server. Each ordinary test run
loads the latest Python code in a fresh process. Database-backed changes still
require an addon upgrade in the test database before testing.
It provides isolated Odoo environments, factories, user and company
contexts, query assertions, HTTP mocking, frozen time, change planning, and
impacted-test selection.

It deliberately does not create databases or install, update, or uninstall Odoo
addons. Prepare those with the normal Odoo CLI before running modootest.

**Modootest's benefit is faster, repeatable testing of business logic,
permissions, and ORM behavior through fixtures and transaction cleanup. It is
not a replacement for Odoo's module lifecycle.** The shorter feedback loop comes
from focused automated tests and fresh Python processes, not from bypassing
database setup or promising that all changes need no upgrade.

## 1. Decide whether modootest fits the requirement

Use modootest for backend behaviour such as:

- calculations, totals, pricing, discounts, commissions, and taxes;
- model methods, constraints, defaults, computed fields, and workflows;
- permissions, record rules, user roles, and multi-company behaviour;
- ORM operations, transaction behaviour, and database query budgets;
- scheduled or integration logic that can use mocked outbound HTTP; and
- regression tests for existing backend customisations.

Modootest does not currently test browser rendering, XML view appearance, Owl or
JavaScript behaviour, CSS, drag-and-drop interactions, or full HTTP browser flows.
Use Odoo's frontend and tour testing tools for those requirements.

A simple field or label change may not need a modootest test. Add one when the
field has a default, computation, constraint, permission rule, migration effect,
or other backend behaviour worth protecting.

Before writing code, state the requirement as an observable outcome. For example:

> A sales representative cannot confirm an order containing a line discount over
> 15%, while a sales manager can override the limit.

## 2. Install modootest in the Odoo environment

### Identify the environment first

Before installing anything, identify the Odoo 19 source directory, the Python
3.12 virtual environment used by your running Odoo, its custom addon directory,
and the PostgreSQL connection settings. Replace every `/path/to/...` example
below with the corresponding absolute path on your machine.

`localhost:8019` is the browser address of an Odoo server, not a modootest
endpoint. Modootest imports Odoo in its own Python process and connects directly
to PostgreSQL. It does not need the HTTP server to be running. A working browser
session alone does not identify the interpreter or database to use for tests.

Use your existing Odoo Python 3.12 environment; you do not need another Odoo
installation or the modootest framework source repository to develop an addon.

Install `modootest` into the same Python environment that imports Odoo and runs
pytest. Do not install it into an unrelated global interpreter.

### Install from PyPI

`modootest` is available on [PyPI](https://pypi.org/project/modootest/). Install it with `pip`:

```bash
/path/to/odoo-venv/bin/python -m pip install modootest
```

Or pin a specific version:

```bash
/path/to/odoo-venv/bin/python -m pip install modootest==1.0.0
```

If you use `uv`:

```bash
uv pip install --python /path/to/odoo-venv/bin/python modootest
```

### Optional: Development installation from source

If you are developing or contributing to `modootest` itself, install a local checkout in editable mode:

```bash
/path/to/odoo-venv/bin/python -m pip install \
  -e /path/to/odoo-modootest-2026
```

Or with `uv`:

```bash
uv pip install --python /path/to/odoo-venv/bin/python \
  -e /path/to/odoo-modootest-2026
```

### Verify the installation

Run all three checks with the Odoo environment's interpreter:

```bash
/path/to/odoo-venv/bin/python -c \
  "import modootest; print(modootest.__version__, modootest.__file__)"

/path/to/odoo-venv/bin/modootest --help

/path/to/odoo-venv/bin/python -m pytest --help | \
  grep -- --modootest-config
```

The first command must show the expected version (`1.0.0` or later) and installation path. The
second must display the `modootest` CLI, and the third must find the pytest
configuration option. Fix the environment before continuing if any check fails.

## 3. Choose your database setup

### Recommended: two databases for uninterrupted development

**Use two databases to get the intended smooth testing workflow:** one
for browser development and demonstrations, and one disposable database for
tests. Your development web server can stay running against its own database
while modootest tests the latest Python business logic in a separate process
against the test database. This recommendation is about everyday productivity
as well as protecting development data.

Both databases can share the same PostgreSQL server, Odoo source, Python environment,
and custom addon code. You do not need a second PostgreSQL or Odoo installation.
Install or upgrade the addon in each database where you use it.

The scope of the no-restart/no-upgrade benefit is important:

| Change or action                                                                        | Required step with a separate test database                                                                                      |
| --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Change an existing Python business method, such as the discount guard                   | Start a fresh pytest run. No development web-server restart or addon upgrade is needed for the test.                             |
| Add or change database-backed definitions, such as fields, XML records, or access rules | Upgrade the addon in the test database before testing.                                                                           |
| Install a new addon                                                                     | Install it in the test database before the first test.                                                                           |
| View changed Python behavior in the browser                                             | Restart or reload that browser server; a test run does not reload it. Apply database-backed updates to its database when needed. |

Two databases do not eliminate Odoo's installation and upgrade requirements.
They let the browser workflow and test workflow proceed independently. A running
development server may still serve older Python code until reloaded, even when
the latest code passes tests.

### What you gain, what it costs, and why

For an installed addon whose database definitions are already up to date, you
can edit an existing Python method and run a focused test immediately. Fixtures
create the required records and user contexts, assertions check the behavior,
and managed transaction cleanup removes test changes afterward. This reduces
repeated manual setup and browser interactions and makes the same scenarios
repeatable as regression tests. Ordinary Python-only edits already do not
require an Odoo addon upgrade; modootest makes testing those edits convenient
without reloading the development web server.

Each ordinary pytest invocation loads Odoo and the current addon code in a fresh
process. There is no continuously running test web server to stop and restart.
However, loading the registry still takes time: this is database-backed testing,
not instant hot reload or a guarantee that every test runs faster.

The two-database setup has real costs:

- **Initial setup and storage:** provision a disposable database, configure it,
  and install the addon and its dependencies there. Install the addon in the
  development database too when you need browser access.
- **Database maintenance:** changes to fields, XML records, access rules, or
  other database-backed definitions require an upgrade in each database that
  will use those changes. Upgrade the test database before testing; upgrade the
  development database before using those changes in the browser. This is not
  required after every Python edit.
- **Separate data and settings:** test and browser databases do not automatically
  synchronize. Set up relevant test configuration explicitly and keep it
  representative. Records created by tests are rolled back, so browser demos
  need their own records.
- **Browser refresh remains separate:** successful tests do not reload a running
  Odoo web server. Restart or reload it when you want to see changed Python
  behavior in the UI.

These costs follow from Odoo's architecture: installed-module state, schema,
and configuration records belong to each database, while Python code is loaded
into each process. A fresh test process reads changed Python code, but it does
not perform the installation or upgrade operations needed to bring database
definitions up to date. Test rollback also does not undo ordinary addon
installation or upgrades.

For the discount-guard example, install the scaffold once, then repeat
`edit Python → run pytest → inspect result`. Addon upgrades are unnecessary
while those edits only change the confirmation method. If you later introduce
a stored approval field or new access rules, upgrade the test database before
testing and the development database before demonstrating those additions.

Choose this workflow for repeatable backend checks and an independent testing
loop. Expect the greatest convenience during business-logic iterations; work
dominated by schema or XML changes still involves Odoo's normal upgrade steps.

### Optional: one shared disposable local database

Two databases are not technically mandatory. Modootest requires exactly one
database selected in its configuration; it does not require or check for a
second development database.

You can share one disposable local development database, but this sacrifices
the convenience of leaving the browser server running during tests. Follow the
stop/test/restart workflow: stop the Odoo web server, scheduled jobs, and any
other database users; install or upgrade the addon if the changes require it;
run tests; then restart the server for browser use. Python-only method changes
still do not require an addon upgrade merely because the database is shared.

For frequent development, choose two databases to avoid those repeated server
interruptions. The shared option retains fixtures, assertions, and managed test
transaction cleanup, but loses this key workflow advantage. The database must
contain only data you can afford to lose. Modootest rolls back changes through
its managed test transactions, but addon installations/upgrades, independent
database connections, and external side effects are outside that protection.

Never run these tests against production, staging, or valuable data, and do not
clone production data into the test database. The examples below use
`modest_testdb` for the recommended separate test database. For the shared local
option, consistently substitute your chosen disposable database name, including
in `db_name`, CLI `-d` arguments, and the browser server's `--db-filter`.

### Create the workspace folders

Keep your consumer addon outside the modootest package repository. An example
workspace is:

```text
sales-discount-demo/
├── .gitignore
├── custom-addons/
│   └── custom_sales/          # The sales-discount guard addon
├── config/
│   └── modootest.conf         # Local credentials; never commit
├── test-data/                # Dedicated Odoo data_dir; never commit
└── logs/                     # Local installation/test logs
```

From your chosen demo workspace, create the directories:

```bash
mkdir -p custom-addons/custom_sales/models custom-addons/custom_sales/tests \
  config test-data logs
```

Add these entries to that workspace's `.gitignore` before creating the config:

```gitignore
config/*.conf
test-data/
logs/
__pycache__/
.pytest_cache/
*.pyc
```

This guide calls the addon `custom_sales`; it implements the proposed
sales-discount guard. If you choose `sale_discount_guard` instead, replace
`custom_sales` consistently in folders, installation commands, and test paths.

### Use a separate Odoo configuration

Use a separate test configuration for clarity, whichever database setup you
choose. A separate configuration file does not require a separate database.

Create an untracked Odoo configuration file such as
`/absolute/path/modootest.conf` (for the layout above, use
`/absolute/path/sales-discount-demo/config/modootest.conf` consistently):

```ini
[options]
addons_path = /path/to/odoo-src/odoo/addons,/path/to/odoo-src/addons,/path/to/custom-addons
db_name = modest_testdb
data_dir = /absolute/path/to/dedicated-test-data
db_host = 127.0.0.1
db_port = 5432
db_user = odoo
db_password = replace-with-local-test-credential
```

The configuration must satisfy these rules:

- `db_name` identifies exactly one database selected for testing;
- `data_dir` is an explicit absolute path for that database's Odoo data;
- `addons_path` contains Odoo's addon roots and the parent directory of every
  custom addon under test; and
- the file does not contain `init`, `update`, or `uninstall` options. Modootest
  rejects database mutation flags.

Keep credentials outside Git. Modootest executes tests sequentially; do not use
pytest-xdist or run concurrent test processes against the same database.

Create this extra configuration separately from the one serving
`localhost:8019`. Reuse the necessary PostgreSQL connection settings privately,
and explicitly select the chosen database, the demo addon root, and its absolute
`data_dir`. For a separate test database, use a separate data directory. For a
shared disposable database, use the same `data_dir` as its browser server so
existing filestore attachments remain accessible. Do not copy module-update
options into the test configuration. Restrict the local config's file permissions:

```bash
chmod 600 /absolute/path/modootest.conf
```

Creating the configuration file does not create the database. If you choose a
new separate database, use your normal
local Odoo/PostgreSQL setup to provision an empty `modest_testdb` owned by the
configured database role, then initialize Odoo's base module once:

```bash
PYTHONPATH=/path/to/odoo-src \
/path/to/odoo-venv/bin/python /path/to/odoo-src/odoo-bin \
  -c /absolute/path/modootest.conf -d modest_testdb \
  -i base --without-demo=all --no-http --stop-after-init \
  > logs/database-init.log 2>&1
```

Run this yourself from the demo workspace, and check the exit status immediately
with `echo $?`; zero indicates success. Skip provisioning and initialization if
your chosen database already exists and is initialized, including when reusing
a disposable local development database. Database provisioning may require help from
your local PostgreSQL administrator; modootest does not perform it.

Do not let a web server, scheduled jobs, or another test runner use this database
while tests run. Your existing server can remain running if it uses a different
database and cannot run jobs against `modest_testdb`.

## 4. Prepare the custom addon

For a new addon, create the ordinary Odoo package structure with a plural
`tests/` directory:

```text
custom-addons/custom_sales/
├── __init__.py
├── __manifest__.py
├── models/
│   ├── __init__.py
│   └── sale_order.py
└── tests/
    ├── __init__.py
    └── test_sale_discount.py
```

An initial model scaffold can contain no business rule:

```python
from odoo import models


class SaleOrder(models.Model):
    _inherit = "sale.order"
```

Save that scaffold in `models/sale_order.py`. The remaining minimum files are:

`__manifest__.py`:

```python
{
    "name": "Sales Discount Guard",
    "version": "19.0.1.0.0",
    "depends": ["sale"],
    "license": "LGPL-3",
    "installable": True,
    "application": False,
}
```

The license above is an example for this new demo addon, independent of
modootest's MPL-2.0 license; choose it deliberately before distributing the addon.

Addon-root `__init__.py`:

```python
from . import models
```

`models/__init__.py`:

```python
from . import sale_order
```

Create an empty `tests/__init__.py`. Add the test from section 6 to
`tests/test_sale_discount.py`; pytest will discover it. Do not import the pytest
test module from the addon-root `__init__.py`. No additional `conftest.py`,
`pytest.ini`, or XML files are needed for this minimal example: modootest supplies
the fixtures through its installed pytest plugin.

For an existing addon, keep its normal structure and add test files under its
`tests/` directory. Confirm that the exact addon copy selected by Odoo's
`addons_path` is the one you are editing. Modootest imports configured addons as
`odoo.addons.<addon>.*`; consumer models do not need an explicit `_module`
workaround.

## 5. Install the addon before the first test run

The custom addon must already be installed in the selected database. Installing
modootest as a Python package does not install the Odoo addon.

For a new addon, install the scaffold once with Odoo's normal CLI:

```bash
PYTHONPATH=/path/to/odoo-src \
/path/to/odoo-venv/bin/python /path/to/odoo-src/odoo-bin \
  -c /absolute/path/modootest.conf \
  -d modest_testdb \
  -i custom_sales \
  --no-http --stop-after-init \
  > logs/addon-install.log 2>&1
```

Run this from the demo workspace and check `echo $?` immediately afterward.
The `sale` manifest dependency installs Sales and its dependencies as needed.
Keep the model scaffold free of the discount guard until the first RED test.

For an existing addon, verify that it is installed in the selected database.
Use Odoo `-u custom_sales --stop-after-init` before pytest when your changes add
or alter fields, models, XML, access controls, record rules, manifest data, or
other database-backed definitions.

Python-only method changes normally need a new pytest process, not an addon
upgrade. Modootest starts a fresh process for each ordinary pytest command.

If the addon is not installed, Odoo may load only the inherited core model. A
test can then fail because the custom method is absent even though collection
works. That is an environment preparation problem, not the intended RED result.

## 6. Write one test first

Start with one observable behaviour. This example specifies that an ordinary
sales representative cannot confirm an order with a discount over 15%:

```python
import pytest
from modootest.developer.factory import RecordFactory, Sequence
from odoo.exceptions import UserError


class PartnerFactory(RecordFactory):
    _model = "res.partner"
    name = Sequence(lambda number: f"Test Customer {number}")
    customer_rank = 1


class ProductFactory(RecordFactory):
    _model = "product.product"
    name = Sequence(lambda number: f"Test Product {number}")
    type = "consu"
    list_price = 100.0


def test_excessive_discount_is_blocked(
    odoo_env,
    odoo_factory,
    as_user,
):
    customer = odoo_factory.create(PartnerFactory)
    product = odoo_factory.create(ProductFactory)
    sales_rep = odoo_env["res.users"].create({
        "name": "Test Sales Representative",
        "login": "test_sales_rep",
        "email": "test-sales-rep@example.com",
        "group_ids": [(
            6,
            0,
            [odoo_env.ref("sales_team.group_sale_salesman").id],
        )],
    })
    order = odoo_env["sale.order"].create({
        "partner_id": customer.id,
        "user_id": sales_rep.id,
        "order_line": [(0, 0, {
            "product_id": product.id,
            "product_uom_qty": 1.0,
            "price_unit": 100.0,
            "discount": 20.0,
        })],
    })

    with as_user(sales_rep) as sales_env:
        sales_order = sales_env["sale.order"].browse(order.id)
        with pytest.raises(UserError, match="exceeding the 15.0% limit"):
            sales_order.action_confirm()

    assert order.state == "draft"
```

Use the `as_user` pytest fixture by including it in the test function arguments.
The fixture yields an environment for the selected user. Browse the record again
through that environment before invoking the behaviour under test.

## 7. Run the focused test and confirm RED

From the repository containing `custom-addons/`, run the single test node:

```bash
PYTHONPATH=/path/to/odoo-src \
/path/to/odoo-venv/bin/python -m pytest \
  --modootest-config=/absolute/path/modootest.conf \
  custom-addons/custom_sales/tests/test_sale_discount.py::test_excessive_discount_is_blocked \
  -vv
```

If `uv run` manages the same Odoo environment, the equivalent command is:

```bash
uv run pytest \
  --modootest-config=/absolute/path/modootest.conf \
  custom-addons/custom_sales/tests/test_sale_discount.py::test_excessive_discount_is_blocked \
  -vv
```

Before implementing the rule, the meaningful RED result is:

```text
Failed: DID NOT RAISE UserError
```

This proves that the test reached `action_confirm()` and that the required rule
does not exist yet.

Do not accept every failure as RED. Fix the test environment first when pytest
reports collection errors, missing addons, invalid external IDs, access errors,
bad fixture usage, database connection failures, or syntax errors. The RED result
must fail for the missing business behaviour described by the requirement.

## 8. Add the smallest production implementation

Implement only the behaviour required by the failing test:

```python
from odoo import _, models
from odoo.exceptions import UserError

MAX_DISCOUNT_PERCENT = 15.0


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def action_confirm(self):
        if not self.env.user.has_group("sales_team.group_sale_manager"):
            for order in self:
                for line in order.order_line:
                    if line.discount > MAX_DISCOUNT_PERCENT:
                        raise UserError(
                            _(
                                "Line '%s' has a discount of %.1f%%, exceeding "
                                "the %.1f%% limit. Approval by a Sales Manager "
                                "is required."
                            )
                            % (line.name, line.discount, MAX_DISCOUNT_PERCENT)
                        )

        return super().action_confirm()
```

Because this is a Python-only method change, start a new pytest process. Do not
run `-u` solely for this change.

## 9. Rerun the focused test and confirm GREEN

Run the exact same focused pytest command. The expected result is:

```text
1 passed
```

If it fails, diagnose the reason before changing the requirement. Correct an
invalid fixture or test setup when necessary, but do not weaken a valid assertion
just to make the test pass.

Once the first behaviour is green, add the next behaviour one at a time. Useful
follow-up cases for this example are:

1. A discount exactly at 15% succeeds.
2. A zero-discount order succeeds.
3. A sales manager can confirm an order over the limit.
4. An order with several lines is blocked when any line exceeds the limit.

For each behaviour, repeat:

```text
Write one test → verify expected RED → implement → verify GREEN → refactor
```

After focused tests pass, run the complete addon test directory:

```bash
PYTHONPATH=/path/to/odoo-src \
/path/to/odoo-venv/bin/python -m pytest \
  --modootest-config=/absolute/path/modootest.conf \
  custom-addons/custom_sales/tests \
  -vv
```

Test records created through modootest fixtures are rolled back after each test.
Addon installation and upgrades are ordinary Odoo database operations and are
not rolled back by modootest.

## 10. Use change planning and impacted testing

From the Git repository containing the custom addons, inspect the current change:

```bash
modootest plan \
  --repo . \
  --addons-path custom-addons \
  --working-tree
```

Preview the tests selected by safe impacted mode:

```bash
modootest impacted \
  --repo . \
  --addons-path custom-addons \
  --working-tree \
  --mode safe
```

Execute the selected tests sequentially:

```bash
PYTHONPATH=/path/to/odoo-src modootest test --impacted \
  --repo . \
  --addons-path custom-addons \
  --working-tree \
  --mode safe \
  --execute \
  --modootest-config=/absolute/path/modootest.conf \
  --pytest-arg=-vv
```

Review the plan before execution. `Fresh Python process: REQUIRED` means rerun
pytest so Odoo imports current Python code. A module-update target means run the
corresponding Odoo `-u` operation against the selected database before testing.

## 11. Troubleshooting

### `Invalid import ... should start with 'odoo.addons'`

Confirm that you are using a modootest version containing canonical addon
collection support, that the addon has `__manifest__.py`, and that its parent
directory appears in the configuration's `addons_path`. Do not add `_module` to
consumer models as a workaround.

### `DID NOT RAISE` after adding the implementation

Check that the addon is installed in the configured database and that Odoo
selected the addon copy you edited. Start a new pytest process. If the change
introduced fields, XML, security, or data, upgrade the addon with Odoo before
rerunning pytest.

### `External ID not found`

The test references an XML ID absent from the database or Odoo version. Verify it
in the relevant Odoo source or installed module. This is test setup failure, not
the desired RED result.

### Access or record-rule errors

Create records with ownership and company values appropriate for the user under
test. Switch with the `as_user` or `as_company` fixture, then browse the record
again through the yielded environment.

### Test path not found

Paths are resolved from the current working directory. From the Odoo root, use
`custom-addons/custom_sales/tests`. From inside `custom-addons`, use
`custom_sales/tests` without repeating `custom-addons/`. Absolute paths also work.

### Odoo configuration warnings

Warnings about obsolete Odoo configuration keys are separate from modootest test
failures. Remove obsolete keys from the local test configuration when practical,
but diagnose failures using the final pytest error and traceback.

## 12. Workflow checklist

- [ ] Decide that the requirement concerns backend behaviour supported by modootest.
- [ ] Identify the Odoo source path and its Python 3.12 interpreter.
- [ ] Create the demo workspace, addon directories, local log/data folders, and ignore rules.
- [ ] Install modootest in the same Python environment as Odoo and pytest.
- [ ] Verify the import, CLI, pytest plugin, and `--modootest-config` option.
- [ ] Use separate development/test databases for uninterrupted testing, or accept the stop/test/restart tradeoff of a shared disposable database.
- [ ] Create an untracked configuration selecting exactly that database and its data directory.
- [ ] Provision and initialize the database if needed; exclude concurrent server/job access.
- [ ] Put the custom addon parent directory in `addons_path`.
- [ ] Create or inspect the addon's plural `tests/` directory.
- [ ] Add the manifest with its Sales dependency and the model import files.
- [ ] Install a new addon scaffold with Odoo `-i`, or confirm an existing addon is installed.
- [ ] Write one focused test before adding the business implementation.
- [ ] Run it and verify that RED is caused by the missing business behaviour.
- [ ] Implement the smallest production change.
- [ ] Run a fresh pytest process and verify GREEN.
- [ ] Add the next behaviour and repeat the cycle.
- [ ] Use Odoo `-u` before pytest when database-backed definitions change.
- [ ] Run the full addon tests, then review `modootest plan` and impacted selection.
- [ ] Keep all tests sequential and keep production data out of the test database.
