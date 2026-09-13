"""pytest fixtures for modootest."""
from contextlib import contextmanager
from typing import Any, Optional

import pytest

from modootest.developer.context import as_company as _dev_as_company, as_user as _dev_as_user
from modootest.developer.factory import OdooFactory
from modootest.developer.mocking import mock_http as _dev_mock_http
from modootest.developer.query import assert_max_queries as _dev_assert_max_queries, query_count as _dev_query_count
from modootest.developer.time import freeze_time as _dev_freeze_time
from modootest.isolation.transaction import TestTransaction


@pytest.fixture(scope="session")
def odoo_registry(pytestconfig):
    """Session-scoped Odoo Registry fixture."""
    config_path = pytestconfig.getoption("--modootest-config")
    if not config_path:
        pytest.fail(
            "Odoo integration requested but --modootest-config option was not provided."
        )

    from modootest.adapter.v19.registry import get_odoo_registry
    params = pytestconfig._modootest_params
    db_name = params["db_name"]
    registry = get_odoo_registry(db_name)
    return registry


@pytest.fixture
def _odoo_tx(odoo_registry):
    """Internal function-scoped fixture creating the single TestTransaction instance."""
    with TestTransaction(odoo_registry) as tx:
        yield tx


@pytest.fixture
def odoo_cr(_odoo_tx):
    """Function-scoped Odoo cursor fixture, referencing the single _odoo_tx transaction."""
    return _odoo_tx.cr


@pytest.fixture
def odoo_env(_odoo_tx):
    """Function-scoped Odoo ORM Environment fixture, referencing the single _odoo_tx transaction."""
    return _odoo_tx.env


@pytest.fixture
def odoo_factory(odoo_env):
    """Provides an OdooFactory instance bound to the active test environment."""
    return OdooFactory(odoo_env)


@pytest.fixture
def as_user(odoo_env):
    """Context manager fixture yielding an Environment derived with the specified user.

    Can be used as:
        with as_user("base.user_demo") as env:
    or:
        with as_user(custom_env, "base.user_demo") as env:
    """
    @contextmanager
    def _as_user(user_or_env: Any, maybe_user: Optional[Any] = None):
        if maybe_user is not None:
            target_env = user_or_env
            target_user = maybe_user
        else:
            target_env = odoo_env
            target_user = user_or_env
        with _dev_as_user(target_env, target_user) as env:
            yield env

    return _as_user


@pytest.fixture
def as_company(odoo_env):
    """Context manager fixture yielding an Environment derived with the specified company.

    Can be used as:
        with as_company("base.main_company") as env:
    or:
        with as_company(custom_env, "base.main_company") as env:
    """
    @contextmanager
    def _as_company(company_or_env: Any, maybe_company: Optional[Any] = None):
        if maybe_company is not None:
            target_env = company_or_env
            target_company = maybe_company
        else:
            target_env = odoo_env
            target_company = company_or_env
        with _dev_as_company(target_env, target_company) as env:
            yield env

    return _as_company


@pytest.fixture
def query_count(odoo_env):
    """Context manager fixture counting SQL queries executed during the block.

    Can be used as:
        with query_count() as qc:
    or:
        with query_count(custom_env) as qc:
    """
    @contextmanager
    def _query_count(env: Optional[Any] = None, flush: bool = True):
        target_env = odoo_env if env is None else env
        with _dev_query_count(target_env, flush=flush) as qc:
            yield qc

    return _query_count


@pytest.fixture
def assert_max_queries(odoo_env):
    """Context manager fixture asserting query count does not exceed limit.

    Can be used as:
        with assert_max_queries(limit):
    or:
        with assert_max_queries(custom_env, limit):
    """
    @contextmanager
    def _assert_max_queries(limit_or_env: Any, maybe_limit: Optional[int] = None, flush: bool = True):
        if maybe_limit is not None:
            target_env = limit_or_env
            limit = maybe_limit
        else:
            target_env = odoo_env
            limit = limit_or_env
        with _dev_assert_max_queries(target_env, limit, flush=flush) as qc:
            yield qc

    return _assert_max_queries


@pytest.fixture
def mock_http():
    """Fixture activating outbound HTTP mocking for requests-based calls.

    Yields an HttpMock instance active for the duration of the test.
    """
    with _dev_mock_http() as http:
        yield http


@pytest.fixture
def freeze_time():
    """Fixture providing freeze_time context manager for date/time control."""
    return _dev_freeze_time
