"""Executable evidence for the portable, fail-closed JSON Schema gate."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pbatch.config import _resolve_validator_specs
from pbatch.json_schema import SchemaError, load_json_file, validate
from pbatch.validator_schema import schema_validator_command


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "pbatch" / "json_schema_cli.py"


def _write_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _run_cli(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *map(str, args)], cwd=cwd,
        capture_output=True, text=True, timeout=30,
    )


def test_shipped_direction_schema_accepts_valid_and_rejects_invalid_instances():
    schema = load_json_file(
        ROOT / "examples" / "schemas" / "direction.schema.json", "schema")
    valid = {"summary": "ready", "directions": [{
        "id": "retry_risk", "problem": "unbounded retries",
        "evidence": ["pbatch/runner.py:1"], "acceptance": ["retry cap is tested"],
    }]}
    assert validate(valid, schema) == []
    invalid = {"summary": "", "directions": [{
        "id": "Bad Id", "problem": "", "evidence": [], "acceptance": [],
        "extra": True,
    }]}
    errors = validate(invalid, schema)
    assert any("minLength" in error for error in errors)
    assert any("pattern" in error for error in errors)
    assert any("minItems" in error for error in errors)
    assert any("additional property" in error for error in errors)


def test_unsupported_keyword_fails_even_in_unused_branch():
    schema = {"type": "object", "properties": {"unused": {"typoKeyword": True}}}
    with pytest.raises(SchemaError, match="unsupported keyword.*typoKeyword"):
        validate({}, schema)


@pytest.mark.parametrize("ref", ["https://example.test/schema.json", "other.json#/x"])
def test_external_refs_are_rejected(ref):
    with pytest.raises(SchemaError, match="local JSON pointer"):
        validate({}, {"$ref": ref})


def test_unresolved_local_ref_fails_even_when_instance_does_not_visit_it():
    """Regression: malformed refs used to pass when their property was absent."""
    schema = {
        "$defs": {"present": {"type": "string"}},
        "type": "object",
        "properties": {"unused": {"$ref": "#/$defs/missing"}},
    }
    with pytest.raises(SchemaError, match=r"unresolved \$ref"):
        validate({}, schema)


def test_local_ref_and_json_pointer_escaping():
    schema = {
        "$defs": {"a/b~c": {"type": "string", "pattern": "^ok$"}},
        "$ref": "#/$defs/a~1b~0c",
    }
    assert validate("ok", schema) == []
    assert validate("bad", schema)


def test_malformed_format_type_is_a_schema_error():
    with pytest.raises(SchemaError, match="format"):
        validate("value", {"type": "string", "format": ["email"]})


@pytest.mark.parametrize(
    ("payload", "max_bytes", "message"),
    [
        (b"NaN", 10, "non-standard JSON constant"),
        (b"Infinity", 10, "non-standard JSON constant"),
        (b'"12345"', 4, "exceeds 4 bytes"),
        (b'"\xff"', 10, "invalid artifact"),
    ],
)
def test_load_json_file_is_strict_and_bounded(tmp_path, payload, max_bytes, message):
    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(payload)
    with pytest.raises(SchemaError, match=message):
        load_json_file(artifact, "artifact", max_bytes)


def test_composition_keywords():
    schema = {
        "allOf": [{"type": "integer"}, {"minimum": 1}],
        "anyOf": [{"multipleOf": 2}, {"multipleOf": 3}],
        "oneOf": [{"maximum": 4}, {"minimum": 6}],
        "not": {"const": 3},
    }
    assert validate(2, schema) == []
    assert validate(6, schema) == []
    assert validate(3, schema)
    assert validate(5, schema)


def test_conditional_object_dependencies_and_property_names():
    schema = {
        "type": "object",
        "required": ["kind", "value"],
        "properties": {
            "kind": {"enum": ["email", "uuid"]},
            "value": {"type": "string"},
            "token": {"type": "string"},
            "confirm": {"type": "string"},
        },
        "patternProperties": {"^x_": {"type": "integer"}},
        "additionalProperties": False,
        "propertyNames": {"pattern": "^[a-z][a-z_]*$"},
        "dependentRequired": {"token": ["confirm"]},
        "dependentSchemas": {"confirm": {"required": ["token"]}},
        "if": {"properties": {"kind": {"const": "email"}}},
        "then": {"properties": {"value": {"format": "email"}}},
        "else": {"properties": {"value": {"format": "uuid"}}},
    }
    good = {"kind": "email", "value": "a@example.test", "x_count": 2,
            "token": "a", "confirm": "a"}
    assert validate(good, schema) == []
    errors = validate({"kind": "email", "value": "bad", "token": "a",
                       "Bad-Key": True}, schema)
    assert any("valid email" in error for error in errors)
    assert any("requires 'confirm'" in error for error in errors)
    assert any("additional property" in error for error in errors)
    assert any("property-name" in error for error in errors)


def test_array_prefix_items_contains_uniqueness_and_bounds():
    schema = {
        "type": "array", "minItems": 2, "maxItems": 4, "uniqueItems": True,
        "prefixItems": [{"const": "primary"}],
        "items": {"type": "string"},
        "contains": {"type": "string", "pattern": "^prod-"},
        "minContains": 1, "maxContains": 1,
    }
    assert validate(["primary", "prod-blue", "dev"], schema) == []
    errors = validate(["wrong", "dev", "dev", "prod-a", "prod-b"], schema)
    assert any("const" in error for error in errors)
    assert any("duplicate" in error for error in errors)
    assert any("maxItems" in error for error in errors)
    assert any("contains matched 2" in error for error in errors)


@pytest.mark.parametrize(
    ("name", "valid", "invalid"),
    [
        ("date", "2024-02-29", "2023-02-29"),
        ("time", "12:30:45Z", "25:00:00Z"),
        ("date-time", "2024-02-29T12:30:45+00:00", "2024-02-29"),
        ("email", "a@example.test", "not-an-email"),
        ("hostname", "api.example.test", "-bad.example"),
        ("ipv4", "192.0.2.1", "300.0.0.1"),
        ("ipv6", "2001:db8::1", "2001:db8::gg"),
        ("uri", "https://example.test/a", "not a uri"),
        ("uuid", "123e4567-e89b-12d3-a456-426614174000", "not-a-uuid"),
    ],
)
def test_supported_formats(name, valid, invalid):
    schema = {"type": "string", "format": name}
    assert validate(valid, schema) == []
    assert validate(invalid, schema)


def test_cli_exit_codes_and_error_limit(tmp_path):
    schema = _write_json(tmp_path / "schema.json", {
        "type": "object", "required": ["a", "b", "c", "d"],
        "properties": {name: {"type": "integer"} for name in "abcd"},
    })
    good = _write_json(tmp_path / "good.json", {name: 1 for name in "abcd"})
    bad = _write_json(tmp_path / "bad.json", {})

    passed = _run_cli("--schema", schema, good)
    assert passed.returncode == 0
    assert json.loads(passed.stdout)["status"] == "pass"

    failed = _run_cli("--schema", schema, "--max-errors", "2", bad)
    assert failed.returncode == 1
    payload = json.loads(failed.stdout)
    assert payload["status"] == "fail"
    assert len(payload["errors"]) == 2

    broken_schema = _write_json(tmp_path / "broken.json", {"unknown": True})
    rejected = _run_cli("--schema", broken_schema, good)
    assert rejected.returncode == 2
    assert json.loads(rejected.stdout)["status"] == "fail"
    assert "unsupported keyword" in rejected.stderr


def test_config_compiles_jsonschema_validator_with_quoted_path(tmp_path):
    schema = tmp_path / "schema with spaces.json"
    command = schema_validator_command(f"jsonschema={schema}")
    assert command is not None
    argv = shlex.split(command.replace("{output}", str(tmp_path / "out.json")))
    assert Path(argv[1]).samefile(CLI)
    assert argv[2:4] == ["--schema", str(schema)]
    specs = _resolve_validator_specs(f"jsonschema={schema}")
    assert len(specs) == 1 and specs[0].cmd == command
    assert schema_validator_command("true") is None


def test_copied_pbatch_cli_runs_from_neutral_directory(tmp_path):
    tool = tmp_path / "portable-tool"
    neutral = tmp_path / "neutral"
    shutil.copytree(ROOT / "pbatch", tool / "pbatch")
    neutral.mkdir()
    schema = _write_json(neutral / "schema.json", {"type": "integer", "minimum": 1})
    artifact = _write_json(neutral / "artifact.json", 2)
    env = dict(os.environ, PYTHONPATH="")
    result = subprocess.run(
        [sys.executable, "-S", str(tool / "pbatch" / "json_schema_cli.py"),
         "--schema", str(schema), str(artifact)],
        cwd=neutral, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "pass"
    assert "JSONSCHEMA: OK" in result.stderr
