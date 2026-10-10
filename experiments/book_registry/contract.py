"""Small, strict JSON contract; no optional Python packages or model APIs."""
from __future__ import annotations

import json
from pathlib import Path

VERSION = "book-registry-v1"


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def text(nullable=False):
    return {"type": ["string", "null"] if nullable else "string", "minLength": 1}


def array(items):
    return {"type": "array", "items": items}


SPAN = obj({"paragraph_id": text(), "start": {"type": "integer", "minimum": 0},
            "end": {"type": "integer", "minimum": 1}, "quote": text()})
EVIDENCE = {**array(SPAN), "minItems": 1}
SCHEMA = obj({
    "schema_version": {"type": "string", "enum": [VERSION]},
    "batch_id": text(), "complete": {"type": "boolean"},
    "model": text(), "reasoning_effort": text(),
    "entries": array(obj({"ref": text(), "existing_id": text(True), "kind": text(),
                          "name": text(), "evidence": EVIDENCE})),
    "mentions": array(obj({"entity": text(), "span": SPAN, "resolution_note": {"type": "string"}})),
    "facts": array(obj({
        "ref": text(),
        "type": {"type": "string", "enum": ["relation", "attribute", "temporal", "identity"]},
        "subject": text(), "predicate": text(), "object": text(True), "value": text(True),
        "polarity": {"type": "string", "enum": ["positive", "negative"]},
        "modality": {"type": "string", "enum": ["actual", "planned", "reported", "conditional", "hypothetical", "unknown"]},
        "attribution": {"type": "string", "enum": ["narrator", "character", "inference", "unknown"]},
        "speaker": text(True), "frame": text(), "story_time": text(True),
        "note": {"type": "string"}, "evidence": EVIDENCE,
    })),
    "coverage": array(text()),
    "unresolved": array(obj({"paragraph_id": text(), "description": text()})),
})
SCHEMA["$schema"] = "https://json-schema.org/draft/2020-12/schema"


def validate(value, schema=SCHEMA, path="$"):
    """Validate exactly the schema vocabulary used above, not arbitrary schemas."""
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    actual = ("null" if value is None else "boolean" if type(value) is bool else
              "integer" if type(value) is int else "string" if type(value) is str else
              "array" if type(value) is list else "object" if type(value) is dict else "invalid")
    if actual not in types:
        raise ValueError(f"{path}: expected {types}, got {actual}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: unsupported value")
    if actual == "object":
        expected = set(schema["properties"])
        if set(value) != expected:
            raise ValueError(f"{path}: missing={sorted(expected-set(value))}, extra={sorted(set(value)-expected)}")
        for key, child in schema["properties"].items():
            validate(value[key], child, path + "." + key)
    elif actual == "array":
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{path}: empty evidence")
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{i}]")
    elif actual == "string" and len(value.strip()) < schema.get("minLength", 0):
        raise ValueError(f"{path}: empty string")
    elif actual == "integer" and value < schema.get("minimum", value):
        raise ValueError(f"{path}: below minimum")


def empty_batch(batch_id="batch-1", complete=True):
    return {"schema_version": VERSION, "batch_id": batch_id, "complete": complete,
            "model": "gpt-6-luna", "reasoning_effort": "max", "entries": [],
            "mentions": [], "facts": [], "coverage": [], "unresolved": []}


if __name__ == "__main__":
    path = Path(__file__).with_name("response.schema.json")
    path.write_text(json.dumps(SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
