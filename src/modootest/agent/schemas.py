"""Resource loading API for modootest JSON schemas."""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path
from typing import Any

KNOWN_SCHEMAS_V1 = {
    "diagnostic": "diagnostic.json",
    "diagnostic.json": "diagnostic.json",
    "plan": "plan.json",
    "plan.json": "plan.json",
    "selection": "selection.json",
    "selection.json": "selection.json",
    "execution": "execution.json",
    "execution.json": "execution.json",
    "result": "result.json",
    "result.json": "result.json",
    "watch_event": "watch_event.json",
    "watch_event.json": "watch_event.json",
}


def _normalize_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned.endswith(".json"):
        cleaned = f"{cleaned}.json"
    return cleaned


def list_schemas(version: str = "v1") -> list[str]:
    """Return a sorted list of canonical schema names for the given version."""
    if version != "v1":
        raise ValueError(f"Unsupported schema version: '{version}'. Only 'v1' is supported.")
    canonical = sorted({k for k in KNOWN_SCHEMAS_V1.keys() if not k.endswith(".json")})
    return canonical


def get_schema_resource(name: str, version: str = "v1"):
    """Return Traversable resource for the given schema name and version."""
    if version != "v1":
        raise ValueError(f"Unsupported schema version: '{version}'. Only 'v1' is supported.")
    filename = _normalize_name(name)
    if filename not in KNOWN_SCHEMAS_V1.values():
        raise FileNotFoundError(f"Unknown schema '{name}' in version '{version}'.")
    return importlib.resources.files(f"modootest.schemas.{version}").joinpath(filename)


def load_schema(name: str, version: str = "v1") -> dict[str, Any]:
    """Load and return parsed JSON schema dict for the given name and version."""
    resource = get_schema_resource(name, version=version)
    content = resource.read_text(encoding="utf-8")
    return json.loads(content)


def validate_data(data: dict[str, Any], schema_name: str, version: str = "v1") -> None:
    """Validate data against a packaged schema using the optional 'jsonschema' library.

    Raises ImportError if jsonschema is not installed.
    """
    try:
        import jsonschema
    except ImportError as err:
        raise ImportError(
            "Schema validation requires the optional 'jsonschema' package: pip install jsonschema"
        ) from err

    schema = load_schema(schema_name, version=version)

    try:
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        registry = Registry()
        for name in list_schemas(version=version):
            sch = load_schema(name, version=version)
            res = Resource.from_contents(sch, default_specification=DRAFT202012)
            if "$id" in sch:
                registry = registry.with_resource(sch["$id"], res)
            registry = registry.with_resource(f"{name}.json", res)
            registry = registry.with_resource(f"https://modootest.dev/schemas/{version}/{name}.json", res)
        validator = jsonschema.Draft202012Validator(schema, registry=registry)
        validator.validate(data)
    except ImportError:
        try:
            resolver = jsonschema.RefResolver.from_schema(schema)
            jsonschema.validate(instance=data, schema=schema, resolver=resolver)
        except Exception:
            jsonschema.validate(instance=data, schema=schema)
