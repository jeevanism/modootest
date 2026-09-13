"""SQL query performance and budget assertion utilities for modootest."""
from contextlib import contextmanager
from typing import Any


class QueryCounter:
    """Tracks SQL query execution count via Odoo cursor's sql_log_count counter."""

    def __init__(self, cr: Any):
        self.cr = cr
        self.start_count = 0
        self.count = 0

    def __repr__(self) -> str:
        return f"<QueryCounter count={self.count}>"


@contextmanager
def query_count(env: Any, flush: bool = True):
    """Context manager that counts SQL queries executed by the active cursor.

    Performs pre- and post-block flush_all() by default to prevent lazy ORM
    computations or deferred SQL flushes from distorting measurement.

    :param env: Active Odoo Environment instance.
    :param flush: Whether to flush pending ORM updates before and after measurement (default True).
    :return: Context manager yielding QueryCounter instance.
    """
    if flush and hasattr(env, "flush_all"):
        env.flush_all()
    if flush and hasattr(env, "cr") and hasattr(env.cr, "flush"):
        env.cr.flush()

    counter = QueryCounter(env.cr)
    counter.start_count = getattr(env.cr, "sql_log_count", 0)
    try:
        yield counter
    finally:
        if flush and hasattr(env, "flush_all"):
            env.flush_all()
        if flush and hasattr(env, "cr") and hasattr(env.cr, "flush"):
            env.cr.flush()
        counter.count = getattr(env.cr, "sql_log_count", 0) - counter.start_count


@contextmanager
def assert_max_queries(env: Any, limit: int, flush: bool = True):
    """Context manager asserting that executed SQL queries do not exceed a given limit.

    Useful for catching N+1 query regressions and performance leaks in business logic.

    :param env: Active Odoo Environment instance.
    :param limit: Maximum allowed SQL queries (inclusive).
    :param flush: Whether to flush pending ORM updates before and after (default True).
    :raises AssertionError: If query count exceeds limit.
    """
    if not isinstance(limit, int) or limit < 0:
        raise ValueError(f"limit must be a non-negative integer, got {limit!r}")

    with query_count(env, flush=flush) as qc:
        yield qc

    if qc.count > limit:
        raise AssertionError(
            f"Query budget exceeded: executed {qc.count} SQL queries, but maximum allowed was {limit} "
            f"({qc.count - limit} queries over budget)."
        )
