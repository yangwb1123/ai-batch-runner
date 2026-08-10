All verification is complete. Here is the QA lead review report for this revision.

---

# QA Lead Review — Remove PY-GODFILE-SPLIT & fail closed on self-referential rule files (round 2)

## 0. Revision state (read this first)

- **HEAD**: `f8f6e9f` (prior-run design commit). The direction's delta is **uncommitted in the working tree**; `pbatch/rule_cli.py` remains **untracked** (`git ls-files` empty — parallel cli.py-split workstream never committed it).
- **R1 subtlety, re-verified**: `git log --all -S PY-GODFILE-SPLIT -- backend-specs/rules.yaml` is **empty** — the rule was never committed to HEAD; it existed only as an uncommitted working-tree block (promoted from the archive at Aug 8 00:50). The "removal" therefore makes `backend-specs/rules.yaml` byte-identical to HEAD (`git diff` empty, grep count 0). Rollback for R1 = manual re-add from `docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml`, not `git checkout`.
- **Rollback gap measured**: `git checkout -- pbatch/rule_cli.py` → `error: pathspec 'pbatch/rule_cli.py' did not match any file(s) known to git`, exit 1. The design's "4-file `git checkout`" rollback cannot restore `rule_cli.py`; the implementer's manual-revert-of-3-blocks deviation is **required**, not optional.
- Concurrent state: our own campaign (pid 1684832, `logs/campaign-run.log`) holds the repo lock — 19 lock-gated test failures reproduce (see §5).
- **Delta reviewed** (5 files): `backend-specs/rules.yaml` (R1, net no-op vs HEAD), `pbatch/rule_cli.py` (R2), `evals/rules.yaml` (+1 `assert:` case), `tests/test_rule_matcher.py` (+2 tests), `docs/SPEC_SYSTEM.md` (R1.2 note). `ui-specs/rules.yaml`/`AGENTS.md`/`Makefile`/`pbatch/*` dirty deltas are parallel-campaign edits (AADM governance, cli.py split), confirmed unrelated to this direction.

## 1. Test inventory — commands actually run for this revision (all executed this session, repo root unless noted)

| # | Command | Result |
|---|---|---|
| T1 | `git diff backend-specs/rules.yaml`; `grep -c PY-GODFILE-SPLIT backend-specs/rules.yaml` | empty diff; 0 (R1) |
| T2 | `git log --all -S PY-GODFILE-SPLIT -- backend-specs/rules.yaml` | empty — never committed |
| T3 | `python pi-batch.py rules --check; echo $?` | exit 0, both registries OK (A3 clean) |
| T4 | `python pi-batch.py eval --json` | **total 41, failed 0** |
| T5 | `python pi-batch.py eval --filter backend-manifest-excludes-repo-internal-policy --json` | **total 1, failed 0** — case active, not inert |
| T6 | `python pi-batch.py eval --filter backend-manifest-excludes-typo --json; echo $?` | total 0, failed 0, **exit 0** — vacuous on typo (F-3/F-B risk confirmed live) |
| T7 | `python pi-batch.py eval --quick --json` | total 35, failed 0 |
| T8 | A1: `rules "写一个订单创建接口，含支付审批，生产，go" --json \| grep -c PY-GODFILE-SPLIT` | 0, grep exit 1 (clean) |
| T9 | A2: `rules "开发一个客户管理模块的列表查询与新增接口" --json \| grep -c PY-GODFILE-SPLIT` | 0, grep exit 1 (clean) |
| T10 | `python -m pytest tests/test_rule_matcher.py -q` | **26 passed** |
| T11 | `python -m pytest tests/test_rule_matcher.py -k self_referential -q` | **1 passed, 25 deselected** (F-4: implement report claims "2 passed" — measured wrong) |
| T12 | `python -m pytest tests/test_agent_engineering.py -k "rules or check" -q` | 3 passed |
| T13 | `python -m pytest tests/test_entrypoints.py -k eval -q` | 1 passed (CI canary `total>=20`) |
| T14 | `python -m pytest tests/test_backend_specs.py -q` | 41 passed |
| T15 | `python -m pytest tests/ -q` | **934 passed, 19 failed, 1 skipped** — 11 in `test_pi_batch.py` + 8 in `test_local_fixes.py`, all lock-refusal (exit 5, "Another pi-batch instance is running (pid=1684832…)"), stderr-inspected; none touch rules/registry/eval |
| T16 | `python -m pytest checks/ -q` | 37 passed |
| T17 | `python quality.py .` | OK |
| T18 | Behavior matrix (direct API): template self-ref / empty profiles / 3 path forms / default-`None` / cross-domain | template flagged ✅; empty-profiles no violation ✅; relative/`./`/absolute all flagged ✅; default-`None` → `[]` ✅; `ui-specs/rules.yaml` listed by backend rule → no violation ✅ |
| T19 | CLI dirty side (monkeypatched poisoned registry → `rules_main(["--check"])`) | **SystemExit code=1** with exact message `files entry 'backend-specs/rules.yaml' resolves to the registry file itself (self-referential rule)` — wiring works, **but no automated test pins it** |
| T20 | `rules --check --registry /nonexistent.yaml` | exit 0 — check path still ignores `--registry` (E7 unchanged) |
| T21 | Re-pollution lock: polluted manifest injected into `run_suite` | case **fails** `lacks_rule present ['PY-GODFILE-SPLIT']`; same case in inert `expect:` form → vacuous PASS |
| T22 | `grep -rn PY-GODFILE-SPLIT` (excluding runs dirs) | only `docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml` + `evals/rules.yaml:28` — zero other dependents |
| T23 | From `/tmp` CWD: `pytest test_rule_matcher.py -k self_ref` | **both new tests FAIL** (F-11 confirmed; CWD-sensitive existence checks) |
| T24 | `git checkout -- pbatch/rule_cli.py` | pathspec error, exit 1 (untracked; rollback gap) |
| T25 | `selfcheck.py` code-read | registry gate = `python -m pbatch rules --check` (selfcheck.py:37) → inherits R2 fail-closed with zero wiring |

## 2. Requirement-to-test matrix

| Req | Status | Evidence |
|---|---|---|
| R1 removal from shipped registry | ✅ | T1, T8, T9; file byte-identical to HEAD |
| R1.1 archive untouched | ✅ | T22 (promoted archive present, Aug 8 00:50) |
| R1.2 doc guardrail | ✅ | `docs/SPEC_SYSTEM.md` diff carries the note |
| R1.3 no dependents, gates green | ✅ | T22 (only eval case), T3 exit 0, T4 41/0, T13/T14/T17 |
| R2.1 optional `registry_paths`, default `None` | ✅ | T18 (backward compat) + `test_agent_engineering.py:158` 1-arg caller passes (T12) |
| R2.2 files + files_template comparison | ⚠️ Partial | `files` covered by unit test (T10); **template branch zero automated coverage** (F-1, still open) |
| R2.3 `_check_registries_cli` passes deduped resolved paths | ✅ | code-read (rule_cli.py:117-121) + T18 dedupe behavior |
| R2.4 exit-code plumbing | ⚠️ Partial | T19 SystemExit(1) verified manually; **no automated test** (F-2, still open) |
| R2.5 resolve-normalized | ✅ | T18: all three forms caught |
| R2.6 cross-domain out of scope | ✅ | T18: no violation as designed |
| R3 eval case (assert: form, non-vacuous) | ✅ | T5 (runs), T4 (41/0), T21 (fails on re-pollution; inert-form risk real) |
| R3.2 optional second case (A2) | ✅ Accepted | T9 same code path; A2 still absent from eval/gate sequence (F-C residual) |
| A1 / A2 | ✅ Pass | T8, T9 |
| A3 clean + poisoned | ✅ Pass (manual) | T3, T19, T10 — **with F-1/F-2 caveats: nothing automated would fail on wiring regression** |
| A4 eval + CI | ✅ Pass (env caveat) | T4, T13 canary; full `make ci` test target blocked by env lock (T15) |

## 3. Findings (severity, untested failure, regression risk, exact test, acceptance)

- **F-1 (Medium) — STILL OPEN: `files_template` self-reference branch has zero automated coverage.** `_check_self_reference`'s template loop (rule_cli.py:61-66) is exercised by no test; both new unit tests use `files:` only. Manual T18 shows it works. **Regression risk**: a future edit to the template comparison silently breaks and no CI signal fires. **Test**: `test_registry_rejects_self_referential_template` — rule with `files_template: "backend-specs/rules.yaml"` + `profiles: ["p"]` → `check_registry(reg, registry_paths=[base])` returns a violation containing "self-referential"; plus `profiles: []` → `[]` (F4 edge). **Acceptance**: `pytest tests/test_rule_matcher.py -k template -q` → 1 passed.
- **F-2 (Medium) — STILL OPEN: CLI exit-1 path is untested.** A3's "exits non-zero" rests on manual verification (T19) + function-level lists. If `_check_registries_cli` drops `registry_paths` (the one line that IS the feature), `rules --check` silently passes on self-referential registries and **every automated gate stays green** (unit tests pass paths explicitly; clean-side canary unaffected; eval case unaffected). **Regression risk**: this is the primary acceptance, guarded by zero failing tests — the exact F-A defect the design review flagged. **Test**: monkeypatch `pbatch.rule_cli.load_registry` + `_default_registry_paths` (Path objects!) to a self-referential in-memory registry; `rules_main(["--check"])` → `pytest.raises(SystemExit)` with `code == 1`. **Acceptance**: `pytest tests/test_agent_engineering.py -k "check_cli" -q` → 1 passed.
- **F-3 (Medium) — STILL OPEN: no CI backstop pins the eval case id / form.** T6 measured `eval --filter <typo>` → total 0/failed 0/**exit 0**; a case silently converted to inert `expect:` keeps eval at 41/0 and CI green (T21 vacuous-PASS). The canary (T13) asserts only `total>=20, failed==0`. §6's proof is a manual command with the correct id (now non-vacuous: T5 total=1) — but nothing fails if the id is typo'd or the form regresses. **Test**: pytest running `eval --filter backend-manifest-excludes-repo-internal-policy --json` asserting `total == 1 and failed == 0`. **Acceptance**: passes today; converting to `expect:` or renaming the id makes it fail.
- **F-4 (Low) — CONFIRMED, now with new evidence: the implement report's own verification command is wrong.** Report line 74 claims `-k self_referential → 2 passed`; measured (T11): **1 passed, 25 deselected** — `test_shipped_registries_have_no_self_reference` ("self_reference") is not matched. The clean-side test therefore was never executed by the claimed command. **Fix**: use `-k "self_referen"` or run the whole file (T10 covers both).
- **F-11 (Low) — CONFIRMED: both new tests are CWD-dependent.** From `/tmp` (T23): `test_shipped_registries…` fails on pre-existing `missing file ui-specs/spacing.md` (relative existence checks resolve against CWD) and `test_registry_rejects…` fails on `assert not check_registry(reg)` for the same reason. Standard `make ci` runs from repo root (green), but the design's "deterministic" claim overstates. **Fix**: `monkeypatch.chdir(Path(__file__).resolve().parents[1])` in the clean-side test, or assert only the self-reference violation. **Acceptance**: both pass from any CWD.
- **F-5 (Low/Info) — open**: `check_registry(reg, [])` silently disables the gate (empty list falsy). No current caller passes `[]`; worth one docstring line (`if registry_paths is not None`) — zero behavior change.
- **F-6 (Info) — re-pollution vector live by design**: `learn promote --files backend-specs/rules.yaml` still writes verbatim and only logs the check (learn.py:333). Lock verified working end-to-end (T21: eval case red + `test_rules_check_passes` would go red → CI catches). Optional hardening out of scope.
- **F-8 (Info) — `make ci`/`pytest tests/` cannot pass right now**: 19/954 failures are lock-refusals from our own live campaign (T15; verified signature, 11+8 across `test_pi_batch.py`/`test_local_fixes.py`). Environmental; re-run after campaign completion.
- **N-1 (Info, new) — rollback plan is broken as written**: `git checkout -- pbatch/rule_cli.py` fails (untracked, T24); R1 has no git source (never committed, T2). Rollback = manual 3-block revert in `rule_cli.py` + re-add rule block from archive + `git checkout` the other 3 tracked files. Documented in the implement report as a deviation — but the design's §6.7 still says "4-file git checkout" and is therefore wrong.
- **N-2 (Info) — T19 harness note**: mocking `_default_registry_paths` must return `Path` objects (not strings) or the real `p.resolve()` call in `_check_registries_cli` raises `AttributeError` — irrelevant to production code, relevant to writing the F-2 test correctly.

## 4. Prioritized scenario list

- **Happy**: A1/A2 manifests clean (T8/T9); `rules --check` exit 0 (T3); eval 41/0 (T4); both new unit tests green at root (T10); archive untouched (T22); `pi-batch check` inherits R2 via selfcheck.py:37 (T25); clean-side shipped-registry scan (T10).
- **Boundary**: three path forms (T18); `files_template` + profiles (T18, untested in suite — F-1); empty profiles (no violation, T18); dual-candidate dedupe (T18); CWD variation (T23 — FAIL, F-11); legit spec file no false positive (T18); cross-domain registry reference out of scope (T18, accepted).
- **Error**: poisoned registry → CLI exit 1 (T19 — manual only, F-2); missing files still flagged (pre-existing, T23 exposes CWD side); broken-registry test still passes (T12); `--registry` override still ignored on check path (T20, unchanged); typo'd eval filter id → vacuous exit 0 (T6, F-3).
- **Race**: `check_registry`/`_check_self_reference` are pure/stateless — no internal races. External hazards: repo lock (T15, F-8) and concurrent campaign writes to the same tree (observed flip in round 1; the final gate must re-verify against the committed revision).
- **Recovery**: re-pollution via `learn promote` → eval case red (T21) + `test_rules_check_passes` red → CI catches; ordering C4 holds (R1 landed with R2 — verify in the final commit's `git log`); rollback = manual 3-block revert + archive re-add + 3-file `git checkout` (N-1, no state).

## 5. CI/manual-suite gaps, flake risks, fixtures, exit criteria

- **Gaps (blocking for merge-as-tested)**: F-1 template test, F-2 CLI exit-1 test, F-3 eval-filter pin — the exact three additions the round-1 review specified, still absent from the landed delta. F-4: correct the implement report's verification command (`-k "self_referen"`, measured 1 vs claimed 2). F-11 chdir guard.
- **Flake risks**: (a) CWD sensitivity of both new tests (T23 — fails outside repo root); (b) repo-lock failures while any campaign runs (currently 19, environmental); (c) `test_shipped_registries…` reads live repo files — a mid-run registry edit produces transient red (observed in round 1).
- **Fixtures needed**: none — F-1/F-2/F-3 tests are in-memory or read shipped registries; no tmp files, no network, no new fixtures.
- **Exit criteria** (each measured this session): `rules --check` exit 0 ✅ (T3); `eval --json` 41/0 ✅ (T4); `eval --filter backend-manifest-excludes-repo-internal-policy` total 1/0 ✅ (T5); `test_rule_matcher.py` 26 ✅ (T10) + agent_engineering -k rules 3 ✅ (T12) + backend_specs 41 ✅ (T14) + eval canary ✅ (T13); `quality.py .` OK ✅ (T17); **full `make ci` green — blocked by env lock (T15), must be re-run after the campaign completes and the delta is committed**; A1/A2 grep-clean at the committed revision ✅ (T8/T9).

## Bottom line

**Functional verdict: A1–A4 all pass by measured commands; the landed implementation matches the design and the gate works end-to-end (verified: CLI SystemExit(1) on poisoned registry T19, eval case fails on re-pollution T21, clean side green T3/T10).**

**Test-completeness verdict: FAIL — the three regression-pinning tests explicitly required by the design review (F-1 template branch, F-2 CLI dirty-side wiring, F-3 eval-filter case-id pin) were not added; F-2 in particular means the primary acceptance A3 can silently regress with every automated gate staying green. Additionally, the implement report's A3 verification claim is measured to be wrong (`-k self_referential` → 1 passed, not 2), and the design's 4-file git-checkout rollback cannot restore the untracked `rule_cli.py` (N-1).**

The change is safe to run and every measured behavior is correct — but as a *tested, merge-ready deliverable* it is not complete: ~30 lines of tests (F-1/F-2/F-3) plus one chdir guard (F-11) close the only real coverage gaps. No oracle-safety, credential, tenant, or replay concerns apply to this surface (pure schema validation; no I/O beyond registry reads).
