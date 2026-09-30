"""Structured-output schemas and a validator for the subset they use.

The schemas follow strict structured-output rules: every property is required
and additional properties are rejected. Optional values are nullable instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SEVERITIES = ("mountain", "boulder", "pebble", "sand", "dust")
BLOCKING = ("mountain", "boulder")


def _object(**properties: Any) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _array(items: dict) -> dict:
    return {"type": "array", "items": items}


STRING = {"type": "string"}
PATHS = _array(STRING)

FINDING = _object(
    severity={"type": "string", "enum": list(SEVERITIES)},
    title=STRING,
    path=STRING,
    line={"type": ["integer", "null"]},
    side={"type": ["string", "null"], "enum": ["RIGHT", "LEFT", None]},
    failure_scenario=STRING,
    evidence=STRING,
    fix=STRING,
)

BOUNDARY = _object(description=STRING, paths=PATHS)

SCHEMAS: dict[str, dict] = {
    "intent": _object(
        has_clear_intent={"type": "boolean"},
        intent=STRING,
        goals=_array(STRING),
        reason=STRING,
    ),
    "chunk": _object(
        status={"type": "string", "enum": ["complete", "incomplete"]},
        summary=STRING,
        units_reviewed=_array(STRING),
        incomplete=_array(_object(unit=STRING, reason=STRING)),
        findings=_array(FINDING),
        prior_findings=_array(
            _object(
                reference=STRING,
                status={"type": "string", "enum": ["resolved", "unresolved", "unclear"]},
                evidence=STRING,
            )
        ),
        interfaces_changed=_array(BOUNDARY),
        assumptions=_array(BOUNDARY),
        questions=_array(BOUNDARY),
    ),
    "integration": _object(
        status={"type": "string", "enum": ["complete", "incomplete"]},
        summary=STRING,
        findings=_array(FINDING),
        answers=_array(_object(question=STRING, answer=STRING, paths=PATHS)),
        unresolved=_array(BOUNDARY),
    ),
    "verification": _object(
        verdicts=_array(
            _object(
                finding_id=STRING,
                verdict={"type": "string", "enum": ["confirmed", "downgraded", "rejected"]},
                severity={"type": "string", "enum": list(SEVERITIES)},
                evidence=STRING,
            )
        ),
        carried_forward=_array(FINDING),
        summary=STRING,
    ),
}


class SchemaError(ValueError):
    pass


def validate(value: Any, schema: dict, where: str = "$") -> None:
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if not any(checks[name](value) for name in types):
        raise SchemaError(f"{where}: expected {' or '.join(types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{where}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        properties = schema["properties"]
        for key in schema["required"]:
            if key not in value:
                raise SchemaError(f"{where}: missing {key}")
        for key in value:
            if key not in properties:
                raise SchemaError(f"{where}: unexpected property {key}")
            validate(value[key], properties[key], f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{where}[{index}]")


def parse_result(text: str, stage: str) -> dict:
    """Parse a final message and validate it against the stage schema."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise SchemaError(f"final message is not JSON: {error}") from error
    validate(value, SCHEMAS[stage])
    return value


def write_schemas(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for stage, schema in SCHEMAS.items():
        path = directory / f"{stage}.schema.json"
        path.write_text(json.dumps(schema, indent=2) + "\n")
        paths[stage] = path
    return paths
