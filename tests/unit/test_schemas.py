"""Unit tests for versioned JSON schema publishing and public resource loading API."""

from __future__ import annotations

import json
from pathlib import Path
import re
import pytest

from modootest.agent.schemas import get_schema_resource, list_schemas, load_schema


class SchemaValidator:
    """Lightweight Draft 2020-12 structural schema validator without third-party dependencies."""

    def __init__(self, schemas: dict[str, dict]):
        self.schemas = schemas

    def validate(self, instance: object, schema: dict, root: dict | None = None, path: str = "$") -> None:
        if root is None:
            root = schema

        expected_type = schema.get("type")
        if expected_type:
            if isinstance(expected_type, list):
                valid_types = expected_type
            else:
                valid_types = [expected_type]

            type_matched = False
            for t in valid_types:
                if t == "object" and isinstance(instance, dict):
                    type_matched = True
                elif t == "array" and isinstance(instance, (list, tuple)):
                    type_matched = True
                elif t == "string" and isinstance(instance, str):
                    type_matched = True
                elif t == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
                    type_matched = True
                elif t == "number" and isinstance(instance, (int, float)) and not isinstance(instance, bool):
                    type_matched = True
                elif t == "boolean" and isinstance(instance, bool):
                    type_matched = True
                elif t == "null" and instance is None:
                    type_matched = True

            if not type_matched:
                raise ValueError(
                    f"At {path}: expected type in {valid_types}, got {type(instance).__name__} ({instance!r})"
                )

        if "const" in schema and instance != schema["const"]:
            raise ValueError(f"At {path}: expected const {schema['const']!r}, got {instance!r}")

        if "enum" in schema and instance not in schema["enum"]:
            raise ValueError(f"At {path}: value {instance!r} not in enum {schema['enum']}")

        if isinstance(instance, dict):
            required = schema.get("required", [])
            for req in required:
                if req not in instance:
                    raise ValueError(f"At {path}: required property '{req}' missing from instance")

            properties = schema.get("properties", {})
            additional = schema.get("additionalProperties", True)
            if additional is False:
                for k in instance:
                    if k not in properties:
                        raise ValueError(f"At {path}: unexpected additional property '{k}'")

            for k, val in instance.items():
                if k in properties:
                    prop_schema, new_root = self._resolve_ref(properties[k], root)
                    self.validate(val, prop_schema, root=new_root, path=f"{path}.{k}")

        elif isinstance(instance, (list, tuple)):
            items_schema = schema.get("items")
            if items_schema:
                res_items, new_root = self._resolve_ref(items_schema, root)
                for idx, item in enumerate(instance):
                    self.validate(item, res_items, root=new_root, path=f"{path}[{idx}]")

        one_of = schema.get("oneOf")
        if one_of:
            matched = 0
            errors = []
            for idx, candidate in enumerate(one_of):
                cand_res, new_root = self._resolve_ref(candidate, root)
                try:
                    self.validate(instance, cand_res, root=new_root, path=f"{path}.oneOf[{idx}]")
                    matched += 1
                except ValueError as err:
                    errors.append(str(err))
            if matched != 1:
                raise ValueError(
                    f"At {path}: expected exactly one oneOf match, got {matched}. Errors: {errors}"
                )

    def _resolve_ref(self, subschema: dict, root_schema: dict) -> tuple[dict, dict]:
        if "$ref" not in subschema:
            return subschema, root_schema
        ref = subschema["$ref"]
        if ref.startswith("#/$defs/"):
            def_key = ref[len("#/$defs/") :]
            defs = root_schema.get("$defs", {})
            if def_key not in defs:
                raise KeyError(f"Missing $defs entry: '{def_key}'")
            return defs[def_key], root_schema
        elif ref in self.schemas:
            target = self.schemas[ref]
            return target, target
        elif ref.endswith(".json") and ref in self.schemas:
            target = self.schemas[ref]
            return target, target
        raise KeyError(f"Could not resolve $ref '{ref}'")


def test_public_schema_listing_and_loading() -> None:
    schemas = list_schemas("v1")
    assert "diagnostic" in schemas
    assert "plan" in schemas
    assert "selection" in schemas
    assert "execution" in schemas
    assert "result" in schemas
    assert "watch_event" in schemas

    with pytest.raises(ValueError, match="Unsupported schema version"):
        list_schemas("v2")

    with pytest.raises(FileNotFoundError, match="Unknown schema"):
        load_schema("nonexistent_schema")


def test_schema_draft_2020_12_attributes() -> None:
    expected_names = ["diagnostic", "plan", "selection", "execution", "result", "watch_event"]
    for name in expected_names:
        schema = load_schema(name)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].startswith("https://modootest.dev/schemas/v1/")
        assert schema["$id"].endswith(f"{name}.json")
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False


def test_schema_validation_of_representative_payloads() -> None:
    loaded = {name + ".json": load_schema(name) for name in list_schemas("v1")}
    loaded.update({name: load_schema(name) for name in list_schemas("v1")})
    validator = SchemaValidator(loaded)

    # 1. Valid diagnostic
    diag = {
        "severity": "WARNING",
        "code": "unmerged_git_conflict",
        "category": "git",
        "message": "Conflict detected at path",
        "remediation": "Resolve conflict",
        "context": {"file": "addon/models.py", "lines": 42},
    }
    validator.validate(diag, loaded["diagnostic.json"])

    # Invalid diagnostic with unexpected field
    invalid_diag = dict(diag)
    invalid_diag["extra_junk"] = "bad"
    with pytest.raises(ValueError, match="unexpected additional property 'extra_junk'"):
        validator.validate(invalid_diag, loaded["diagnostic.json"])

    # 2. Valid plan
    plan_payload = {
        "$schema": "https://modootest.dev/schemas/v1/plan.json",
        "git_root": "/repo",
        "comparison_mode": "commit",
        "base_commit": "abc1234",
        "head_commit": "def5678",
        "addons_paths": ["addons"],
        "is_complete": True,
        "fresh_process_required": False,
        "module_update_targets": ["crm"],
        "changed_addons": ["crm"],
        "deleted_addons": [],
        "downstream_impact_addons": ["sale_crm"],
        "file_classifications": [
            {
                "path": "addons/crm/models.py",
                "old_path": None,
                "status": "M",
                "category": "python_model",
                "reason": "Model change",
                "is_uncertain": False,
                "module_update": True,
                "fresh_process": False,
                "old_owner": "crm",
                "new_owner": "crm",
                "affected_addons": ["crm"],
            }
        ],
        "diagnostics": [diag],
        "graph_snapshot_policy": "Conservative old+new",
    }
    validator.validate(plan_payload, loaded["plan.json"])

    # 3. Valid selection
    selection_payload = {
        "$schema": "https://modootest.dev/schemas/v1/selection.json",
        "mode": "direct",
        "is_complete": True,
        "broadened_to_all_tests": False,
        "explanation": "Direct mode selection",
        "selected_tests": [
            {
                "path": "addons/crm/tests/test_lead.py",
                "addon": "crm",
                "reason": "Directly changed addon",
            }
        ],
        "excluded_targets": [
            {
                "addon": "sale",
                "reason": "No test directory",
            }
        ],
        "test_paths": ["addons/crm/tests/test_lead.py"],
        "diagnostics": [],
        "plan": plan_payload,
    }
    validator.validate(selection_payload, loaded["selection.json"])

    # 4. Valid execution
    execution_payload = {
        "$schema": "https://modootest.dev/schemas/v1/execution.json",
        "status": "execution_passed",
        "returncode": 0,
        "collect_only": False,
        "executed": True,
        "test_count": 1,
        "log_file": "logs/pytest.log",
        "summary": "pytest execution succeeded",
    }
    validator.validate(execution_payload, loaded["execution.json"])

    # 5. Valid result envelope
    envelope = {
        "$schema": "https://modootest.dev/schemas/v1/result.json",
        "contract_version": "1.0",
        "command": "impacted",
        "status": "complete",
        "exit_code": 0,
        "is_complete": True,
        "diagnostics": [],
        "payload": selection_payload,
        "execution": execution_payload,
    }
    validator.validate(envelope, loaded["result.json"])

    # 6. Valid watch event
    watch_event = {
        "$schema": "https://modootest.dev/schemas/v1/watch_event.json",
        "contract_version": "1.0",
        "sequence": 1,
        "event": "watch_started",
        "data": {
            "repo": "/repo",
            "debounce": 0.3,
            "poll_interval": 0.5,
        },
    }
    validator.validate(watch_event, loaded["watch_event.json"])


def test_validate_data_api() -> None:
    """Test public validate_data helper."""
    from modootest.agent.schemas import validate_data

    valid_watch = {
        "$schema": "https://modootest.dev/schemas/v1/watch_event.json",
        "contract_version": "1.0",
        "sequence": 1,
        "event": "watch_started",
        "data": {
            "repo": "/repo",
            "debounce": 0.3,
            "poll_interval": 0.5,
        },
    }
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError, match="jsonschema"):
            validate_data(valid_watch, "watch_event")
    else:
        # Should succeed without error
        validate_data(valid_watch, "watch_event")

        # Invalid event name should raise validation error
        invalid_watch = dict(valid_watch)
        invalid_watch["event"] = "unknown_bogus_event"
        with pytest.raises(Exception):
            validate_data(invalid_watch, "watch_event")


def test_schemas_require_constant_dollar_schema() -> None:
    """Verify that $schema is required on all versioned object schemas and missing it fails."""
    versioned = ["plan", "selection", "execution", "result", "watch_event"]
    loaded = {name: load_schema(name) for name in list_schemas("v1")}
    validator = SchemaValidator(loaded)

    for name in versioned:
        schema = loaded[name]
        assert "$schema" in schema["required"], f"$schema not required in {name}.json"
        assert schema["properties"]["$schema"]["const"] == f"https://modootest.dev/schemas/v1/{name}.json"

    # Verify validator rejects missing $schema
    with pytest.raises(ValueError, match="required property '\\$schema' missing"):
        validator.validate(
            {
                "contract_version": "1.0",
                "sequence": 1,
                "event": "watch_stopped",
                "data": {},
            },
            loaded["watch_event"],
        )


def test_documentation_examples_exact_validation() -> None:
    """Validate exact documentation examples from docs/agent_interface.md against schemas."""
    loaded = {name + ".json": load_schema(name) for name in list_schemas("v1")}
    loaded.update({name: load_schema(name) for name in list_schemas("v1")})
    validator = SchemaValidator(loaded)

    docs_path = Path(__file__).resolve().parent.parent.parent / "docs" / "agent_interface.md"
    content = docs_path.read_text(encoding="utf-8")

    # 1. Exact diagnostic example
    diag_match = re.search(r"```json\n({\n  \"severity\": \"ERROR\".*?\n})\n```", content, re.DOTALL)
    assert diag_match is not None
    diag_data = json.loads(diag_match.group(1))
    assert "target" not in diag_data
    validator.validate(diag_data, loaded["diagnostic.json"])

    # 2. Exact watch events streaming lines
    watch_block_match = re.search(r"### Streaming NDJSON Events.*?\n```json\n(.*?)\n```", content, re.DOTALL)
    assert watch_block_match is not None
    ndjson_lines = [line.strip() for line in watch_block_match.group(1).strip().splitlines() if line.strip()]
    assert len(ndjson_lines) >= 5

    for line in ndjson_lines:
        event_obj = json.loads(line)
        validator.validate(event_obj, loaded["watch_event.json"])
        validator.validate(
            event_obj["data"],
            loaded["watch_event.json"]["$defs"][event_obj["event"]],
        )


def test_watch_event_data_shapes_reject_unknown_fields() -> None:
    loaded = {name + ".json": load_schema(name) for name in list_schemas("v1")}
    validator = SchemaValidator(loaded)
    started = {
        "addons_paths": ["addons"],
        "debounce": 0.3,
        "mode": "direct",
        "poll_interval": 0.5,
        "repo": "/repo",
        "timeout": 120.0,
        "unexpected": True,
    }
    with pytest.raises(ValueError, match="unexpected additional property 'unexpected'"):
        validator.validate(started, loaded["watch_event.json"]["$defs"]["watch_started"])
