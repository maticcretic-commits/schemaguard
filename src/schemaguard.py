#!/usr/bin/env python3
"""
SchemaGuard — "Contracts are for agents too."

Downstream systems consume agent outputs sight unseen: a pipeline
reads the JSON, a dashboard renders it, a webhook forwards it. Then one
run renames `email` to `email_address`, invents a `confidence_score`
nobody asked for, or quietly returns null for the field everything
depends on — and nothing complains until the consumer breaks.

SchemaGuard contract-checks saved agent outputs. Validate a batch of
outputs against a JSON Schema contract and get per-file PASS/FAIL with
the exact path of every violation. Infer a baseline schema from the
outputs you trust (`snapshot`), then diff contract versions for
breaking changes (`diff`) before a rename ships.

Offline and dependency-free: the validator is a pragmatic,
honestly-documented subset of JSON Schema (see README). Anything
outside the subset is reported as an UNSUPPORTED keyword warning —
never silently treated as passing.

Usage:
    schemaguard validate <file-or-dir> --schema contract.json
                        [--format text|json] [--strict]
    schemaguard snapshot <outputs-dir> --out baseline.schema.json
    schemaguard diff <old.schema.json> <new.schema.json> [--format text|json]
    schemaguard init
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

VERSION = "1.0.0"

TYPE_ORDER = ("null", "boolean", "integer", "number", "string",
              "array", "object")

# Keywords this validator enforces. Everything else found in a schema
# is either an annotation (ignored silently, as JSON Schema intends)
# or UNSUPPORTED — warned about, counted, never enforced.
ENFORCED_KEYWORDS = {
    "type", "enum", "const",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "pattern", "format",
    "minItems", "maxItems", "items",
    "required", "properties", "additionalProperties",
}
ANNOTATION_KEYWORDS = {
    "$schema", "$id", "$comment", "title", "description", "default",
    "examples", "example", "readOnly", "writeOnly", "deprecated",
}

KNOWN_FORMATS = ("email", "uri", "url", "date", "date-time", "uuid")


# ---------------------------------------------------------------------------
# JSON value helpers
# ---------------------------------------------------------------------------

def json_type_name(value):
    """The JSON Schema type name for a parsed JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"not a JSON value: {value!r}")


def type_matches(value, declared):
    actual = json_type_name(value)
    if declared == "number":
        return actual in ("integer", "number")
    return actual == declared


def canon(value):
    """Canonical string for enum/const comparison (keeps 1, 1.0 and
    true distinct, which Python's == would happily conflate)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def child_path(path, key):
    if _IDENT_RE.match(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key)}]"


# ---------------------------------------------------------------------------
# Format assertions (regex/heuristic, per the documented subset)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:\S+$")
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://\S+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?"
    r"([Zz]|[+-]\d{2}:?\d{2})?$")


def check_format(fmt, value):
    """True/False for a format assertion, None for an unknown format."""
    if fmt == "email":
        return bool(_EMAIL_RE.match(value))
    if fmt == "uuid":
        return bool(_UUID_RE.match(value))
    if fmt == "uri":
        return bool(_URI_RE.match(value))
    if fmt == "url":
        return bool(_URL_RE.match(value))
    if fmt == "date":
        if not _DATE_RE.match(value):
            return False
        try:
            date.fromisoformat(value)
            return True
        except ValueError:
            return False
    if fmt == "date-time":
        if not _DATETIME_RE.match(value):
            return False
        try:
            datetime.fromisoformat(
                value.replace("Z", "+00:00").replace("z", "+00:00"))
            return True
        except ValueError:
            return False
    return None


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def _err(errors, path, message):
    errors.append({"path": path, "message": message})


def validate_instance(instance, schema, path="$", schema_path="$schema",
                      strict=False, errors=None, warnings=None):
    """Validate one parsed JSON value against a schema (dict or bool).

    Appends {"path", "message"} dicts to `errors` and counts
    unsupported-keyword / unknown-format warnings into `warnings`
    (a Counter keyed by message). Returns (errors, warnings)."""
    if errors is None:
        errors = []
    if warnings is None:
        warnings = Counter()
    if isinstance(schema, bool):
        if not schema:
            _err(errors, path, "no value is allowed here (schema is false)")
        return errors, warnings
    if not isinstance(schema, dict):
        warnings[f"schema at {schema_path} is not an object — not enforced"] += 1
        return errors, warnings

    for kw in schema:
        if kw not in ENFORCED_KEYWORDS and kw not in ANNOTATION_KEYWORDS:
            warnings[f"unsupported keyword '{kw}' at {schema_path} "
                     f"— ignored, NOT enforced"] += 1

    # -- type -------------------------------------------------------------
    if "type" in schema:
        declared = schema["type"]
        names = declared if isinstance(declared, list) else [declared]
        if not any(type_matches(instance, n) for n in names):
            want = " or ".join(str(n) for n in names)
            _err(errors, path,
                 f"expected {want}, got {json_type_name(instance)}")

    # -- enum / const -------------------------------------------------------
    if "enum" in schema and isinstance(schema["enum"], list):
        allowed = {canon(v) for v in schema["enum"]}
        if canon(instance) not in allowed:
            shown = ", ".join(json.dumps(v) for v in schema["enum"])
            _err(errors, path, f"value {json.dumps(instance)} is not "
                               f"one of [{shown}]")
    if "const" in schema:
        if canon(instance) != canon(schema["const"]):
            _err(errors, path, f"expected constant "
                               f"{json.dumps(schema['const'])}, "
                               f"got {json.dumps(instance)}")

    # -- numbers ------------------------------------------------------------
    if is_number(instance):
        if "minimum" in schema and instance < schema["minimum"]:
            _err(errors, path, f"{instance} is below minimum "
                               f"{schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            _err(errors, path, f"{instance} exceeds maximum "
                               f"{schema['maximum']}")
        ex_min = schema.get("exclusiveMinimum")
        if isinstance(ex_min, (int, float)) and not isinstance(ex_min, bool):
            if instance <= ex_min:
                _err(errors, path, f"{instance} is not above "
                                   f"exclusiveMinimum {ex_min}")
        elif ex_min is True and "minimum" in schema:
            if instance <= schema["minimum"]:
                _err(errors, path, f"{instance} is not above "
                                   f"minimum {schema['minimum']} (exclusive)")
        ex_max = schema.get("exclusiveMaximum")
        if isinstance(ex_max, (int, float)) and not isinstance(ex_max, bool):
            if instance >= ex_max:
                _err(errors, path, f"{instance} is not below "
                                   f"exclusiveMaximum {ex_max}")
        elif ex_max is True and "maximum" in schema:
            if instance >= schema["maximum"]:
                _err(errors, path, f"{instance} is not below "
                                   f"maximum {schema['maximum']} (exclusive)")

    # -- strings --------------------------------------------------------------
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            _err(errors, path, f"string of length {len(instance)} is "
                               f"shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            _err(errors, path, f"string of length {len(instance)} exceeds "
                               f"maxLength {schema['maxLength']}")
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], instance):
                    _err(errors, path, f"{json.dumps(instance)} does not "
                                       f"match pattern "
                                       f"{json.dumps(schema['pattern'])}")
            except re.error as e:
                warnings[f"invalid pattern at {schema_path}.pattern "
                         f"({e}) — not enforced"] += 1
        if "format" in schema:
            fmt = schema["format"]
            ok = check_format(fmt, instance)
            if ok is None:
                warnings[f"unknown format '{fmt}' at {schema_path} "
                         f"— not asserted"] += 1
            elif not ok:
                _err(errors, path, f"expected format {fmt}")

    # -- arrays ---------------------------------------------------------------
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            _err(errors, path, f"array of {len(instance)} item(s) is "
                               f"below minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            _err(errors, path, f"array of {len(instance)} item(s) exceeds "
                               f"maxItems {schema['maxItems']}")
        if "items" in schema:
            item_schema = schema["items"]
            if isinstance(item_schema, (dict, bool)):
                for i, item in enumerate(instance):
                    validate_instance(item, item_schema, f"{path}[{i}]",
                                      f"{schema_path}.items", strict,
                                      errors, warnings)
            else:
                warnings[f"'items' at {schema_path} is a list (tuple "
                         f"validation) — unsupported, NOT enforced"] += 1

    # -- objects ----------------------------------------------------------------
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            props = {}
        for name in schema.get("required", []):
            if name not in instance:
                _err(errors, child_path(path, name),
                     "missing required field")
        for key, subschema in props.items():
            if key in instance:
                validate_instance(instance[key], subschema,
                                  child_path(path, key),
                                  f"{schema_path}.properties.{key}",
                                  strict, errors, warnings)
        extras = [k for k in instance if k not in props]
        if "additionalProperties" in schema:
            ap = schema["additionalProperties"]
            if ap is False:
                for key in extras:
                    _err(errors, child_path(path, key),
                         "unexpected field "
                         "(additionalProperties is false)")
            elif isinstance(ap, dict):
                for key in extras:
                    validate_instance(instance[key], ap,
                                      child_path(path, key),
                                      f"{schema_path}.additionalProperties",
                                      strict, errors, warnings)
        elif strict:
            for key in extras:
                _err(errors, child_path(path, key),
                     "unexpected field (strict mode: not declared "
                     "in the schema)")

    return errors, warnings


# ---------------------------------------------------------------------------
# Batch validation
# ---------------------------------------------------------------------------

def iter_output_files(target):
    """All .json / .jsonl files under a file-or-dir target, sorted."""
    p = Path(target)
    if p.is_file():
        return [p]
    found = [f for f in p.rglob("*")
             if f.is_file() and f.suffix in (".json", ".jsonl")]
    return sorted(found, key=lambda f: str(f))


def collect_outputs(target):
    """Parse every output under target into records:
    {"source", "value", "parse_error"}. A .json file is one record;
    each non-blank .jsonl line is one record (label file:line)."""
    records = []
    for f in iter_output_files(target):
        if f.suffix == ".jsonl":
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError as e:
                records.append({"source": str(f), "value": None,
                                "parse_error": f"cannot read file: {e}"})
                continue
            for lineno, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                label = f"{f}:{lineno}"
                try:
                    records.append({"source": label,
                                    "value": json.loads(line),
                                    "parse_error": None})
                except json.JSONDecodeError as e:
                    records.append({"source": label, "value": None,
                                    "parse_error": f"invalid JSON: {e}"})
        else:
            try:
                text = f.read_text(encoding="utf-8")
            except OSError as e:
                records.append({"source": str(f), "value": None,
                                "parse_error": f"cannot read file: {e}"})
                continue
            try:
                records.append({"source": str(f), "value": json.loads(text),
                                "parse_error": None})
            except json.JSONDecodeError as e:
                records.append({"source": str(f), "value": None,
                                "parse_error": f"invalid JSON: {e}"})
    return records


def run_validation(target, schema, strict=False):
    """Validate every output under target. Returns the report dict."""
    records = collect_outputs(target)
    warnings = Counter()
    results = []
    for rec in records:
        if rec["parse_error"] is not None:
            results.append({"source": rec["source"], "status": "fail",
                            "errors": [{"path": "$",
                                        "message": rec["parse_error"]}]})
            continue
        errors, _ = validate_instance(rec["value"], schema, strict=strict,
                                      warnings=warnings)
        results.append({"source": rec["source"],
                        "status": "fail" if errors else "pass",
                        "errors": errors})
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    return {
        "strict": strict,
        "results": results,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed * 100.0 / total, 1) if total else 0.0,
        },
        "warnings": [{"message": msg, "count": n}
                     for msg, n in sorted(warnings.items())],
    }


# ---------------------------------------------------------------------------
# Schema inference (snapshot)
# ---------------------------------------------------------------------------

def infer_schema(values, path="$", stats=None):
    """Infer a schema node from a list of sample values.

    Types merge across samples (integer widens into number); for
    objects, properties are the union of keys, required = keys present
    in EVERY object sample, and per-field presence counts land in
    `stats` as {json-path: {"present": n, "of": m}}."""
    if stats is None:
        stats = {}
    seen = {json_type_name(v) for v in values}
    if "number" in seen:
        seen.discard("integer")  # integer widens into number
    ordered = [t for t in TYPE_ORDER if t in seen]
    node = {}
    node["type"] = ordered[0] if len(ordered) == 1 else ordered

    objects = [v for v in values if isinstance(v, dict)]
    if objects:
        keys = set()
        for o in objects:
            keys.update(o.keys())
        props = {}
        for key in sorted(keys):
            present = [o[key] for o in objects if key in o]
            stats[child_path(path, key)] = {"present": len(present),
                                            "of": len(objects)}
            props[key] = infer_schema(present, child_path(path, key), stats)
        node["properties"] = props
        required = sorted(k for k in keys
                          if all(k in o for o in objects))
        if required:
            node["required"] = required

    arrays = [v for v in values if isinstance(v, list)]
    if arrays:
        elements = [e for a in arrays for e in a]
        if elements:
            node["items"] = infer_schema(elements, f"{path}[]", stats)
    return node


def snapshot_schema(target):
    """Infer a baseline schema from every output under target.
    Returns (schema, stats, sample_count, load_errors)."""
    records = collect_outputs(target)
    samples, load_errors = [], []
    for rec in records:
        if rec["parse_error"] is not None:
            load_errors.append(f"{rec['source']}: {rec['parse_error']}")
        else:
            samples.append(rec["value"])
    if not samples:
        return None, {}, 0, load_errors
    stats = {}
    schema = infer_schema(samples, "$", stats)
    return schema, stats, len(samples), load_errors


# ---------------------------------------------------------------------------
# Schema diff
# ---------------------------------------------------------------------------

def _type_set(schema):
    t = schema.get("type")
    if isinstance(t, list):
        return {str(x) for x in t}
    if isinstance(t, str):
        return {t}
    return None


def _type_label(type_set):
    return " or ".join(t for t in TYPE_ORDER if t in type_set) or \
        ", ".join(sorted(type_set))


def diff_schemas(old, new, path="$", changes=None):
    """Compare two schemas; append {"path", "change", "breaking",
    "detail"} records for every difference that matters to a consumer
    of outputs validated against the OLD schema."""
    if changes is None:
        changes = []
    if not isinstance(old, dict) or not isinstance(new, dict):
        return changes

    def add(change, breaking, detail, at=None):
        changes.append({"path": at if at is not None else path,
                        "change": change, "breaking": breaking,
                        "detail": detail})

    old_types, new_types = _type_set(old), _type_set(new)
    if old_types is not None and new_types is not None \
            and old_types != new_types:
        add("type-changed", True,
            f"type changed from {_type_label(old_types)} "
            f"to {_type_label(new_types)}")

    if "enum" in old and "enum" in new:
        old_vals = {canon(v): v for v in old["enum"]}
        new_vals = {canon(v): v for v in new["enum"]}
        for k in sorted(set(old_vals) - set(new_vals)):
            add("enum-value-removed", True,
                f"enum value {json.dumps(old_vals[k])} was removed")
        for k in sorted(set(new_vals) - set(old_vals)):
            add("enum-value-added", False,
                f"enum value {json.dumps(new_vals[k])} was added")
    elif "enum" in old:
        add("enum-removed", False,
            "enum constraint was dropped (any value now allowed)")
    elif "enum" in new:
        add("enum-added", True,
            "an enum constraint was added where any value was allowed")

    old_props = old.get("properties", {}) if isinstance(
        old.get("properties", {}), dict) else {}
    new_props = new.get("properties", {}) if isinstance(
        new.get("properties", {}), dict) else {}
    for key in sorted(set(old_props) - set(new_props)):
        add("field-removed", True, f"field '{key}' was removed",
            at=child_path(path, key))
    for key in sorted(set(new_props) - set(old_props)):
        add("field-added", False, f"field '{key}' was added",
            at=child_path(path, key))

    old_req = set(old.get("required", []))
    new_req = set(new.get("required", []))
    for key in sorted(new_req - old_req):
        add("field-now-required", True,
            f"field '{key}' is now required", at=child_path(path, key))
    for key in sorted(old_req - new_req):
        add("field-no-longer-required", False,
            f"field '{key}' is no longer required",
            at=child_path(path, key))

    for key in sorted(set(old_props) & set(new_props)):
        diff_schemas(old_props[key], new_props[key],
                     child_path(path, key), changes)
    if isinstance(old.get("items"), dict) \
            and isinstance(new.get("items"), dict):
        diff_schemas(old["items"], new["items"], f"{path}[]", changes)
    return changes


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def print_warnings(warnings, stream=None):
    if not warnings:
        return
    stream = stream or sys.stdout
    total = sum(w["count"] for w in warnings)
    print(f"⚠️  {total} unsupported-schema warning(s) — these parts of "
          f"the contract were NOT enforced:", file=stream)
    for w in warnings:
        suffix = f" (×{w['count']})" if w["count"] > 1 else ""
        print(f"   - {w['message']}{suffix}", file=stream)


def load_schema_file(path):
    """Load a schema JSON file. Returns (schema, None) or (None, error)."""
    p = Path(path)
    if not p.is_file():
        return None, f"schema file not found: {path}"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, f"cannot parse schema {path}: {e}"
    if not isinstance(data, (dict, bool)):
        return None, f"schema {path} must be a JSON object (or true/false)"
    return data, None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _caller_error(message, code):
    """Caller error (exit 2): the human line plus one JSON error-envelope
    line, both to stderr (see CONVENTIONS.md)."""
    print(f"error: {message}", file=sys.stderr)
    print(json.dumps({"error": {"code": code, "message": message}}),
          file=sys.stderr)
    sys.exit(2)


def cmd_validate(args):
    target = Path(args.path)
    if not target.exists():
        _caller_error(f"path not found: {args.path}", "file_not_found")
    schema, err = load_schema_file(args.schema)
    if err:
        _caller_error(err, "invalid_schema")
    report = run_validation(args.path, schema, strict=args.strict)
    if report["summary"]["total"] == 0:
        _caller_error(
            f"no .json/.jsonl outputs found under {args.path}",
            "no_outputs")
    if args.format == "json":
        error_count = sum(len(r["errors"]) for r in report["results"])
        highest = "HIGH" if report["summary"]["failed"] else (
            "INFO" if report["warnings"] else "NONE")
        out = {"tool": "schemaguard", "version": VERSION,
               "schema_version": 1,
               "schema": args.schema, **report,
               "findings_summary": {"count": error_count,
                                    "highest_severity": highest}}
        print(json.dumps(out, indent=2))
    else:
        s = report["summary"]
        print("=" * 68)
        print("SchemaGuard — validate report")
        print("=" * 68)
        print(f"Schema:  {args.schema}"
              f"{' · strict mode ON' if args.strict else ''}")
        print(f"Outputs: {s['total']} · {s['passed']} passed · "
              f"{s['failed']} failed · pass rate {s['pass_rate']}%")
        print()
        for r in report["results"]:
            if r["status"] == "pass":
                print(f"✅ {r['source']} — PASS")
            else:
                print(f"❌ {r['source']} — FAIL")
                for e in r["errors"]:
                    print(f"   {e['path']}: {e['message']}")
        print()
        print_warnings(report["warnings"])
        if s["failed"]:
            print(f"❌ FAIL — {s['failed']} of {s['total']} output(s) "
                  f"violate the contract")
        else:
            print(f"✅ PASS — all {s['total']} output(s) fit the contract")
    sys.exit(1 if report["summary"]["failed"] else 0)


def cmd_snapshot(args):
    target = Path(args.path)
    if not target.exists():
        _caller_error(f"path not found: {args.path}", "file_not_found")
    schema, stats, count, load_errors = snapshot_schema(args.path)
    for e in load_errors:
        print(f"warning: skipped {e}", file=sys.stderr)
    if schema is None:
        _caller_error(
            f"no parseable outputs found under {args.path}", "no_outputs")
    out = Path(args.out)
    out.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"Snapshot: inferred a baseline schema from {count} sample(s) "
          f"→ {out}")
    print("Field presence (present / samples):", file=sys.stderr)
    for p in sorted(stats):
        st = stats[p]
        print(f"  {p}: {st['present']}/{st['of']}", file=sys.stderr)
    print("Use it directly:  schemaguard validate <outputs> "
          f"--schema {out}")
    sys.exit(0)


def cmd_diff(args):
    old, err = load_schema_file(args.old)
    if err:
        _caller_error(err, "invalid_schema")
    new, err = load_schema_file(args.new)
    if err:
        _caller_error(err, "invalid_schema")
    changes = diff_schemas(old, new)
    breaking = [c for c in changes if c["breaking"]]
    if args.format == "json":
        highest = "HIGH" if breaking else (
            "INFO" if changes else "NONE")
        print(json.dumps({"tool": "schemaguard", "version": VERSION,
                          "schema_version": 1,
                          "old": args.old, "new": args.new,
                          "breaking_count": len(breaking),
                          "changes": changes,
                          "findings_summary": {
                              "count": len(breaking),
                              "highest_severity": highest}}, indent=2))
    else:
        print("=" * 68)
        print("SchemaGuard — schema diff")
        print("=" * 68)
        print(f"Old: {args.old}")
        print(f"New: {args.new}")
        print()
        if not changes:
            print("✅ No differences that matter — the contracts agree "
                  "on every checked field, type, requirement and enum.")
        else:
            for c in changes:
                icon = "🔴 BREAKING" if c["breaking"] else "🟢 ok      "
                print(f"{icon} {c['path']}: [{c['change']}] {c['detail']}")
            print()
            if breaking:
                print(f"❌ {len(breaking)} breaking change(s) — outputs "
                      f"valid under the old contract can fail the new one")
            else:
                print("✅ No breaking changes — all differences are "
                      "backward-compatible")
    sys.exit(1 if breaking else 0)


CONTRACT_TEMPLATE = """\
{
  "type": "object",
  "required": ["id", "status", "summary"],
  "properties": {
    "id": {"type": "string", "format": "uuid"},
    "status": {"type": "string", "enum": ["ok", "error"]},
    "summary": {"type": "string", "minLength": 1},
    "score": {"type": "number", "minimum": 0, "maximum": 1}
  },
  "additionalProperties": false
}
"""

PASS_TEMPLATE = """\
{
  "id": "123e4567-e89b-12d3-a456-426614174000",
  "status": "ok",
  "summary": "Ticket resolved and the customer confirmed the fix.",
  "score": 0.92
}
"""

FAIL_TEMPLATE = """\
{
  "id": "not-a-uuid",
  "status": "done",
  "score": 1.7
}
"""

EXPECTED_TEMPLATE = """\
# What to expect

`contract.json` is a small contract for an agent that answers support
tickets: every output needs a UUID `id`, a `status` of "ok" or
"error", a non-empty `summary`, and may carry a `score` between 0
and 1. Nothing else is allowed (`additionalProperties: false`).

Run:

    schemaguard validate outputs --schema contract.json

- `outputs/output-pass.json` PASSES — every field fits.
- `outputs/output-fail.json` FAILS four ways: the id is not a UUID,
  "done" is not an allowed status, `summary` is missing entirely,
  and the score 1.7 is above the maximum of 1.

Then try the other two commands:

    schemaguard snapshot outputs --out baseline.schema.json
    schemaguard diff contract.json baseline.schema.json

`snapshot` infers what the outputs *actually* look like (note that
`summary` is required in the contract but missing from one sample, so
the inferred schema will not require it). `diff` shows what changed
between the contract you wrote and the shape you observed.
"""


def cmd_init(args):
    base = Path(args.dir) if args.dir else Path.cwd()
    (base / "outputs").mkdir(parents=True, exist_ok=True)
    targets = {"contract.json": CONTRACT_TEMPLATE,
               "outputs/output-pass.json": PASS_TEMPLATE,
               "outputs/output-fail.json": FAIL_TEMPLATE,
               "EXPECTED.md": EXPECTED_TEMPLATE}
    for name, content in targets.items():
        p = base / name
        if p.exists() and not args.force:
            print(f"{p} exists — use --force to overwrite")
            return
        p.write_text(content, encoding="utf-8")
    print(f"created contract.json, outputs/ and EXPECTED.md in {base}")
    print("try it:  schemaguard validate outputs --schema contract.json")


def main():
    ap = argparse.ArgumentParser(
        prog="schemaguard",
        description="Contract-check agent outputs against a JSON "
                    "Schema. Contracts are for agents too.")
    ap.add_argument("--version", action="version",
                    version=f"schemaguard {VERSION}")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI color output (accepted for "
                         "family consistency; output is never colored)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate",
                       help="validate outputs against a schema contract")
    v.add_argument("path", help="output file or directory of outputs")
    v.add_argument("--schema", required=True, help="contract (JSON Schema)")
    v.add_argument("--format", choices=("text", "json"), default="text")
    v.add_argument("--strict", action="store_true",
                   help="fail on fields the schema does not declare, "
                        "even without additionalProperties: false")
    v.set_defaults(fn=cmd_validate)

    s = sub.add_parser("snapshot",
                       help="infer a baseline schema from sample outputs")
    s.add_argument("path", help="output file or directory of outputs")
    s.add_argument("--out", required=True, metavar="FILE",
                   help="where to write the inferred schema")
    s.set_defaults(fn=cmd_snapshot)

    d = sub.add_parser("diff",
                       help="diff two schemas; exit 1 on breaking changes")
    d.add_argument("old", help="the old (baseline) schema")
    d.add_argument("new", help="the new schema")
    d.add_argument("--format", choices=("text", "json"), default="text")
    d.set_defaults(fn=cmd_diff)

    i = sub.add_parser("init",
                       help="scaffold a sample contract + outputs + EXPECTED.md")
    i.add_argument("--force", action="store_true")
    i.add_argument("--dir", help="directory to write the sample files into")
    i.set_defaults(fn=cmd_init)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
