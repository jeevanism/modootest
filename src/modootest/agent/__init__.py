"""modootest AI agent interface module."""

from modootest.agent.models import ExecutionResult, ExecutionStatus, map_subprocess_returncode
from modootest.agent.schemas import get_schema_resource, list_schemas, load_schema, validate_data
from modootest.agent.serializers import (
    serialize_diagnostic,
    serialize_envelope,
    serialize_execution,
    serialize_plan,
    serialize_result_envelope,
    serialize_selection,
    serialize_watch_event,
    to_json_str,
    to_ndjson_line,
)

__all__ = [
    "ExecutionResult",
    "ExecutionStatus",
    "get_schema_resource",
    "list_schemas",
    "load_schema",
    "map_subprocess_returncode",
    "serialize_diagnostic",
    "serialize_execution",
    "serialize_plan",
    "serialize_result_envelope",
    "serialize_selection",
    "serialize_watch_event",
    "to_json_str",
    "to_ndjson_line",
    "validate_data",
]
