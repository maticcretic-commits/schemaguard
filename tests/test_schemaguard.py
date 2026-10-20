"""Tests for SchemaGuard validation, snapshot inference, and diffing."""
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from schemaguard import (check_format, collect_outputs, diff_schemas,
                         infer_schema, json_type_name, run_validation,
                         snapshot_schema, type_matches, validate_instance)

CLI = Path(__file__).parent.parent / "src" / "schemaguard.py"


def run_cli(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True, text=True, cwd=cwd)


def errors_of(instance, schema, strict=False):
    errors, warnings = validate_instance(instance, schema, strict=strict)
    return errors, warnings


def msgs(errors):
    return [e["message"] for e in errors]


def has_msg(errors, fragment):
    return any(fragment in m for m in msgs(errors))


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

def test_json_type_names():
    assert json_type_name(None) == "null"
    assert json_type_name(True) == "boolean"
    assert json_type_name(3) == "integer"
    assert json_type_name(3.0) == "integer"
    assert json_type_name(3.5) == "number"
    assert json_type_name("x") == "string"
    assert json_type_name([]) == "array"
    assert json_type_name({}) == "object"


def test_type_matches_number_accepts_integer():
    assert type_matches(3, "number")
    assert type_matches(3.5, "number")
    assert not type_matches(True, "number")
    assert not type_matches(True, "integer")


@pytest.mark.parametrize("schema,value", [
    ({"type": "string"}, "hello"),
    ({"type": "integer"}, 42),
    ({"type": "number"}, 42),
    ({"type": "number"}, 4.2),
    ({"type": "boolean"}, False),
    ({"type": "null"}, None),
    ({"type": "array"}, [1, 2]),
    ({"type": "object"}, {"a": 1}),
    ({"type": ["string", "null"]}, None),
    ({"type": ["string", "null"]}, "x"),
])
def test_type_pass(schema, value):
    errors, _ = errors_of(value, schema)
    assert errors == []


@pytest.mark.parametrize("schema,value", [
    ({"type": "string"}, 42),
    ({"type": "integer"}, 4.2),
    ({"type": "integer"}, True),
    ({"type": "boolean"}, 1),
    ({"type": "object"}, []),
    ({"type": ["string", "null"]}, 7),
])
def test_type_fail(schema, value):
    errors, _ = errors_of(value, schema)
    assert len(errors) == 1
    assert "expected" in errors[0]["message"]


# ---------------------------------------------------------------------------
# enum / const
# ---------------------------------------------------------------------------

def test_enum_pass_and_fail():
    schema = {"enum": ["ok", "error"]}
    assert errors_of("ok", schema)[0] == []
    errors, _ = errors_of("done", schema)
    assert len(errors) == 1 and "not one of" in errors[0]["message"]


def test_enum_distinguishes_bool_from_int():
    # Python says True == 1; the contract must not.
    errors, _ = errors_of(True, {"enum": [1]})
    assert len(errors) == 1
    assert errors_of(1, {"enum": [1]})[0] == []


def test_const():
    assert errors_of("v1", {"const": "v1"})[0] == []
    errors, _ = errors_of("v2", {"const": "v1"})
    assert "expected constant" in errors[0]["message"]


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

def test_minimum_maximum():
    assert errors_of(5, {"minimum": 5, "maximum": 5})[0] == []
    assert has_msg(errors_of(4, {"minimum": 5})[0], "below minimum")
    assert has_msg(errors_of(6, {"maximum": 5})[0], "exceeds maximum")


def test_exclusive_bounds_numeric_form():
    assert has_msg(errors_of(5, {"exclusiveMinimum": 5})[0], "not above exclusiveMinimum")
    assert errors_of(6, {"exclusiveMinimum": 5})[0] == []
    assert has_msg(errors_of(5, {"exclusiveMaximum": 5})[0], "not below exclusiveMaximum")
    assert errors_of(4, {"exclusiveMaximum": 5})[0] == []


def test_exclusive_bounds_draft4_bool_form():
    schema = {"minimum": 5, "exclusiveMinimum": True}
    assert len(errors_of(5, schema)[0]) == 1
    assert errors_of(6, schema)[0] == []


def test_numeric_keywords_ignore_non_numbers():
    assert errors_of("text", {"minimum": 5})[0] == []
    assert errors_of(True, {"minimum": 0})[0] == []


# ---------------------------------------------------------------------------
# Strings
# ---------------------------------------------------------------------------

def test_length_bounds():
    assert errors_of("abc", {"minLength": 3, "maxLength": 3})[0] == []
    assert has_msg(errors_of("ab", {"minLength": 3})[0], "shorter than minLength")
    assert has_msg(errors_of("abcd", {"maxLength": 3})[0], "exceeds maxLength")


def test_empty_string_fails_minlength():
    # The silent-data-loss case: a field that must be populated.
    errors, _ = errors_of("", {"type": "string", "minLength": 1})
    assert len(errors) == 1


def test_pattern():
    schema = {"pattern": "^[A-Z]{2}-\\d+$"}
    assert errors_of("AB-123", schema)[0] == []
    assert has_msg(errors_of("ab-1", schema)[0], "does not match pattern")


def test_invalid_pattern_warns_not_crashes():
    errors, warnings = errors_of("x", {"pattern": "([bad"})
    assert errors == []
    assert any("invalid pattern" in w for w in warnings)


@pytest.mark.parametrize("fmt,good,bad", [
    ("email", "user@example.com", "not-an-email"),
    ("email", "a.b@sub.domain.org", "missing-at.example.com"),
    ("uuid", "123e4567-e89b-12d3-a456-426614174000", "not-a-uuid"),
    ("uri", "https://example.com/x", "no scheme here"),
    ("url", "https://example.com/x", "example.com/x"),
    ("date", "2026-10-20", "2026-02-30"),
    ("date", "2026-10-20", "20-10-2026"),
    ("date-time", "2026-10-20T22:41:00Z", "2026-10-20 25:99"),
    ("date-time", "2026-10-20T22:41:00+05:30", "yesterday"),
])
def test_formats(fmt, good, bad):
    assert check_format(fmt, good) is True
    assert check_format(fmt, bad) is False
    errors, _ = errors_of(bad, {"format": fmt})
    assert f"expected format {fmt}" in msgs(errors)


def test_unknown_format_warns():
    errors, warnings = errors_of("x", {"format": "hostname"})
    assert errors == []
    assert any("unknown format" in w for w in warnings)


# ---------------------------------------------------------------------------
# Arrays
# ---------------------------------------------------------------------------

def test_array_size_bounds():
    assert errors_of([1], {"minItems": 1, "maxItems": 1})[0] == []
    assert has_msg(errors_of([], {"minItems": 1})[0], "below minItems")
    assert has_msg(errors_of([1, 2], {"maxItems": 1})[0], "exceeds maxItems")


def test_items_validation_paths():
    schema = {"type": "array", "items": {"type": "integer"}}
    assert errors_of([1, 2], schema)[0] == []
    errors, _ = errors_of([1, "two", 3.5], schema)
    assert [e["path"] for e in errors] == ["$[1]", "$[2]"]


def test_items_tuple_form_warns():
    errors, warnings = errors_of([1], {"items": [{"type": "integer"}]})
    assert any("tuple" in w for w in warnings)


# ---------------------------------------------------------------------------
# Objects: required / properties / additionalProperties / strict
# ---------------------------------------------------------------------------

USER_SCHEMA = {
    "type": "object",
    "required": ["user"],
    "properties": {
        "user": {
            "type": "object",
            "required": ["email"],
            "properties": {"email": {"type": "string", "format": "email"}},
        }
    },
}


def test_nested_paths_in_errors():
    errors, _ = errors_of({"user": {"email": "nope"}}, USER_SCHEMA)
    assert errors == [{"path": "$.user.email",
                       "message": "expected format email"}]
    errors, _ = errors_of({"user": {}}, USER_SCHEMA)
    assert errors == [{"path": "$.user.email",
                       "message": "missing required field"}]
    errors, _ = errors_of({}, USER_SCHEMA)
    assert errors[0]["path"] == "$.user"


def test_additional_properties_false():
    schema = {"properties": {"a": {}}, "additionalProperties": False}
    assert errors_of({"a": 1}, schema)[0] == []
    errors, _ = errors_of({"a": 1, "confidence_score": 0.9}, schema)
    assert len(errors) == 1
    assert errors[0]["path"] == "$.confidence_score"
    assert "unexpected field" in errors[0]["message"]


def test_additional_properties_schema():
    schema = {"properties": {"a": {}},
              "additionalProperties": {"type": "integer"}}
    assert errors_of({"a": 1, "extra": 2}, schema)[0] == []
    errors, _ = errors_of({"extra": "s"}, schema)
    assert errors[0]["path"] == "$.extra"


def test_strict_mode_flags_undeclared_fields():
    schema = {"properties": {"a": {}}}
    value = {"a": 1, "hallucinated": True}
    assert errors_of(value, schema)[0] == []          # lenient by default
    errors, _ = errors_of(value, schema, strict=True)
    assert len(errors) == 1
    assert errors[0]["path"] == "$.hallucinated"
    assert "strict mode" in errors[0]["message"]


def test_strict_mode_quiet_when_schema_forbids_anyway():
    schema = {"properties": {"a": {}}, "additionalProperties": False}
    errors, _ = errors_of({"b": 1}, schema, strict=True)
    assert len(errors) == 1  # one error, from additionalProperties


def test_null_where_value_expected_fails():
    schema = {"properties": {"email": {"type": "string"}}}
    errors, _ = errors_of({"email": None}, schema)
    assert errors == [{"path": "$.email",
                       "message": "expected string, got null"}]


def test_boolean_schema():
    assert errors_of(1, True)[0] == []
    errors, _ = errors_of(1, False)
    assert len(errors) == 1


def test_unsupported_keywords_warn_and_do_not_enforce():
    schema = {"$ref": "#/defs/x", "allOf": [{"type": "string"}],
              "type": "integer"}
    errors, warnings = errors_of(5, schema)
    assert errors == []
    warned = " ".join(warnings)
    assert "$ref" in warned and "allOf" in warned
    assert warnings["unsupported keyword '$ref' at $schema "
                    "— ignored, NOT enforced"] == 1


def test_annotation_keywords_do_not_warn():
    schema = {"title": "T", "description": "D", "$schema": "http://x",
              "default": 3, "type": "integer"}
    _, warnings = errors_of(3, schema)
    assert warnings == Counter()


# ---------------------------------------------------------------------------
# Batch validation
# ---------------------------------------------------------------------------

CONTRACT = {
    "type": "object",
    "required": ["id", "email"],
    "properties": {
        "id": {"type": "integer"},
        "email": {"type": "string", "format": "email"},
    },
    "additionalProperties": False,
}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_batch_mixed_dir(tmp_path):
    write(tmp_path / "good.json",
          json.dumps({"id": 1, "email": "user@example.com"}))
    write(tmp_path / "renamed.json",
          json.dumps({"id": 2, "email_address": "user@example.com"}))
    write(tmp_path / "bad.json", "{not json")
    report = run_validation(str(tmp_path), CONTRACT)
    s = report["summary"]
    assert (s["total"], s["passed"], s["failed"]) == (3, 1, 2)
    assert s["pass_rate"] == pytest.approx(33.3)
    by_source = {r["source"]: r for r in report["results"]}
    renamed = next(r for src, r in by_source.items() if "renamed" in src)
    paths = {e["path"] for e in renamed["errors"]}
    assert "$.email" in paths and "$.email_address" in paths
    bad = next(r for src, r in by_source.items() if src.endswith("bad.json"))
    assert "invalid JSON" in bad["errors"][0]["message"]


def test_batch_strict_catches_hallucinated_field(tmp_path):
    write(tmp_path / "o.json",
          json.dumps({"id": 1, "email": "user@example.com",
                      "confidence_score": 0.5}))
    schema = {k: v for k, v in CONTRACT.items()
              if k != "additionalProperties"}
    assert run_validation(str(tmp_path), schema)["summary"]["failed"] == 0
    report = run_validation(str(tmp_path), schema, strict=True)
    assert report["summary"]["failed"] == 1
    assert report["results"][0]["errors"][0]["path"] == "$.confidence_score"


def test_jsonl_batch_records(tmp_path):
    lines = [json.dumps({"id": 1, "email": "user@example.com"}),
             "",
             json.dumps({"id": "x", "email": "user@example.com"}),
             "{broken"]
    write(tmp_path / "out.jsonl", "\n".join(lines))
    report = run_validation(str(tmp_path), CONTRACT)
    s = report["summary"]
    assert (s["total"], s["passed"], s["failed"]) == (3, 1, 2)
    sources = [r["source"] for r in report["results"]]
    assert sources[0].endswith("out.jsonl:1")
    assert sources[1].endswith("out.jsonl:3")
    assert sources[2].endswith("out.jsonl:4")


def test_collect_outputs_labels(tmp_path):
    write(tmp_path / "a.json", "1")
    write(tmp_path / "sub" / "b.jsonl", "2\n3\n")
    records = collect_outputs(str(tmp_path))
    assert [r["value"] for r in records] == [1, 2, 3]


def test_empty_dir_has_zero_outputs(tmp_path):
    report = run_validation(str(tmp_path), CONTRACT)
    assert report["summary"]["total"] == 0


def test_warnings_aggregated_with_counts(tmp_path):
    schema = {"$ref": "#/defs/thing", "type": "object"}
    write(tmp_path / "a.json", "{}")
    write(tmp_path / "b.json", "{}")
    report = run_validation(str(tmp_path), schema)
    assert len(report["warnings"]) == 1
    assert report["warnings"][0]["count"] == 2


# ---------------------------------------------------------------------------
# Snapshot inference
# ---------------------------------------------------------------------------

def test_infer_required_and_presence_stats():
    stats = {}
    schema = infer_schema(
        [{"a": 1, "b": "x"}, {"a": 2}, {"a": 3, "b": "y"}], "$", stats)
    assert schema["type"] == "object"
    assert schema["required"] == ["a"]
    assert schema["properties"]["a"]["type"] == "integer"
    assert stats["$.a"] == {"present": 3, "of": 3}
    assert stats["$.b"] == {"present": 2, "of": 3}


def test_infer_int_widens_to_number():
    schema = infer_schema([1, 2.5, 3])
    assert schema["type"] == "number"


def test_infer_mixed_types_become_list():
    schema = infer_schema(["x", None, "y"])
    assert schema["type"] == ["null", "string"]


def test_infer_null_field_keeps_presence():
    stats = {}
    schema = infer_schema([{"email": "user@example.com"},
                           {"email": None}], "$", stats)
    assert schema["required"] == ["email"]
    assert schema["properties"]["email"]["type"] == ["null", "string"]
    assert stats["$.email"] == {"present": 2, "of": 2}


def test_infer_arrays_merge_items():
    schema = infer_schema([{"tags": ["a", "b"]}, {"tags": ["c"]}])
    assert schema["properties"]["tags"]["items"]["type"] == "string"
    schema = infer_schema([{"tags": []}, {"tags": []}])
    assert "items" not in schema["properties"]["tags"]


def test_infer_nested_objects():
    schema = infer_schema([{"user": {"id": 1, "name": "n"}},
                           {"user": {"id": 2}}])
    user = schema["properties"]["user"]
    assert user["required"] == ["id"]
    assert user["properties"]["name"]["type"] == "string"


def test_snapshot_from_dir_and_round_trip(tmp_path):
    write(tmp_path / "s1.json",
          json.dumps({"id": 1, "email": "user@example.com",
                      "api_key": "example-key-123"}))
    write(tmp_path / "s2.json",
          json.dumps({"id": 2, "email": "other@example.org"}))
    schema, stats, count, load_errors = snapshot_schema(str(tmp_path))
    assert count == 2 and load_errors == []
    assert schema["required"] == ["email", "id"]
    # The inferred schema must validate the very samples it came from.
    report = run_validation(str(tmp_path), schema)
    assert report["summary"]["failed"] == 0
    # ...and reject a renamed field (missing required + type drift).
    write(tmp_path / "s3.json",
          json.dumps({"id": "three", "email_address": "x@example.org"}))
    report = run_validation(str(tmp_path), schema)
    assert report["summary"]["failed"] == 1


def test_snapshot_skips_malformed_with_error_note(tmp_path):
    write(tmp_path / "ok.json", json.dumps({"a": 1}))
    write(tmp_path / "bad.json", "{nope")
    schema, stats, count, load_errors = snapshot_schema(str(tmp_path))
    assert count == 1
    assert len(load_errors) == 1 and "bad.json" in load_errors[0]


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

OLD = {
    "type": "object",
    "required": ["id"],
    "properties": {
        "id": {"type": "integer"},
        "email": {"type": "string"},
        "status": {"type": "string", "enum": ["ok", "error", "pending"]},
        "legacy": {"type": "string"},
    },
}
NEW = {
    "type": "object",
    "required": ["id", "email"],
    "properties": {
        "id": {"type": "string"},
        "email": {"type": "string"},
        "status": {"type": "string", "enum": ["ok", "error", "archived"]},
        "extra": {"type": "boolean"},
    },
}


def changes_by(changes, kind):
    return [c for c in changes if c["change"] == kind]


def test_diff_all_change_classes():
    changes = diff_schemas(OLD, NEW)
    removed = changes_by(changes, "field-removed")
    assert removed and removed[0]["path"] == "$.legacy"
    assert removed[0]["breaking"] is True
    added = changes_by(changes, "field-added")
    assert added and added[0]["breaking"] is False
    now_req = changes_by(changes, "field-now-required")
    assert now_req and now_req[0]["path"] == "$.email"
    assert now_req[0]["breaking"] is True
    type_changed = changes_by(changes, "type-changed")
    assert any(c["path"] == "$.id" for c in type_changed)
    enum_removed = changes_by(changes, "enum-value-removed")
    assert enum_removed and enum_removed[0]["breaking"] is True
    assert "pending" in enum_removed[0]["detail"]
    enum_added = changes_by(changes, "enum-value-added")
    assert enum_added and enum_added[0]["breaking"] is False


def test_diff_no_longer_required_is_non_breaking():
    changes = diff_schemas({"required": ["a", "b"]}, {"required": ["a"]})
    relaxed = changes_by(changes, "field-no-longer-required")
    assert relaxed and relaxed[0]["breaking"] is False


def test_diff_identical_schemas_no_changes():
    assert diff_schemas(OLD, json.loads(json.dumps(OLD))) == []


def test_diff_nested_field_removed():
    old = {"properties": {"user": {"properties": {"email": {}}}}}
    new = {"properties": {"user": {"properties": {}}}}
    changes = diff_schemas(old, new)
    assert changes[0]["path"] == "$.user.email"
    assert changes[0]["breaking"] is True


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------

def test_cli_validate_exit_codes(tmp_path):
    schema = write(tmp_path / "contract.json", json.dumps(CONTRACT))
    write(tmp_path / "out" / "good.json",
          json.dumps({"id": 1, "email": "user@example.com"}))
    r = run_cli("validate", str(tmp_path / "out"), "--schema", str(schema))
    assert r.returncode == 0, r.stderr
    write(tmp_path / "out" / "bad.json",
          json.dumps({"id": 1, "email_address": "user@example.com"}))
    r = run_cli("validate", str(tmp_path / "out"), "--schema", str(schema))
    assert r.returncode == 1
    assert "$.email" in r.stdout


def test_cli_validate_json_shape(tmp_path):
    schema = write(tmp_path / "contract.json", json.dumps(CONTRACT))
    write(tmp_path / "o.json",
          json.dumps({"id": 1, "email": "user@example.com"}))
    r = run_cli("validate", str(tmp_path / "o.json"), "--schema",
                str(schema), "--format", "json")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["schema"] == str(schema)
    assert out["summary"] == {"total": 1, "passed": 1, "failed": 0,
                              "pass_rate": 100.0}
    assert out["results"][0]["status"] == "pass"
    assert out["warnings"] == []


def test_cli_validate_usage_errors_exit_2(tmp_path):
    schema = write(tmp_path / "contract.json", json.dumps(CONTRACT))
    r = run_cli("validate", str(tmp_path / "missing"), "--schema",
                str(schema))
    assert r.returncode == 2
    write(tmp_path / "o.json", "{}")
    r = run_cli("validate", str(tmp_path / "o.json"), "--schema",
                str(tmp_path / "nope.json"))
    assert r.returncode == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    r = run_cli("validate", str(empty), "--schema", str(schema))
    assert r.returncode == 2


def test_cli_validate_warns_on_unsupported(tmp_path):
    schema = write(tmp_path / "c.json",
                   json.dumps({"$ref": "#/defs/x", "type": "object"}))
    write(tmp_path / "o.json", "{}")
    r = run_cli("validate", str(tmp_path / "o.json"), "--schema", str(schema))
    assert r.returncode == 0
    assert "unsupported" in r.stdout and "$ref" in r.stdout


def test_cli_snapshot_then_validate(tmp_path):
    write(tmp_path / "samples" / "a.json",
          json.dumps({"id": 1, "tags": ["x"]}))
    write(tmp_path / "samples" / "b.json",
          json.dumps({"id": 2, "tags": []}))
    out = tmp_path / "baseline.schema.json"
    r = run_cli("snapshot", str(tmp_path / "samples"), "--out", str(out))
    assert r.returncode == 0, r.stderr
    schema = json.loads(out.read_text())
    assert schema["required"] == ["id", "tags"]
    r = run_cli("validate", str(tmp_path / "samples"), "--schema", str(out))
    assert r.returncode == 0


def test_cli_diff_exit_codes(tmp_path):
    old = write(tmp_path / "old.json", json.dumps(OLD))
    new = write(tmp_path / "new.json", json.dumps(NEW))
    r = run_cli("diff", str(old), str(new))
    assert r.returncode == 1
    assert "BREAKING" in r.stdout
    r = run_cli("diff", str(old), str(old))
    assert r.returncode == 0
    r = run_cli("diff", str(old), str(new), "--format", "json")
    out = json.loads(r.stdout)
    assert out["breaking_count"] >= 3
    assert all("change" in c and "breaking" in c for c in out["changes"])


# ---------------------------------------------------------------------------
# Phase 1: version flag, JSON envelope, error envelope
# ---------------------------------------------------------------------------

def test_cli_version():
    r = run_cli("--version")
    assert r.returncode == 0
    assert r.stdout.strip() == "schemaguard 1.0.0"


def test_cli_validate_json_envelope_on_failure(tmp_path):
    schema = write(tmp_path / "contract.json", json.dumps(CONTRACT))
    # Renamed field: missing required $.email + unexpected $.email_address
    # = exactly 2 validation errors.
    write(tmp_path / "o.json",
          json.dumps({"id": 1, "email_address": "user@example.com"}))
    r = run_cli("validate", str(tmp_path / "o.json"), "--schema",
                str(schema), "--format", "json")
    assert r.returncode == 1
    out = json.loads(r.stdout)
    assert out["tool"] == "schemaguard"
    assert out["version"] == "1.0.0"
    assert out["schema_version"] == 1
    assert out["findings_summary"] == {"count": 2,
                                       "highest_severity": "HIGH"}
    # Existing payload keys are untouched.
    assert out["schema"] == str(schema)
    assert out["summary"]["failed"] == 1


def test_cli_schema_version_is_exactly_int_1(tmp_path):
    schema = write(tmp_path / "contract.json", json.dumps(CONTRACT))
    write(tmp_path / "o.json",
          json.dumps({"id": 1, "email": "user@example.com"}))
    r = run_cli("validate", str(tmp_path / "o.json"), "--schema",
                str(schema), "--format", "json")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert type(out["schema_version"]) is int
    assert out["schema_version"] == 1


def test_cli_diff_json_envelope_on_breaking_pair(tmp_path):
    old = write(tmp_path / "old.json", json.dumps(OLD))
    new = write(tmp_path / "new.json", json.dumps(NEW))
    r = run_cli("diff", str(old), str(new), "--format", "json")
    assert r.returncode == 1
    out = json.loads(r.stdout)
    assert out["tool"] == "schemaguard"
    assert out["version"] == "1.0.0"
    assert out["schema_version"] == 1
    assert out["findings_summary"] == {
        "count": out["breaking_count"], "highest_severity": "HIGH"}
    assert out["breaking_count"] == 4


def test_cli_snapshot_out_file_has_no_envelope_keys(tmp_path):
    write(tmp_path / "samples" / "a.json",
          json.dumps({"id": 1, "tags": ["x"]}))
    out = tmp_path / "baseline.schema.json"
    r = run_cli("snapshot", str(tmp_path / "samples"), "--out", str(out))
    assert r.returncode == 0, r.stderr
    schema = json.loads(out.read_text())
    assert "tool" not in schema
    assert "version" not in schema
    assert "schema_version" not in schema
    assert "findings_summary" not in schema


def test_cli_missing_path_error_envelope(tmp_path):
    schema = write(tmp_path / "contract.json", json.dumps(CONTRACT))
    missing = str(tmp_path / "missing")
    r = run_cli("validate", missing, "--schema", str(schema))
    assert r.returncode == 2
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert lines[0] == f"error: path not found: {missing}"
    envelope = json.loads(lines[-1])
    assert envelope == {"error": {"code": "file_not_found",
                                  "message": f"path not found: {missing}"}}


def test_cli_init_scaffolds(tmp_path):
    r = run_cli("init", "--dir", str(tmp_path))
    assert r.returncode == 0
    assert (tmp_path / "contract.json").is_file()
    assert (tmp_path / "outputs" / "output-pass.json").is_file()
    assert (tmp_path / "outputs" / "output-fail.json").is_file()
    assert (tmp_path / "EXPECTED.md").is_file()
    r = run_cli("validate", str(tmp_path / "outputs"), "--schema",
                str(tmp_path / "contract.json"))
    assert r.returncode == 1  # the failing sample fails, as documented
    # Second run without --force refuses to overwrite.
    r = run_cli("init", "--dir", str(tmp_path))
    assert "exists" in r.stdout


# ---------------------------------------------------------------------------
# Phase 2: SARIF output + stdin inputs
# ---------------------------------------------------------------------------

def assert_sarif_shape(doc, tool_name):
    """SARIF 2.1.0 top-level required fields (structure assertion)."""
    assert doc["version"] == "2.1.0"
    assert doc["$schema"].endswith("sarif-2.1.0.json")
    assert len(doc["runs"]) == 1
    run = doc["runs"][0]
    driver = run["tool"]["driver"]
    assert driver["name"] == tool_name
    assert driver["version"]
    assert driver["informationUri"].startswith(
        "https://github.com/maticcretic-commits/")
    rule_ids = {r["id"] for r in driver["rules"]}
    for res in run["results"]:
        assert res["ruleId"] in rule_ids
        assert res["level"] in ("error", "warning", "note", "none")
        assert res["message"]["text"]
    return run


CONTRACT_P2 = {"type": "object", "required": ["id"],
            "properties": {"id": {"type": "string"}}}


def test_sarif_validate(tmp_path):
    schema = tmp_path / "contract.json"
    schema.write_text(json.dumps(CONTRACT_P2))
    out = tmp_path / "o.json"
    out.write_text(json.dumps({"id": 7}))
    proc = run_cli("validate", str(out), "--schema", str(schema),
                   "--format", "sarif")
    assert proc.returncode == 1
    doc = json.loads(proc.stdout)  # stdout is pure JSON in sarif mode
    run = assert_sarif_shape(doc, "schemaguard")
    errors = [r for r in run["results"] if r["level"] == "error"]
    assert len(errors) == 1  # matches findings_summary count in json mode
    assert errors[0]["ruleId"] == "contract-violation"
    loc = errors[0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == str(out)


def test_sarif_validate_clean(tmp_path):
    schema = tmp_path / "contract.json"
    schema.write_text(json.dumps(CONTRACT_P2))
    out = tmp_path / "o.json"
    out.write_text(json.dumps({"id": "x"}))
    proc = run_cli("validate", str(out), "--schema", str(schema),
                   "--format", "sarif")
    assert proc.returncode == 0
    run = assert_sarif_shape(json.loads(proc.stdout), "schemaguard")
    assert [r for r in run["results"] if r["level"] == "error"] == []


def test_sarif_diff(tmp_path):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    old.write_text(json.dumps({"type": "object",
                               "properties": {"a": {"type": "string"}}}))
    new.write_text(json.dumps({"type": "object", "properties": {}}))
    proc = run_cli("diff", str(old), str(new), "--format", "sarif")
    assert proc.returncode == 1
    run = assert_sarif_shape(json.loads(proc.stdout), "schemaguard")
    errors = [r for r in run["results"] if r["level"] == "error"]
    assert errors and all(r["ruleId"] == "breaking-change"
                          for r in errors)


def test_validate_stdin_json_and_jsonl(tmp_path):
    schema = tmp_path / "contract.json"
    schema.write_text(json.dumps(CONTRACT_P2))
    bad = subprocess.run(
        [sys.executable, str(CLI), "validate", "-", "--schema",
         str(schema), "--format", "json"],
        input=json.dumps({"id": 7}), capture_output=True, text=True)
    assert bad.returncode == 1
    data = json.loads(bad.stdout)
    assert data["results"][0]["source"] == "<stdin>"
    good = subprocess.run(
        [sys.executable, str(CLI), "validate", "-", "--schema",
         str(schema), "--format", "json"],
        input='{"id": "a"}\n{"id": "b"}\n',
        capture_output=True, text=True)
    assert good.returncode == 0
    assert json.loads(good.stdout)["summary"]["total"] == 2


def test_diff_stdin_one_side(tmp_path):
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"type": "object",
                               "properties": {"a": {"type": "string"}}}))
    proc = subprocess.run(
        [sys.executable, str(CLI), "diff", str(old), "-",
         "--format", "json"],
        input=json.dumps({"type": "object", "properties": {}}),
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["breaking_count"] >= 1
    both = subprocess.run(
        [sys.executable, str(CLI), "diff", "-", "-"],
        input="{}", capture_output=True, text=True)
    assert both.returncode == 2  # stdin can only be read once
