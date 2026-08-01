All evidence gathered. Here is the QA-lead review.

---

# QA Lead Review — T0 (R0 validator-execution refactor)

**Revision**: `4b27031` + uncommitted T0 diff (4 files: `pbatch/runner.py`, `pbatch/__init__.py`, `tests/test_pi_batch.py`, `docs/RUNNING_247.md`). **Role**: per `prompts/README.md` — risk-based test review; findings Verified by execution, not by trusting the handoff's PoCs. I independently re-ran the security PoCs (F1/F2 **confirmed**, below).

## 1. Test inventory and commands actually run

| Command | Result (this session) | Evidence |
|---|---|---|
| `python -m pytest tests/` | **118 passed**, 9.43s | full suite, incl. 8 new T0 tests |
| `make ci` (check + self-test + test + quality) | **exit 0** | gates + 118 pytest + `quality: OK` |
| `python -m pytest --cov=pbatch --cov-report=term-missing tests/` | runner.py **75%** (211/280); `__init__.py` 100%; config.py 89% | measured, current |
| Live PoC: symlink artifact → `revalidate_existing(link, "cat {output}")` | **True** (gate ran on victim file) | F1 confirmed |
| Live PoC: symlink artifact → `revalidate_existing(link, "echo pwned > {output}")` | victim overwritten `'pwned\n'`, gate **True** | F1 confirmed |
| Live PoC: pre-created `o.md.tmp → victim.txt` → `save_result` | victim overwritten with `'agent-content'`, artifact becomes symlink | F2 confirmed |
| Live PoC: `run_validation(["echo","hello"], …)` / `""` / `timeout=0` | `ok=True, stdout='\n'` (silent mis-execution) / `ok=True` / immediate timed-out fail-closed | contract gaps |
| Diff review | existing validator tests (571–774) **byte-untouched**; only appends at 774+ | `git diff tests/` |
| `_filter_reused` (`cli.py:368-375`) | existence-only `output_path().exists()`; zero `revalidate_existing` callers in production code | T1 unwired — Verified |
| `make coverage` target | measures `checks/` + `cli.py` only — **pbatch/ not covered by any configured target** | Makefile:41-43 |

## 2. Requirement-to-test matrix (requirements from plan §T0 + `docs/RUNNING_247.md` additions)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| R1 | `run_validation()` returns structured `(ok, exit_code, stdout, stderr, timed_out)` | **Covered** | 4 unit tests: success, exit+stderr tail, spawn-crash fail-closed, timeout kills group (tests 777–812) |
| R2 | `_save_validated` uses executor; AND semantics; mkstemp→rename; zero-artifact on failure | **Covered** | existing 571–641 unchanged + new 850–860 (no `o.md*` residue) |
| R3 | Spawn crash fails closed with exception text in stderr | **Covered (unit only)** | `test_run_validation_spawn_crash_fails_closed`; not exercised through `_save_validated` |
| R4 | Timeout → process-group kill, `timed_out=True` | **Covered (unit only)** | `test_run_validation_timeout_kills_group`; the `(timeout)` label through `_save_validated`/`revalidate_existing` is **untested** |
| R5 | `revalidate_existing`: missing/empty fail closed; no-gate trivial pass; `{output}` placeholder; `{cwd}` | **Partial** | 3 tests (818–847) cover happy/missing/empty/placeholder. **Untested**: OSError race branch (296–297), failure tail logging (313/315), `workdir=""` fallback semantics, symlink artifacts (F1), multi-gate AND in revalidate path |
| R6 | Refactored save path leaves no temp residue | **Covered** | 850–860 (glob check) |
| R7 | Existing validator tests unchanged | **Covered** | diff appends only |
| R8 | SDK exports additive + importable | **Covered** | `pbatch/__init__.py` 100% cov; `load_batch()` exercises it |
| R9 | Reuse freshness revalidation (plan T1) | **Not in scope — untested E2E** | `_filter_reused` existence-only; both `--reuse` E2E tests (432–435, 1270–1272) assert only skip-on-exists |

## 3. Findings

**QA-1 — Medium — Symlink read/write-through in `revalidate_existing` (T0-introduced, empirically confirmed)** — untested failure: artifact path is a symlink to an attacker-chosen file.
- Regression risk: T1 wires `--reuse` onto this function; the security review's "before T1" mandate is correct.
- Exact test: `out = tmp_path/"o.md"; victim = tmp_path/"secret.txt"; victim.write_text("s"); out.symlink_to(victim); assert mod.revalidate_existing(out, "cat {output} > {cwd}/leak.txt", str(tmp_path)) is False`; plus write-through variant with gate `echo pwned > {output}`.
- Acceptance assertion: `revalidate_existing` returns **False** for any symlink artifact, the gate command never executes against the target, and `victim` content is unchanged.

**QA-2 — Medium — `save_result` fixed-`.tmp` symlink write-through (pre-existing, interacts with T0/T1; confirmed)** — untested failure: attacker pre-creates `<output>.tmp` as symlink.
- Regression risk: majority of flows are no-validator tasks; the promoted artifact becomes a symlink that T1's `revalidate_existing` would follow (F1 manufacturer).
- Exact test: pre-create `o.md.tmp → victim.txt`, run `save_result` with success result; assert victim unchanged and `o.md` is a regular file.
- Acceptance assertion: `save_result` writes via unique mkstemp fd (or fails closed), victim untouched, `o.md.is_symlink() is False`.

**QA-3 — Low — Coverage gaps inside the new code (measured)**: lines 277 (stdout-tail loop in `_save_validated` failure), 296–297 (OSError race), 313/315 (revalidate failure tails), 256–257 (validators + no-file-target), 248 (defensive `not result.success` guard, unreachable in current flow).
- Untested failures: (a) failing gate emitting **stdout** through the save path (only stderr-emitting gates tested); (b) file vanishing between `is_file` and `stat` (fail-closed race, unreachable without monkeypatch); (c) failing gate emitting output through revalidate (only silent `exit 1` tested).
- Exact tests: (a) `validate_cmd='echo out >&1; exit 3'` → assert not output.exists() and log tail contains "out"; (b) `monkeypatch.setattr(Path, "stat", ...)` raising OSError once → `revalidate_existing(...) is False`; (c) revalidate with `"echo boom >&2; exit 1"` → False.
- Acceptance assertions: fail-closed outcome in all three; the OSError branch returns False without raising.

**QA-4 — Low — Validator + no-file-output task silently bypasses gates (untested branch 256–257)** — `_save_validated` prints to stdout and returns True when `validate_cmd` is set but the task has no `output`. Behavior is per docstring ("nothing to validate against") but unpinned.
- Exact test: `run_serial([Task(prompt="x")], validate_cmd="exit 1")` → task succeeds, stdout has agent text.
- Acceptance assertion: no exception, no artifact, success=True (document the deliberate bypass).

**QA-5 — Low — `run_validation` accepts non-`str`/empty commands (verified mis-execution)** — `["echo","hello"]` runs `echo` with `hello` as a shell arg (`ok=True, stdout='\n'`); `""` returns `ok=True`. No type guard.
- Exact test: `run_validation(["echo","x"], cwd)` raises `TypeError` (after adding the guard per security F3); `run_validation("", cwd)` documented as trivially-pass (or rejected).
- Acceptance assertion: TypeError on non-str; empty string behavior pinned explicitly.

**QA-6 — Info — Revalidate AND-semantics across multiple gates not distinctly tested** (shared executor makes this low risk; `test_validate_multiple_and_semantics` covers the save path). Add `revalidate_existing(out, "true,exit 1") is False` for symmetry.

**QA-7 — Info — Timeout path discards partial validator output** (`proc.communicate()` result dropped, lines 232–234) — directly relevant to T2's retry feedback: timeout failures will carry no signature text. Add test: validator `echo partial; sleep 30` with `timeout=1` → result has `timed_out=True` and `stdout` contains "partial" (after the F4 fix).

## 4. Prioritized scenario list

**Happy**: pass gate → artifact saved (571–580 ✓); revalidate pass + trivial no-gate (818–826 ✓); multi-gate AND pass (762–772 ✓).
**Boundary**: empty artifact fail-closed (842–847 ✓); missing artifact fail-closed (841 ✓); `{output}` with spaces/metacharacters (F3/F14 quoting — unit-verified in review PoC, no regression test pins it: **add** `artifact name with space.md` through `_save_validated`); workdir default vs explicit (code-review F1, **add** in T1).
**Error**: exit≠0 (581–590 ✓); spawn crash (790–795 ✓); timeout kill (803–809 ✓, plus save-path `(timeout)` label **missing**); stdout-emitting failure (QA-3a **missing**); non-str cmd (QA-5 **missing**).
**Race**: file vanishes between is_file/stat (QA-3b **missing**); `.tmp` symlink pre-creation (QA-2 **missing**); interrupted mkstemp→rename residue (security F7, **missing** — no KeyboardInterrupt test through `_save_validated`).
**Recovery**: retry regenerates failed validation (602–614 ✓); `--reuse` skip-on-exists E2E (432–435, 1270–1272 ✓); **reuse revalidation E2E is T1 work — must land with the F1 fix, not after it**.

## 5. CI/manual-suite gaps, flake risks, fixtures, exit criteria

**CI gaps**: (1) `make coverage` measures `checks/`+`cli.py` — **`pbatch/` has no coverage gate or target**; the 75% runner figure exists only because I invoked `--cov=pbatch` manually. Add `--cov=pbatch --cov-fail-under=80` (runner.py) to the target. (2) No CI job runs `make ci` in this repo state (only the Makefile exists); the transient exit-2 anomaly stays **Partial** — three consecutive clean runs (mine + reviewers' two) since, consistent with pytest-cache contention; recommend `pytest -p no:cacheprovider` in CI. (3) Pre-commit hook execution unobserved; component gates verified directly.

**Flake risks**: low. New tests use no wall-clock assertions (`sleep 30`/`timeout=1` is outcome-deterministic; elapsed-bounds tests pre-existing with generous margins). One hang risk, not flake: post-kill `proc.communicate()` has no timeout (security F4) — a D-state validator child would hang the suite; bounded-drain fix belongs in T2.

**Fixtures needed**: `symlink_artifact` (QA-1/QA-2), `monkeypatched_stat` race (QA-3b), `noisy_failing_gate` (stdout+stderr emitters, QA-3a/c), T1's six-path reuse fixtures (single-batch, from_prompt, from_dir, from_outputs, aggregate, full-reuse stage) each with `--validate "exit 1"` ⇒ regenerate / `true` ⇒ "reused+validated".

**Exit criteria for this revision**: (1) QA-1 symlink rejection lands before any T1 commit; QA-2 in the same pass (shared `.tmp` hygiene); (2) QA-3/4/5/7 tests added to close the measured gaps — target: `pbatch/runner.py` ≥90% with zero uncovered lines inside `run_validation`/`_save_validated`/`revalidate_existing` except the documented defensive guard (248); (3) `make ci` + `pytest` green; (4) acceptance checks from plan §T0 all demonstrated in CI output, not just locally.

**Verdict**: approve T0's test state for merge **with the QA-1/QA-2 fixes** (both empirically confirmed exploitable today; QA-2 pre-existing but the refactor is the natural place to normalize it). The 8 new tests pin the fail-closed contract well; the remaining gaps are branch-level coverage and the two symlink paths — all cheap to close before T1.
