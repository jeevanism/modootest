# Developer workflow: testing Odoo 19 customisations with `modootest`

This guide explains when to use `modootest`, how to prepare Odoo safely, and how
to develop one backend behaviour at a time with a RED → GREEN → refactor cycle.

`modootest` runs pytest tests against an Odoo 19 registry and one dedicated test
database. It provides isolated Odoo environments, factories, user and company
contexts, query assertions, HTTP mocking, frozen time, change planning, and
impacted-test selection.

It deliberately does not create databases or install, update, or uninstall Odoo
addons. Prepare those with the normal Odoo CLI before running modootest.

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

Install modootest into the same Python environment that imports Odoo and runs
pytest. Do not install it into an unrelated global interpreter.

### Current pre-PyPI installation

The package has not yet been published to PyPI. Install the current development
version from Git:

```bash
uv pip install --python /path/to/odoo-venv/bin/python \
  "git+https://github.com/jeevanism/modest.git@main"
```

Or install a local checkout in editable mode while developing modootest itself:

```bash
uv pip install --python /path/to/odoo-venv/bin/python \
  -e /path/to/odoo-modest-framework
```

The equivalent local pip command is:

```bash
/path/to/odoo-venv/bin/python -m pip install \
  -e /path/to/odoo-modest-framework
```

After the package is published, the intended commands will be:

```bash
uv pip install --python /path/to/odoo-venv/bin/python modootest

# Or:
/path/to/odoo-venv/bin/python -m pip install modootest
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

The first command must show the expected version and installation path. The
second must display the `modootest` CLI, and the third must find the pytest
configuration option. Fix the environment before continuing if any check fails.

## 3. Prepare one dedicated test database

Use a disposable database created specifically for sequential tests. Never point
modootest at production or staging data, and do not clone production data into the
test database.

Create an untracked Odoo configuration file such as
`/absolute/path/modootest.conf`:

```ini
[options]
addons_path = /path/to/odoo-src/odoo/addons,/path/to/odoo-src/addons,/path/to/custom-addons
db_name = modootest_testdb
data_dir = /absolute/path/to/dedicated-test-data
db_host = 127.0.0.1
db_port = 5432
db_user = odoo
db_password = replace-with-local-test-credential
```

The configuration must satisfy these rules:

- `db_name` identifies exactly one dedicated test database;
- `data_dir` is an absolute path reserved for that environment;
- `addons_path` contains Odoo's addon roots and the parent directory of every
  custom addon under test; and
- the file does not contain `init`, `update`, or `uninstall` options. Modootest
  rejects database mutation flags.

Keep credentials outside Git. Modootest executes tests sequentially; do not use
pytest-xdist or run concurrent test processes against the same database.

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

For an existing addon, keep its normal structure and add test files under its
`tests/` directory. Confirm that the exact addon copy selected by Odoo's
`addons_path` is the one you are editing. Modootest imports configured addons as
`odoo.addons.<addon>.*`; consumer models do not need an explicit `_module`
workaround.

## 5. Install the addon before the first test run

The custom addon must already be installed in the dedicated database. Installing
modootest as a Python package does not install the Odoo addon.

For a new addon, install the scaffold once with Odoo's normal CLI:

```bash
PYTHONPATH=/path/to/odoo-src \
/path/to/odoo-venv/bin/python /path/to/odoo-src/odoo-bin \
  -c /absolute/path/modootest.conf \
  -d modootest_testdb \
  -i custom_sales \
  --stop-after-init
```

For an existing addon, verify that it is installed in the dedicated database.
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
corresponding Odoo `-u` operation against the dedicated database before testing.

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
- [ ] Install modootest in the same Python environment as Odoo and pytest.
- [ ] Verify the import, CLI, pytest plugin, and `--modootest-config` option.
- [ ] Create an untracked configuration for exactly one dedicated test database.
- [ ] Put the custom addon parent directory in `addons_path`.
- [ ] Create or inspect the addon's plural `tests/` directory.
- [ ] Install a new addon scaffold with Odoo `-i`, or confirm an existing addon is installed.
- [ ] Write one focused test before adding the business implementation.
- [ ] Run it and verify that RED is caused by the missing business behaviour.
- [ ] Implement the smallest production change.
- [ ] Run a fresh pytest process and verify GREEN.
- [ ] Add the next behaviour and repeat the cycle.
- [ ] Use Odoo `-u` before pytest when database-backed definitions change.
- [ ] Run the full addon tests, then review `modootest plan` and impacted selection.
- [ ] Keep all tests sequential and keep production data out of the test database.
