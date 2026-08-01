All verification complete: `make ci` exit 0 (118 pytest + gates + quality OK), plus empirical PoCs confirming the injection-quoting mitigation and two symlink write-through paths. Here is the review.

---

# Security Review — T0 (R0 validator-execution refactor)

**Revision reviewed**: working tree at `4b27031` + uncommitted T0 diff (`pbatch/runner.py`, `pbatch/__init__.py`, `tests/test_pi_batch.py`, `docs/RUNNING_247.md`). **Scope**: the T0 validator-execution refactor only; adjacent pre-existing issues are flagged where they interact with the changed surface.

**Checks actually run** (per `prompts/README.md` evidence standard): `make ci` (exit 0), `python -m pytest tests/` (118 passed), `python quality.py --strict .` (OK), `git diff` review of all 4 files, and three live PoC scripts against `run_validation`/`revalidate_existing`/`save_result`. All material claims below are **Verified** (code + execution), **Partial**, or **Proposed** where stated.

---

## 1. Assets, trust boundaries, attacker capabilities, entry points

**Assets**
| Asset | Location | T0 relevance |
|---|---|---|
| Validator commands (arbitrary shell authority) | `pi-batch.yaml` `validators:` registry (`config.py:70-82`), CLI `--validate/--validate-cmd`, task YAML `validate:`, stage YAML `validate_cmd:` | T0 executes them via new `run_validation()` |
| Agent credentials (provider API keys) in process env | inherited by agent and validator children | validators can read them (`runner.py:217` no env scrubbing) |
| Artifacts (outputs) + integrity chain | `task.output_path()`, temp files, `--log-file`, decision log, git history | T0's temp→rename lifecycle and `revalidate_existing` |
| Agent session state | keyed by `--session-id` derived from `--session-name` (`cli.py:_derive_session_name`) | untouched |
| User workspace / home | validators + agent run with full user privileges, `cwd=task.workdir()` | untouched |

**Trust boundaries**
1. **Config/YAML authors (trusted)** → arbitrary shell + agent invocation with user credentials. Untrusted task/pipeline YAML is equivalent to RCE (by design, pre-existing). T0 adds no new YAML surface.
2. **Agent output (untrusted** — `AGENTS.md` §5 explicitly) → file writes, gate input, downstream prompts. T0's executor is one of the boundary enforcement points (fail-closed, zero-artifact).
3. **Local users sharing a workspace (semi-trusted)** → file-layout attacks (symlinks, artifact tampering, session-id replay).
4. **Validator subprocesses (trusted commands, untrusted output)** → resource exhaustion, output leakage into logs.
5. **Provider network** — handled by the agent CLI; failure signatures reject provider errors (unchanged).

**Entry points for the changed surface**
- CLI: `--validate`, `--validate-cmd` → `_save_validated` → `run_validation` (`runner.py:270`)
- Task/stage YAML: `validate` / `validate_cmd` → same path
- **SDK (new in T0)**: `pbatch.run_validation`, `pbatch.revalidate_existing`, `ValidationResult` — exported in `pbatch/__init__.py:12-13,26-27`
- T1 (planned): `--reuse` → `revalidate_existing` (not wired in T0 — `cli.py:_filter_reused` still existence-only)

**Attacker capabilities**: (a) local user with write access to any output directory; (b) untrusted agent (can write arbitrary files in the workdir during a task); (c) author of a task/pipeline YAML the user runs; (d) operator error (running untrusted files, embedding secrets in validator commands).

---

## 2. Findings

### F1 — **Medium** — `revalidate_existing` follows symlinks: read/write-through to attacker-chosen files (T0-introduced)

- **Evidence (Verified)**: `runner.py:286-310` — `path.is_file()` and `path.stat()` both follow symlinks; no `lstat`/`O_NOFOLLOW` check. Live PoC: `artifact.md → secret.txt` ⇒ `revalidate_existing(link, "cat {output}", …)` returned **True** (gates ran against `secret.txt`); `artifact4.md → victim.txt` with gate `echo pwned > {output}` ⇒ **victim.txt overwritten** (`'pwned\n'`). Dangling and empty-target symlinks do fail closed (Verified — good). Contrast: the write path `_save_validated` is symlink-safe (mkstemp + `rename` replaces destination symlink, `runner.py:252-281`).
- **Exploit preconditions/steps**: attacker with write access to the artifact's directory — a local user on a shared workspace, an untrusted agent (per `AGENTS.md` §5, agent output/files are untrusted and the agent runs with user credentials in the workdir), or a malicious pipeline `stage.commands` hook — runs `ln -s <victim> <expected-output-path>`. Any SDK caller today, or `--reuse` after T1, then feeds that path into `run_validation`.
- **Impact**: (a) read-through: validator stdout/stderr tails are logged (`runner.py:311-314`) → target-file content exfiltration into `--log-file`; (b) write-through: any gate that rewrites `{output}` (formatters are a common gate style, cf. registry `gofmt`) modifies an arbitrary user-writable file of the attacker's choosing (source files, `.git/config`, dotfiles). Undermines the documented fail-closed contract ("missing or empty artifacts fail closed" — `docs/RUNNING_247.md`).
- **Remediation (required before T1 wiring; recommended now since the function is already public)**: reject symlinks via `lstat` (`path.is_symlink()` / `os.path.islink`) or `path.resolve(strict=True)` + containment check, fail closed; mirror in `_filter_reused` and T1's reuse decision.
- **Regression test**: `revalidate_existing` on a symlink artifact → `False`; gate command never executes against the target (e.g., gate `cat {output}` and assert target content absent from captured output).

### F2 — **Medium** — `save_result` still uses a fixed `.tmp` name: symlink write-through (pre-existing; interacts with T0/T1)

- **Evidence (Verified)**: `runner.py:151-160` — `tmp = out_path.with_name(out_path.name + ".tmp"); tmp.write_text(...)`. The round-2 F14 fix (unique mkstemp name) was applied to `_save_validated` only. Live PoC: pre-created `o.md.tmp → victim.txt` ⇒ `save_result` **overwrote victim.txt with agent-controlled content**, and the final artifact `o.md` became the attacker's symlink (`out.is_symlink() == True`). Downstream `_combine_outputs`/`_gate_verdict` (`pipeline.py:57-64, 330-338`) and T1's `revalidate_existing` all read through that symlink — F2 manufactures the exact artifact F1 follows.
- **Exploit preconditions/steps**: no-validator task (the majority of analysis flows per docs) whose output directory the attacker can write (same attacker set as F1). Pre-create `<output>.tmp` as a symlink before the task completes.
- **Impact**: arbitrary file write of agent-controlled content with user privileges; the promoted artifact is a symlink, so later stages read an attacker-chosen file (content then enters prompts sent to the agent and appears in deliverables/decision log).
- **Remediation**: apply the same mkstemp treatment as `_save_validated` (keep the fd open and write through it — also closes the close-then-reopen race at `runner.py:255-257`).
- **Regression test**: with a symlink at the fixed `.tmp` name, `save_result` leaves the victim untouched and produces a regular-file artifact.

### F3 — **Low** — Public `run_validation`/`revalidate_existing` trust contract undocumented; `shell=True` with no input validation

- **Evidence (Verified)**: `runner.py:217-238` executes `cmd` via `subprocess.Popen(..., shell=True)` with no type/string checks, inheriting the full environment (agent API keys) and `cwd` of the caller's choice. Both functions are newly exported (`pbatch/__init__.py`). `_resolve_validators` treats unknown `--validate` names as raw shell (`config.py:82-89`) — documented in CLI help but easy to trip on typos.
- **Exploit preconditions**: an SDK consumer passes an untrusted string (or a list — silently mis-executed under `shell=True`); an operator typos a validator name. Any such caller gets arbitrary shell with the user's credentials.
- **Impact**: RCE-equivalent for anyone who can influence validator strings. This is inherent to the design (validators are shell by definition), so the fix is contract discipline, not removing `shell=True` (validators legitimately use `&&`, `test -z "$(...)"`).
- **Remediation**: docstring + `docs/RUNNING_247.md` trust-boundary paragraph ("validator commands are fully trusted shell executed with the user's environment; treat pi-batch.yaml, task/stage YAML, and --validate-cmd as trusted input; never run untrusted pipeline files"); reject non-`str` input with `TypeError`; keep `shlex.quote` on all `{output}`/`{cwd}` substitution (already done — Verified).
- **Regression test**: `run_validation(["echo", "x"], cwd)` raises `TypeError` (new guard).

### F4 — **Low** — Unbounded stdout/stderr capture and unbounded post-kill drain in the centralized executor

- **Evidence (Verified, code)**: `runner.py:228-231` — `proc.communicate(timeout=...)` buffers the full output in memory (a validator streaming gigabytes → runner OOM); after `TimeoutExpired`, `_kill_group(proc)` then `proc.communicate()` has **no timeout** — a child stuck in uninterruptible sleep (D-state, e.g. hung NFS) survives SIGKILL and blocks the runner indefinitely. Pre-existing pattern (old `_run_validator` did the same), but T0 centralizes it and T2's 4000-char/40-line cap covers only the retry-feedback copy, not the capture.
- **Impact**: denial of the 7×24 runner by one misbehaving validator; parallel workers multiply the memory exposure.
- **Remediation**: capture with a rolling tail (keep last N lines) or an output-size cap; give the drain communicate a bounded timeout; treat drain failure as `timed_out`.
- **Regression test**: validator emitting >cap bytes → `ValidationResult` bounded, runner survives.

### F5 — **Low** — Validator commands and output tails logged in full to `--log-file`

- **Evidence (Verified, code)**: `runner.py:272, 301-302` log the fully-substituted command (`VALIDATE:`/`REVALIDATE:` lines); failures log the last 10 lines of stdout+stderr (`runner.py:275-278, 303-306`). A `--validate-cmd` embedding credentials (e.g. `curl -H "Authorization: Bearer …"`) or a gate printing secret-bearing content lands verbatim in the log file at rest. Pre-existing pattern (the old `VALIDATION FAILED` logging was equivalent), replicated by the new revalidate path.
- **Impact**: secrets-at-rest in `logs/`; the tool already truncates prompts for this reason (`_prompt_brief`, `runner.py:58-61`) but not validator output.
- **Remediation**: documented redaction policy, or truncate/redact command tails; keep the artifact-content tails (they are needed for triage) but document that `--log-file` is a sensitive file.
- **Regression test**: gate emitting a sentinel secret → sentinel absent from captured log record (or documented exception).

### F6 — **Info** — `revalidate_existing` is fail-open on an empty effective gate; contract requires caller-supplied effective gate

- **Evidence (Verified, code)**: `runner.py:294-296` — `_resolve_validators(None/"")` ⇒ no commands ⇒ `return True`. Consistent with the documented `"" = disabled` semantic (`config.py`), but the function's fail-closed promise ("missing or empty artifacts fail closed") does not extend to a missing gate: T1's wiring must replicate `_save_validated`'s precedence exactly (`task.validate` if not None, else stage `validate_cmd`, else CLI, else none — `runner.py:263`, `pipeline.py:296-298`). A wiring bug silently promotes unvalidated artifacts under `--reuse`.
- **Remediation**: docstring contract note ("caller must pass the fully-resolved effective gate"); T1 fixture asserting generation-path and revalidation-path gate parity per reuse path; optionally an explicit `no_gate_ok: bool` parameter to make the choice visible at call sites.
- **Regression test**: (T1) task with `validate: "exit 1"` under `--reuse` ⇒ regenerated, never promoted.

### F7 — **Info** — Interrupt/exception between mkstemp and unlink leaks a 0600 temp file (pre-existing)

- **Evidence (Verified, code)**: `_save_validated` (`runner.py:252-281`) has no `finally`; `KeyboardInterrupt` during a validator run skips `tmp.unlink`. Residue is 0600 with random name (low exposure), and the planned T5 residue markers target exactly this class.
- **Remediation**: wrap the validate loop in `try/finally` with `tmp.unlink(missing_ok=True)`.

---

## 3. Abuse-case table

| # | Category | Abuse case | Reachable path | Outcome (current state) |
|---|---|---|---|---|
| A1 | Identity spoofing | Task/pipeline YAML sets `validate:` (or `--validate` typo) to an attacker command, masquerading as a named registry gate | `config._resolve_validators` → `run_validation` | Arbitrary shell as invoking user. **By design** (validators are shell); trust boundary F3. YAML authors are trusted-equivalent. |
| A2 | Replay | Stale artifact promoted under `--reuse` without revalidation | `cli.py:_filter_reused` (existence-only today) → T1 `revalidate_existing` | T0: no revalidation yet (stated non-goal). T1: gates decide; missing/empty artifacts fail closed (**Verified**). Gate-parity risk = F6. |
| A3 | Replay | Session continuation: predictable `--session-id` (`batch`, source stem — `cli.py:_derive_session_name`) lets another local user resume/poison a shared session | `--session-mode shared` + agent `--session-id` | Pre-existing, out of T0 scope. Local-user only; no auth in tool. |
| A4 | Cross-tenant access | Shared machine, workspace-writable output dirs: symlink artifact → read/write of another user's files | F1 (`revalidate_existing`) + F2 (`save_result` `.tmp`) | **Verified exploitable** (both PoCs ran). F1 is T0-introduced; F2 pre-existing. |
| A5 | Proxy/header forgery | Tool has no HTTP surface; but a malicious validator (or `stage.commands`) can rewrite `~/.curlrc`, proxy env, credentials | `run_validation` env inheritance; `pipeline._run_single_cmd` | Arbitrary child process with user env — inherent to shell-validator design; covered by F3 trust-boundary documentation. |
| A6 | Resource exhaustion | Validator fork bomb / unbounded output / D-state child | `run_validation` timeout path | Process-group SIGKILL at 600s hard deadline (**Verified** — `test_run_validation_timeout_kills_group`; kill is SIGKILL so fork-bomb descendants die with the group). Unbounded memory capture + unbounded post-kill drain = F4. |
| A7 | Sensitive-data leakage | Gate output / symlink target content into `--log-file`; secrets embedded in `--validate-cmd` | `runner.py:272-278, 301-306` | F5 (log tails) + F1 (read-through). Prompt content is truncated (`_prompt_brief`) but validator output is not. |

---

## 4. Positive controls verified, residual risks, prioritized validation plan

**Positive controls (all Verified)**
- `{output}`/`{cwd}` substitution is `shlex.quote`-escaped in both `_save_validated` and `revalidate_existing` (`runner.py:266, 300`). Live attempt with a metacharacter artifact name (`x;touch pwned.txt`) — no injection.
- Validator runs in its own process group (`start_new_session=True`); timeout → `os.killpg(SIGKILL)`; spawn crash fails closed with exception text in `stderr` (`test_run_validation_spawn_crash_fails_closed`).
- Zero-artifact guarantee preserved on gate failure: mkstemp temp deleted, no residue (`test_validate_failure_keeps_zero_artifact_with_refactored_executor`; my `o.md*` glob check).
- `revalidate_existing` fails closed on missing/empty/dangling artifacts (tests + PoC).
- AND-semantics, failure-signature rejection, retry/session logic, exit codes, agent-invocation contract: unchanged (diff review); existing validator tests (571–646) green; full suite 118 passed; `make ci` exit 0 (single clean run in this session; the handoff's one mid-session transient exit-2 anomaly remains **unexplained/Partial** — not reproduced here or in their two subsequent runs); `quality.py --strict .` OK; `runner.py` 474 lines < 500-line pre-commit budget (**Verified** — `checks/config.py` + `engineering.yaml` `max_lines: 500`); `ai/run-review.py` untouched (git diff shows exactly 4 modified files).
- Oracle-safe/fail-closed invariants from `AGENTS.md` §5 preserved: gate missing-VERDICT ⇒ FAIL, rejection ⇒ no artifact, role-template confinement, role-name sanitization (all untouched code paths).

**Residual risks**
- F1/F2 unfixed until T1 — **T1 must not land before F1's symlink rejection**; the plan's T1 scope (`cli.py _filter_reused`, four builders, `execute_stage` short-circuit) touches exactly the paths that consume symlink artifacts.
- F6 gate-parity wiring risk in T1 (fail-open if effective-gate resolution diverges between generation and revalidation).
- Validator deadline hardcoded 600s (plan non-goal); validator env inheritance of agent credentials (documented, F3); interrupt-time temp residue (F7); session-id predictability (A3, pre-existing); the unexplained transient `make ci` anomaly.
- `docs/agent-os/EVALUATION.md` absent from this tree — evidence standard substituted from `prompts/README.md` + `AGENTS.md` (as the handoff did); no impact on findings, all of which are code/execution-verified.

**Prioritized validation plan**
1. **Before T1** (required): F1 symlink rejection in `revalidate_existing` + regression test; F2 mkstemp normalization of `save_result` + regression test; F7 `try/finally` cleanup.
2. **T1 entry**: effective-gate parity fixtures across all six reuse paths (F6); symlink-artifact handling in `_filter_reused`; revalidation fail-closed fixtures per plan.
3. **T2**: bound validator output capture (F4) and cap log tails/redaction (F5).
4. **Docs**: trust-boundary paragraph for the new public API (F3); `--log-file` sensitivity note (F5).
5. **Gate re-run after each**: `make ci`, `pytest tests/`, `python quality.py --strict .`, plus the F1/F2/F6 regression set.

**Bottom line**: T0 is a faithful, byte-compatible refactor — the executor centralization, quoting discipline, and fail-closed/zero-artifact invariants all check out. The one T0-introduced defect is F1 (symlink-following in `revalidate_existing`, write-through empirically confirmed); fix it before T1 wires `--reuse`, and normalize F2's fixed-`.tmp` twin in the same pass. No release-blocking issue exists in the current (unwired) state.
