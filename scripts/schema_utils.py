"""Shared helpers for loading the extraction schema and turning it into an
Anthropic tool definition, and for validating extraction output against it.
"""
from __future__ import annotations

import json
import os
from typing import Any

import jsonschema

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(ROOT, "schema", "denial_extraction.schema.json")

_FIELD_KINDS = {
    "str_field": (str, type(None)),
    "date_field": (str, type(None)),
    "int_field": (int, type(None)),
    "money_field": (int, float, type(None)),
    "bool_field": (bool, type(None)),
    "str_list_field": (list,),
    "enum_field": (str, type(None)),
}


def load_schema(path: str = SCHEMA_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_tool_definition(schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn the extraction schema into an Anthropic tool definition.

    Anthropic's tool-use API accepts a standard JSON Schema object
    (including $defs/$ref) as `input_schema`; the model uses it to shape the
    tool call, but does not guarantee strict validation. `validate_instance`
    below re-checks the result in code, which is the actual guarantee.
    """
    schema = schema or load_schema()
    input_schema = {k: v for k, v in schema.items() if k not in ("$schema", "$id", "title", "description")}
    return {
        "name": "record_denial_extraction",
        "description": (
            "Record the structured extraction of a health insurance denial "
            "letter. Call exactly once with the complete object."
        ),
        "input_schema": input_schema,
    }


def validator(schema: dict[str, Any] | None = None) -> jsonschema.Draft202012Validator:
    schema = schema or load_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def validate_instance(instance: dict[str, Any], schema: dict[str, Any] | None = None) -> list[str]:
    """Return a list of human-readable validation error strings (empty = valid)."""
    v = validator(schema)
    errors = sorted(v.iter_errors(instance), key=lambda e: list(e.path))
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]


def null_field() -> dict[str, Any]:
    return {"value": None, "confidence": 0, "evidence": []}


def empty_list_field() -> dict[str, Any]:
    return {"value": [], "confidence": 0, "evidence": []}


def walk_leaf_fields(node: Any, path: str = ""):
    """Yield (dot.path, field_obj) for every {value, confidence, evidence}-shaped
    dict inside an extraction (or label) instance. Shared by validate_rules.py
    and score.py so both walk the schema the same way.
    """
    if isinstance(node, dict):
        if {"value", "confidence", "evidence"} <= node.keys():
            yield path, node
        else:
            for k, v in node.items():
                yield from walk_leaf_fields(v, f"{path}.{k}" if path else k)


def get_field(instance: dict[str, Any], dotted_path: str) -> dict[str, Any] | None:
    node: Any = instance
    for part in dotted_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if isinstance(node, dict) and {"value", "confidence", "evidence"} <= node.keys():
        return node
    return None
