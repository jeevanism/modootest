"""modootest Developer API.

High-productivity testing utilities built on the verified modootest isolation kernel:
- as_user, as_company: context-switching utilities preserving transaction isolation
- assert_max_queries, query_count: SQL query budgeting and profiling
- OdooFactory, RecordFactory, Sequence: declarative and imperative test data generation
- mock_http, HttpMock, RecordedCall: outbound HTTP mocking
- freeze_time: time control and time-travel for ORM fields
"""

from modootest.developer.context import as_company, as_user
from modootest.developer.factory import OdooFactory, RecordFactory, Sequence
from modootest.developer.mocking import HttpMock, RecordedCall, mock_http
from modootest.developer.query import QueryCounter, assert_max_queries, query_count
from modootest.developer.time import freeze_time

__all__ = [
    "as_company",
    "as_user",
    "assert_max_queries",
    "query_count",
    "QueryCounter",
    "OdooFactory",
    "RecordFactory",
    "Sequence",
    "mock_http",
    "HttpMock",
    "RecordedCall",
    "freeze_time",
]
