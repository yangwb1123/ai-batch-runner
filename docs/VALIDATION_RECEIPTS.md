# Artifact validation receipts

## Deliberate behavior change: no output target is not a passing gate

A task with configured validators must have an artifact `output` target.
Previously `_save_validated()` printed the answer and returned success when
`Task.output_path()` was `None`, silently skipping every configured validator.
It now refuses publication, marks validation unsuccessful, and reports the
bounded reason:

> validation failed: configured validator requires an artifact output target

No validator runs in this case, so no invocation receipt is invented. Agent
execution may already have happened: this refusal does **not** roll back code
changes or external side effects. Do not blindly replay an uncertain operation.

An explicit task/stage `validate: ""` (or stage `validate_cmd: ""`) still
intentionally disables the inherited gate. Ungated stdout remains allowed.
With an artifact target, existing filtering, parallel validator execution,
declaration-order AND decisions, JSON status and judge-verdict semantics,
symlink refusal, rejected diagnostics and atomic publication remain in place.
Repo-scoped validators are still deferred; non-applicable validators are still
skipped. Neither is recorded as a file-validator invocation.

## What is recorded

`pbatch/validation_receipts.py` observes the existing execution call, without
running another command or changing `ValidationResult`. Fresh artifact gates,
`revalidate_existing()`, the compatibility `_revalidate_one()` helper and
Gate-fix artifact validation each attempt one receipt per actual invocation.
Meta uses the same `_save_validated()` path. Existing function signatures stay
compatible; cancellation is forwarded to the actual validator. Parallel receipts
can append in completion order; gate decisions still fold in declaration order.

The ledger is `config.data_dir(cwd)/validation/receipts.jsonl`, separate from
pipeline stage receipts and optional decision logs. Each JSON object contains:

| Field | Meaning |
| --- | --- |
| `schema_version`, `kind` | `1`, `validator_invocation` |
| `command_hash` | Full SHA-256 of the resolved command template, before temporary-path expansion; direct observer calls hash the supplied command |
| `scope` | `task`, `reuse`, or `gate-fix` |
| `task_id_hash`, `stage_id_hash` | SHA-256 identities when execution IDs are available, otherwise `null` |
| `artifact_path_hash` | SHA-256 of the intended artifact path (not the temporary filename for fresh tasks) |
| `exit_code`, `timed_out`, `ok` | The unmodified executor outcome; unknown if the call raised without returning a result |
| `duration_seconds` | Measured wall duration around the actual call only, using `perf_counter`; excludes identity reads and receipt writes |
| `artifact_bytes` | Observed complete regular-file size |
| `artifact_digest`, `artifact_digest_bytes` | Full SHA-256 of at most the first 64 KiB, and the number of bytes actually hashed |

Missing, non-regular, unreadable or detectably changing artifact identities are
`null`, not empty success or fabricated zero. An actually empty file can have
size zero and the SHA-256 of empty bytes. Unavailable/non-finite/backwards
clocks or a duration below measurable clock resolution produce `null` duration.
A raised executor call retains `null` outcome fields and still propagates its
original exception. The executor's existing `-1` timeout/crash sentinel is
preserved; it is not a newly observed native process exit code.

**`ok` is the executor result, not the complete artifact/Gate verdict.** An
exit-zero validator emitting JSON `status=fail`, or a rejecting/missing judge
verdict, still blocks publication/reuse even though its process receipt says
`ok: true`. Receipt presence, digest or process success never authorizes reuse,
downstream execution, goal acceptance or release. The prefix digest is bounded
change-detection evidence, not a full-file integrity proof or atomic snapshot.

Gate-fix scope recognizes the existing `stage: <name>-fix` metadata together
with its `<name>-fix-<round>.md` output naming; it does not inspect prompt text
or change Gate-fix permissions. Ordinary tasks remain task-scoped.

## Bounds, privacy and failure handling

Storage reuses `pipeline_receipts` byte/line budgets, tail reads and rotation:

- Ledger at most 4 MiB; each observation at most 8 KiB.
- On rotation, retain at most 2,000 recent complete lines within the remaining
  byte budget; rotation uses atomic replacement. No growing backup chain.
- Reads inspect at most the bounded ledger tail and return at most 1,000
  records. Missing/corrupt/foreign records do not become successful evidence.
- New ledger directories are mode `0700`, new files `0600`; unsafe existing
  targets are refused rather than adopted or chmodded. In-process validator
  writers serialize append/rotation. Existing workspace/run ownership remains
  necessary; this is not a new cross-process transaction or lease.
- No implicit repository output directory is created. Even a runtime override
  inside the execution workspace causes this observation to be dropped, not a
  repository runtime write or a changed execution decision.

Receipts contain no prompts, artifact bodies, stdout/stderr, raw paths, raw
commands/arguments, environment values or diagnostic exception text. Command
and identity hashes are deterministic, unkeyed identities, not encryption;
operators must still keep credentials out of command arguments.

Observation failures (including serialization, clock, filesystem and rotation
failures) log only bounded debug diagnostics and never turn a passing validator
into failure or a failing validator into success. There is one append attempt,
not a receipt retry or second execution. Abrupt termination, failed writes and
rotation can lose evidence: this is **not a durable exactly-once audit log**.
Unknown/missing receipts must remain missing evidence, never a synthesized PASS.
The best-effort policy does not apply to required validation, authorization or
other mandatory execution records.

## Regression checks

`tests/test_validation_receipts.py` covers the old silent skip, intentional
empty override, artifact publication/rejection, invocation outcomes and measured
or unknown duration, reuse/Gate-fix, single execution, write-failure degradation,
private bounded storage/rotation, leakage prevention, and declaration-order AND
folding. These local fixtures do not prove real-provider, production, seven-day
unattended or actual-phone acceptance.
