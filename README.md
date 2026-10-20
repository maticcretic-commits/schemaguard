# SchemaGuard 🛡️

**Contracts are for agents too.**

Downstream systems consume agent outputs sight unseen: a pipeline
reads the JSON, a dashboard renders it, a webhook forwards it. Then
one run renames `email` to `email_address`, invents a
`confidence_score` nobody asked for, or quietly returns null for the
field everything depends on — and nothing complains until the
consumer breaks.

SchemaGuard contract-checks saved agent outputs. Validate a batch
against a JSON Schema contract and get per-file PASS/FAIL with the
exact path of every violation. Infer a baseline schema from the
outputs you trust, and diff contract versions for breaking changes
before a rename ships.

Offline and dependency-free: the validator is a pragmatic,
honestly-documented subset of JSON Schema (below). Anything outside
the subset is reported as an **UNSUPPORTED** keyword warning —
never silently treated as passing.

```bash
python src/schemaguard.py validate outputs/ --schema contract.json
python src/schemaguard.py snapshot outputs/ --out baseline.schema.json
python src/schemaguard.py diff contract.v1.json contract.v2.json
```

## The commands

| Command | What it does |
|---------|--------------|
| `validate <file-or-dir> --schema contract.json` | Validate every `.json` output (and each line of every `.jsonl`) against the contract. Per-file PASS/FAIL with precise failure paths, a batch pass rate, `--format json` for scripts. Exit `1` if any output fails. |
| `validate ... --strict` | Also fails on fields the schema does not declare — hallucinated-field detection — even when the schema never sets `additionalProperties`. |
| `snapshot <outputs-dir> --out baseline.schema.json` | Infer a baseline schema from sample outputs: types merged per field, `required` = present in *every* sample, per-field presence counts reported. The result is a valid schema you can feed straight back to `validate`. |
| `diff <old.schema.json> <new.schema.json>` | Breaking-change report between two contract versions. Exit `1` if anything breaking is found. |
| `init` | Scaffold a sample contract, two sample outputs (one passing, one failing), and an `EXPECTED.md`. |

Exit codes everywhere: `0` = clean, `1` = contract violations /
breaking changes, `2` = usage or IO error. Drop `validate` and
`diff` in GitHub Actions and a drifting agent output fails the build
like a test would.

## What the validator enforces

A pragmatic subset of JSON Schema, enforced exactly as documented:

- **Types** — `type` (including type lists); `integer` widens into
  `number`, never the reverse; `true` is a boolean, not a number.
- **Objects** — `required`, `properties`, `additionalProperties`
  (as `false` or as a schema applied to extra fields).
- **Arrays** — `items` (a single schema), `minItems`, `maxItems`.
- **Strings** — `minLength`, `maxLength`, `pattern`, and `format`
  assertions for `email`, `uri`, `url`, `date`, `date-time`, `uuid`
  (regex/heuristic checks — a format *assertion*, not a parser).
- **Numbers** — `minimum`, `maximum`, `exclusiveMinimum`,
  `exclusiveMaximum` (numeric form; the draft-4 boolean form works
  too).
- **Values** — `enum` and `const`, compared exactly (`1`, `1.0` and
  `true` are three different values here).

Null or empty where the contract expects a populated value of another
type fails with a clear path and message — silent data loss is the
enemy this tool exists to catch.

**Outside the subset:** `$ref`, `allOf`, `anyOf`, tuple-form `items`,
and friends are *not* enforced. They are collected, counted and
printed as warnings in every report (and in the JSON output), so a
contract that leans on them can never look more verified than it is.
Unknown `format` values warn the same way.

## Quick start

```bash
# 1. Scaffold a sample contract + outputs and see what to expect:
schemaguard init

# 2. Validate a batch of agent outputs against your contract:
schemaguard validate outputs/ --schema contract.json

# 3. No contract yet? Infer the baseline from outputs you trust:
schemaguard snapshot outputs/ --out baseline.schema.json

# 4. Before adopting a new contract version, diff it:
schemaguard diff contract.v1.json contract.v2.json
```

## Example

From `examples/` — a contract for a support-ticket agent, and three
outputs. One is clean. One renamed a field. One invented a field the
contract never mentions — inside `user`, where the contract doesn't
set `additionalProperties`, so it takes `--strict` to catch it:

```
$ schemaguard validate examples/outputs --schema examples/contract.json
====================================================================
SchemaGuard — validate report
====================================================================
Schema:  examples/contract.json
Outputs: 3 · 2 passed · 1 failed · pass rate 66.7%

✅ examples/outputs/clean.json — PASS
✅ examples/outputs/hallucinated-field.json — PASS
❌ examples/outputs/renamed-field.json — FAIL
   $.user.email: missing required field

❌ FAIL — 1 of 3 output(s) violate the contract

$ schemaguard validate examples/outputs/hallucinated-field.json --schema examples/contract.json --strict
====================================================================
SchemaGuard — validate report
====================================================================
Schema:  examples/contract.json · strict mode ON
Outputs: 1 · 0 passed · 1 failed · pass rate 0.0%

❌ examples/outputs/hallucinated-field.json — FAIL
   $.user.confidence_score: unexpected field (strict mode: not declared in the schema)

❌ FAIL — 1 of 1 output(s) violate the contract
```

Don't have a contract yet? `snapshot` infers the shape you actually
ship — and its presence counts expose drift at a glance (`email`
shows up in only 2 of 3 outputs; `email_address` and the invented
`confidence_score` appear once each):

```
$ schemaguard snapshot examples/outputs --out baseline.schema.json
Snapshot: inferred a baseline schema from 3 sample(s) → baseline.schema.json
Field presence (present / samples):
  $.id: 3/3
  $.status: 3/3
  $.summary: 3/3
  $.tags: 1/3
  $.user: 3/3
  $.user.confidence_score: 1/3
  $.user.email: 2/3
  $.user.email_address: 1/3
  $.user.name: 3/3
```

And when the contract itself changes, `diff` separates the breaking
from the benign before the new version ships:

```
$ schemaguard diff examples/contract.json examples/contract-v2.json
====================================================================
SchemaGuard — schema diff
====================================================================
Old: examples/contract.json
New: examples/contract-v2.json

🟢 ok       $.channel: [field-added] field 'channel' was added
🔴 BREAKING $.channel: [field-now-required] field 'channel' is now required
🔴 BREAKING $.id: [type-changed] type changed from integer to string
🔴 BREAKING $.status: [enum-value-removed] enum value "pending" was removed
🟢 ok       $.status: [enum-value-added] enum value "closed" was added

❌ 3 breaking change(s) — outputs valid under the old contract can fail the new one
```

Breaking means: a field was removed, a field's type changed, a field
became required, or an enum value disappeared. Adding an optional
field, adding an enum value, or relaxing a requirement is reported
as non-breaking.

## Why

Agent outputs are an API — usually an accidental one. The producer
renames a field on a Tuesday, the consumer finds out on Friday, and
nobody can say which runs in between were malformed. SchemaGuard
makes the contract explicit, versioned and diffable: validate what
the agent produced, snapshot what it *actually* produces, and never
let a contract change ship without knowing whether it breaks the
consumers you already have.

## Part of AgentForge

SchemaGuard is the sixth tool in the [AgentForge](https://github.com/maticcretic-commits)
trust layer for AI agents:

- **AgentProof** — verify agent work — [github.com/maticcretic-commits/agentproof](https://github.com/maticcretic-commits/agentproof)
- **PromptRE** — reverse-engineer agent prompts — [github.com/maticcretic-commits/promptre](https://github.com/maticcretic-commits/promptre)
- **AgentCI** — regression harness for agents — [github.com/maticcretic-commits/agentci](https://github.com/maticcretic-commits/agentci)
- **SecretScan** — scan agent outputs for leaked secrets — [github.com/maticcretic-commits/secretscan](https://github.com/maticcretic-commits/secretscan)
- **CostLedger** — cost accounting for agent runs — [github.com/maticcretic-commits/costledger](https://github.com/maticcretic-commits/costledger)
- **SchemaGuard** — validate agent outputs against their contracts (this repo)
- **SkillAudit** — audit agent skills for hidden instructions and quality — [github.com/maticcretic-commits/skillaudit](https://github.com/maticcretic-commits/skillaudit)
- **PromptDiff** — diff prompt versions — [github.com/maticcretic-commits/promptdiff](https://github.com/maticcretic-commits/promptdiff)
- **TraceTape** — the canonical run tape — [github.com/maticcretic-commits/tracetape](https://github.com/maticcretic-commits/tracetape)
- **PromptLint** — lint prompts for quality defects — [github.com/maticcretic-commits/promptlint](https://github.com/maticcretic-commits/promptlint)

AgentProof checks what an agent *did*; PromptRE checks what it was *told*; AgentCI checks it still *works* after you change it; SecretScan checks that nothing *leaked*; CostLedger checks what it *cost*; SchemaGuard checks the output still *fits the contract*; SkillAudit checks the skill was *safe to install*; PromptDiff checks what the prompt change *changed*; TraceTape keeps the whole run on *tape*; PromptLint keeps the prompt itself *linted clean*.

## License

MIT
