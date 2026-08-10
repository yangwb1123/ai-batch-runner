All claims verified. I have everything needed for the QA review. Summary of my independent verification: every one of the design's 15 evidence claims (E1–E15) holds against HEAD; the code sketches behave exactly as claimed (all three path forms caught, dedupe works, fires on the live polluted registry, `None` default is backward compatible); baseline is 40/0 eval; both acceptance inputs leak `PY-GODFILE-SPLIT` today. But I found three real gaps in the **proposed test plan** itself.

---

# Testing Review — Remove PY-GODFILE-SPLIT & fail closed on self-referential rule files

## Scope note

This review is at **design stage** (implementation not yet run). I reviewed the design's test plan, its acceptance mapping, and its verification evidence against HEAD. All design evidence claims were independently re-executed, not taken on faith: `rules --check` exit 0 with pollution live (E6 ✓), eval baseline 40/0 (E9 ✓), leak in both A1/A2 inputs (✓), `eval --filter` flag exists (✓), `test_rules_check_passes` clean-side canary (E13 ✓), `test_agent_engineering.py:158` one-arg `check_registry` caller (E11 ✓), `learn promote` verbatim `--files` write + log-only post-check (E15 ✓), and the §3.1/§3.2 code sketches re-executed in isolation — relative/`./`/absolute forms all caught, dual candidates deduped to one path, `files_template` branch fires, default-`None` behavior preserved.

## 1. Verdict

**VERDICT: FAIL - blocking test gap: no test exercises the CLI dirty side — `_check_registries_cli`'s wiring (passing `registry_paths`) is the entire A3 acceptance, and every gate in the design's §6 sequence passes even if that wiring is omitted; plus `eval --filter` on a typo'd/absent case id returns total 0/failed 0/exit 0, so the F7 proof command proves nothing.**

The design is otherwise sound — but A3's dirty side must be pinned by an integration test before implementation is approved.

## 2. Findings table

| # | Sev | Defect pattern | Missing test | Exact test case that would catch it |
|---|---|---|---|---|
| F-A | **HIGH** | **A3 claimed "satisfied by construction"; wiring not regression-pinned.** If `_check_registries_cli` forgets to pass `registry_paths` (the one new line that IS the feature), `check_registry(registry)` runs with `None` → self-reference invisible → `rules --check` exits 0. Both proposed unit tests still pass (they call `check_registry` with paths passed explicitly), `test_rules_check_passes` still passes (clean side), eval passes (rule removed). A3 silently fails and **no gate in §6 goes red** — the acceptance criterion has zero failing-test coverage. | CLI-level dirty-side integration test | ```python
def test_rules_check_cli_fails_closed_on_self_reference(monkeypatch):
    from pbatch import rule_cli
    polluted = {"rules": {"bad": {"description": "x", "min_tier": "standard",
                                  "files": ["backend-specs/rules.yaml"]}}}
    monkeypatch.setattr(rule_cli, "load_registry", lambda domain="": polluted)
    with pytest.raises(SystemExit) as ei:
        rule_cli.rules_main(["--check"])
    assert ei.value.code == 1
# and the clean side already exists: test_rules_check_passes (exit 0)
```
Deterministic, non-mutating, pins CLI → `_check_registries_cli` → `check_registry(paths)` → `sys.exit(1)` end-to-end. |
| F-B | MEDIUM | **F7's proof command is vacuous.** `eval --filter <id>` with a typo'd or missing case id yields `{"total": 0, "failed": 0}` and exit 0 (verified live). §6 step 6's "the new case actually runs" check passes even if the case id is wrong or the case is inert — exactly the failure F7 claims to prevent. | Filter-run must assert case count | ```bash
total=$(python pi-batch.py eval --filter backend-manifest-excludes-repo-internal-policy --json | python -c "import sys,json;print(json.load(sys.stdin)['total'])")
test "$total" = "1"   # FAIL if id typo'd or case missing
```
(CI backstop is real — full `make ci` eval runs the case unfiltered — but the §6 step as written is not evidence.) |
| F-C | MEDIUM | **Acceptance A2 has no gate step.** A2's input `"开发一个客户管理模块的列表查询与新增接口"` appears in no eval case (R3 covers only A1's input), and §6's gate sequence omits both A1/A2 direct grep commands. A2 rests solely on the implementation report's word. | A2 in the gate sequence | Add to §6 step 6: `python pi-batch.py rules "开发一个客户管理模块的列表查询与新增接口" --json | grep -c PY-GODFILE-SPLIT; test $? -eq 1` (grep exit 1 = no match), or add a second eval case per R3.2. |
| F-D | MEDIUM | **Re-pollution vector (E15) untested end-to-end.** `learn promote --files backend-specs/rules.yaml` succeeds today (existence-only check, learn.py:139–141) and the post-promote `rules --check` is a log line, not a gate (learn.py:333). Design relies on CI running the gate later; nothing pins the chain at its source. | learn-promote chain test | ```python
def test_promote_self_referential_files_then_check_fails(tmp_path, monkeypatch, capsys):
    # tmp registry copy + draft fixture; --registry accepts a path (learn.py:140-143)
    reg = tmp_path / "rules.yaml"; reg.write_text("rules: {}\n", encoding="utf-8")
    # _cmd_promote with --files "<reg>" (exists-check passes) → block written verbatim
    # then: check_registry(load_registry(str(reg)), [str(reg.resolve())]) must flag it
```
(Design scoped learn.py changes out — acceptable — but a test at the vector costs ~10 lines and makes F1 detection automatic rather than dependent on someone running the gate.) |
| F-E | MEDIUM-LOW | **`files_template` branch is new code with zero regression tests** (F4's edge documented but unpinned). Only the `files` branch is tested; a dropped/changed template comparison passes all proposed tests. | Template self-reference unit test | ```python
reg = {"rules": {"t": {"description": "x", "min_tier": "standard",
                       "files_template": "backend-specs/{profile}.yaml",
                       "profiles": ["rules"]}}}
assert any("self-referential" in v for v in
           check_registry(reg, registry_paths=["backend-specs/rules.yaml"]))
# plus: profiles: [] → no violation, no crash (F4 edge)
```
(My isolated sketch confirms the branch fires; it just isn't locked.) |
| F-F | LOW | **Path-form claim unpinned.** Headline claims all three forms caught; proposed test covers only the bare-relative form. | Parametrize the unit test | ```python
@pytest.mark.parametrize("form", ["backend-specs/rules.yaml",
                                  "./backend-specs/rules.yaml",
                                  str(Path("backend-specs/rules.yaml").resolve())])
def test_self_reference_all_path_forms(form): ...
```
(Behavior verified in isolation — the design's sketch claim is honest; this only locks it against regression.) |
| F-G | LOW | **Reproduce-first order not mandated** (testing.md §2). Both proposed tests and the eval case fail against HEAD today (self-reference unflagged; rule present) — the red state is available — but §6 order is R1→R2→tests→R3, so the implementer never observes the failing tests before the fix. | Implementation instruction | Migration order should be: write R3 eval case + R2.4 tests → run (observe FAIL: `lacks_rule` present / self-reference not flagged — reproduction) → apply R1+R2 → run gates. One sentence in §6; zero code cost. |

## 3. Risk-coverage matrix

| Risky path | Design's mechanism | Covered by proposed test plan? |
|---|---|---|
| Self-referential `files` detected at `check_registry` level | R2.1/R2.2 comparison | ✅ `test_registry_rejects_self_referential_files` |
| **CLI wiring passes registry paths (A3 dirty side)** | R2.3 `_check_registries_cli` | ❌ **No test — "by construction" only (F-A)** |
| Clean registries stay clean (false-positive side) | E14 scan | ✅ `test_shipped_registries_have_no_self_reference` + `rules --check` in §6 |
| Rule silently re-added (F2) | R3 eval case | ✅ `assert: lacks_rule` (full eval/`make ci`; ⚠ `--filter` proof vacuous, F-B) |
| A2 input leak | A2 direct command | ❌ Not in gate sequence (F-C) |
| `files_template` self-reference (F4) | R2.2 template loop | ❌ No test (F-E) |
| Path-form variants (`./`, absolute, symlink) | R2.5 resolve-normalized | ⚠ Behavior verified once in isolation; no regression test (F-F) |
| Re-promotion via `learn promote` (F1/E15) | R2 gate + CI detection | ❌ No test at the vector; relies on someone running the gate (F-D) |
| Cross-domain registry reference (F5) | Explicitly out of scope | ❌ Documented, not covered (accepted per requirements scope) |
| Ordering slip R2-before-R1 (F8) | C4 + step order | ✅ `test_rules_check_passes` canary + §6 order |
| Backward compat (`check_registry` 1-arg callers) | C1 default-`None` | ✅ existing `test_agent_engineering.py:158` caller + new test's `assert not check_registry(reg)` |
| Eval count bounds 40→41 | C6 | ✅ `>= 20` assertion (test_entrypoints.py:67) + explicit §6 total-41 check |
| Races/timeouts/duplicates/permissions/partial success | — | N/A: deterministic pure functions + one CLI entry; no async, no shared mutable state (isolation requirements satisfied by construction) |

## 4. Honesty audit (completion-evidence)

| Claimed | Executed/verified | Status |
|---|---|---|
| `rules --check` exits 0 with pollution live (E6) | Re-ran: `[backend] registry: OK`, exit 0 | ✅ honest |
| Eval baseline 40 total / 0 failed (E9) | Re-ran: `"total": 40, "failed": 0` | ✅ honest |
| Both A1/A2 inputs emit `PY-GODFILE-SPLIT` | Re-ran both: `grep -c` = 1 each | ✅ honest |
| `eval --filter` command in §6 step 6 | Flag exists (`eval --filter FILTER`) — **but see F-B: exit 0 with 0 cases on typo** | ⚠ real command, **ineffective proof** |
| Code sketches "executed in isolation" | Re-executed §3.1/§3.2 verbatim: all 6 assertions pass (3 path forms, dedupe→1 path, fires on live registry, frontend clean, `None`-default, template branch) | ✅ credible, reproducible |
| `test_rules_check_passes` is a CI canary (E13) | Confirmed at tests/test_agent_engineering.py:141; `selfcheck.py:37` registry gate | ✅ honest |
| `learn promote` writes `--files` verbatim, check only logged (E15) | Confirmed learn.py:139–153, 333 | ✅ honest |
| New violation message contains `"files"` (C2 compat) | Sketch message: `"rule 'bad': files entry '…' resolves to the registry file itself"` — satisfies `test_rules_check_fails_on_broken_registry`'s substring assert | ✅ honest |
| Gate sequence commands exist (`pytest -k`, `make ci`, `quality.py --strict .`) | All real commands in this repo | ✅ honest |
| A1/A2 direct greps | Real commands, but **A2 omitted from §6 sequence** (F-C); A1/A2 rely on implementation-report claims otherwise | ⚠ gap |

No fabricated commands, no "理论上应该通过" phrasing, no skipped tests. The design's own verification discipline is strong — every claim I spot-checked held. The gaps are in what the design *fails to test*, not in what it claims.

## Bottom line

The test plan correctly pins the *specific* defect (eval case) and the *class* of defect (function-level unit test), and every stated fact checks out. But the primary acceptance A3 — `rules --check` exits non-zero on a self-referential registry — is guarded by **zero tests that would fail** if the core wiring is omitted, and the §6 proof command for F7 is vacuous. Both are fixed by the two exact tests above (F-A, F-B) plus two small gate-sequence additions (F-C, A2 grep). With those, this design's test plan is complete.

**VERDICT: FAIL - blocking test gaps: no CLI dirty-side test pins `_check_registries_cli` passing `registry_paths` (A3 is "by construction" only — a one-line wiring omission passes every proposed gate), and §6's `eval --filter` step returns exit 0 with 0 cases on a typo'd id, so the F7 proof is vacuous; add the two specified tests (F-A, F-B) plus A2's grep step before implementation.**
