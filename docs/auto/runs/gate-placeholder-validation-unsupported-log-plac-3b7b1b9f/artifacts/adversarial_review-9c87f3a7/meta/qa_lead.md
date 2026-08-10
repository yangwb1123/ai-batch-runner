# QA Lead — Risk-Based Test Review: Gate Placeholder Validation (`{log}` false-pass)

Scope: design for `gate-placeholder-validation-unsupported-log-plac-3b7b1b9f` (Campaign direction: *"unsupported `{log}` placeholder silently false-passes the evolution node gate"*), revision as of 2026-08-08. Advisory analysis only — no files modified. Note on artifact traceability: the pipeline's `task-1-design.md` artifact is a 17-line evidence wrapper; the referenced 425-line body (F1–F7 table, T1a/T1b/T2/T3) is not in the file. The review below reconstructs the design from the wrapper summary plus **live code verification**.

---

## 1. Test inventory and commands actually run (this revision)

| # | Command | Result | Evidence status |
|---|---|---|---|
| 1 | `python -m pytest tests/test_org.py -q` | **43 passed in 24.57s** | Verified (baseline) |
| 2 | `python -m pytest tests/test_retro.py -q` | **1 passed in 0.10s** | Verified |
| 3 | `python pi-batch.py org check` | `valid`, 17 nodes, stages 0–16, **exit 0** | Verified |
| 4 | `python pi-batch.py check --steps org --skip-eval` | `all 1 gates passed` (selfcheck.py:38 org step) | Verified |
| 5 | `python pi-batch.py retro --actions '{log}'` | `1 file(s)`, zero evidence, **exit 0** — false-pass repro | Verified |
| 6 | `python pi-batch.py org gate evolution --code . --task x --file x.md --json` | `{log}` check → **`passed`, exit 0** (zero files scanned); `gate_passed=False` only because unrelated `project verify` fails | Verified |
| 7 | `python pi-batch.py org gate requirement --json` (empty task) | 4× `skipped 缺少参数 {task}` — 缺参 guard intact | Verified |
| 8 | Placeholder inventory (regex scan, agent-engineering/*.yaml) | `{task}`×22, `{code}`×12, `{file}`×3, `{log}`×1 = **38/38**, `{log}` sole unsupported | Verified |
| 9 | Regex `\{[A-Za-z_][A-Za-z0-9_]*\}` false-positive scan | zero hits beyond the 38 tokens; `${VAR}` only in `scripts/*.sh` (not auto_checks) | Verified |
| 10 | `python quality.py pbatch/org.py` | `quality: OK` — dup-body detector does **not** flag `_summarize` dead code (org.py:194–202) | Verified |
| 11 | Code reads: `pbatch/org.py` (57–67 `_check_exists` token-blind; 149 params; 164–166 replace; 161 `_execute_check`; 185 `_summarize` dead dup), `pbatch/retro.py:95/:263` (dirs via `rglob("*.log")`), `Makefile:47–50` (`ci: check self-test test quality org`; `org:` → `python pi-batch.py org check`), `selfcheck.py:38`, `pytest.ini` markers | All design claims confirmed | Verified |
| 12 | `grep -rln "org_check|run_gate" pbatch/eval.py evals/ checks/` | **zero coverage** — regression protection lives solely in tests/test_org.py | Verified |

**Not run** (explicit gap): full `make ci` (includes eval + harness + self-test; ~minutes). Coverage claims below are bounded to the commands above; line coverage was not measured (`pytest-cov` target exists at `Makefile coverage:` but was not part of this review).

**Suite taxonomy** (per `prompts/README.md`): default CI = `make ci` (check/self-test/test/quality/org + eval), and `pytest` runs **all** markers — `unit/contract/property/scenario/chaos` (pytest.ini). There is **no separate chaos, load, benchmark, or conformance suite** in this repo; `chaos` is declared but unused. For this change only the default suite is relevant; nothing tagged/manual applies.

---

## 2. Requirement-to-test matrix

| Requirement | Test (existing/new) | Status | Evidence |
|---|---|---|---|
| R1 static fail-closed: `org check` rejects unsupported `{name}` (AC1) | **None exists** — no CLI test asserts exit 1 on invalid org dir | **Missing** | Live: `org check` exit 0 with `{log}` present (evolution.yaml:31) |
| R2 runtime: `{log}` never `passed` (AC3) | **None exists** | **Missing** | Live: run #6 shows `{log}` → `passed` (bug) |
| R3 self-healing: evolution.yaml:31 → `--actions {code}` (AC4) | **None** (yaml unchanged) | **Missing** | grep confirms line 31 still `{log}` |
| D1 shared `SUPPORTED_PLACEHOLDERS` + anti-drift | **None** (constant doesn't exist) | **Missing** | org.py:149 hardcodes params dict |
| 缺参 guard regression (`{task}` → skipped) | `test_org_gate_skipped_when_param_missing`, `test_org_run_flow_passed_semantics` | **Passing** | run #7; suite run #1 |
| Shipped tree org_check valid | `test_auto_checks_reference_real_commands`, `test_org_check_parent_valid`, `test_org_cli_check_json`, `test_org_cli_flow` | **Passing** | suite run #1 |
| Command-existence check (token-blind, unchanged) | `test_check_exists_pi_batch_subcommand`, `test_check_exists_file_reference`, `test_org_check_detects_bad_command` | **Passing** | suite run #1 |
| `pi-batch check` aggregation incl. org | `test_check_steps_include_org_and_capabilities`, `test_check_run_step_ok`, `test_check_cli_full` | **Passing** | run #4 |
| Retro dir scanning (R3 precondition) | `test_retro.py` (single test, no `--actions`/dir coverage) | **Partial** | retro.py:95/:263 code-read only |
| Doc contract 「缺参不伪装通过」 (AGENT_ORGANIZATION.md:234/:246) | none | **Missing** (doc unchanged) | content verified at :234/:246; :256 miscite confirmed (file is 247 lines) |
| **Feature total** | 0 of 4 ACs have tests | **All Missing** | — |

---

## 3. Findings (severity-sorted)

### F1 — High — AC3 test as designed is environment-dependent and currently red adjacent
`run_gate('evolution', task=..., code_dir=...)` executes the **real** node: `advance --dir .` (whole-repo scan), `reflect --task` (writes `.pi-batch/reflections.jsonl`), and `project verify`, which **fails on the shipped tree right now** (decisions.yaml/ADR index inconsistency — measured in run #6). A test asserting only the `{log}` result would pass but be slow and stateful; any gate-level assertion would be nondeterministic.

**Exact test to add** (fixture-based, deterministic — modeled on `test_org_check_detects_bad_command`):
```python
def test_org_gate_unsupported_placeholder_never_passed(tmp_path, monkeypatch):
    (tmp_path / "node.yaml").write_text(  # ALL 15 REQUIRED_KEYS, role_template → real file
        "id: evolution\nname: E\nrole: r\nstage: 0\nparent: \"\"\n"
        "responsibilities: []\ninputs: []\noutputs: []\ncheck_rules: []\n"
        "forbidden: []\nbest_practices: []\nfailure_cases: []\n"
        "auto_checks:\n- pi-batch retro --actions {log}\n- echo ok {task}\n"
        "review_checklist: []\nrole_template: prompts/critic.md\n",
        encoding="utf-8")
    monkeypatch.setattr(org, "ORG_DIR", tmp_path)
    report = org.run_gate("evolution", task="t", code_dir="d")
    log_hits = [r for r in report["results"] if "{log}" in r["check"]]
    assert log_hits and all(r["status"] == "skipped" for r in log_hits)
    assert all(r["reason"] == "不支持的占位符 {log}" for r in log_hits)
```
Acceptance assertion: `{r["status"] for r in log_hits} == {"skipped"}` and `report["results"][-1]["status"] == "passed"` (supported `echo {task}` still substitutes — byte-identical claim). **Note**: the design's own verification already caught the missing-`REQUIRED_KEYS` fixture bug (role/role_template) — pin that in the fixture comment.

### F2 — High — "skipped" does not block `gate_passed`: residual false-pass at gate level
With R2, an unsupported token yields `skipped`, and `_summarize` computes `gate_passed = (failed == 0)` — a node whose only check is unsupported would report **gate PASS with 100% skipped**. The AC wording ("skipped with reason or failed") permits this, but for a trust-boundary fix it preserves a weaker form of the original bug.

**Recommendation**: (a) unsupported token → `failed` (true fail-closed), or (b) gate-level guard `gate_passed = failed == 0 and not any(r["reason"] == 不支持的占位符…)`. If (a)/(b) rejected, pin the accepted semantics in a test and in the doc contract.

**Exact test to add** (if (b) adopted):
```python
def test_org_gate_unsupported_token_blocks_gate(tmp_path, monkeypatch):
    # fixture node with auto_checks: [pi-batch retro --actions {log}]
    report = org.run_gate("evolution", task="t", code_dir="d")
    assert report["gate_passed"] is False
    assert report["skipped"] == 1 and report["failed"] == 0
```

### F3 — Medium — R1 scanner boundary is unpinned (over-block vs. blind-spot)
Measured: regex `\{[A-Za-z_][A-Za-z0-9_]*\}` matches exactly the 38 tokens, zero false positives today. But two edges are untested: (a) **over-block** — future `python -c "x = {y}"`-style auto_checks would be flagged unsupported (F3 in design); (b) **blind-spot** — `${VAR}` is *not* matched, so a shell variable still flows verbatim into `shell=True` and can silently expand empty (same failure class as the bug being fixed).

**Exact test to add** (pins collision behavior, prevents future regression):
```python
def test_org_check_shell_syntax_not_flagged(tmp_path, monkeypatch):
    # fixture (all REQUIRED_KEYS) with auto_checks:
    #   ["awk '{print $1}' file", "echo ${VAR:-x}", "sed 's/{code}/y/' f"]
    report = org.org_check()
    assert report["valid"] is True  # no false positives
```
Acceptance: `not any("占位符" in p for p in report["problems"])`; and a companion case asserting `{log}` in the *same* fixture yields exactly one problem `evolution: auto_check 占位符不支持: {log}` (message format pinned).

### F4 — Medium — regression protection gap: zero eval/checks coverage
`evals/` and `checks/` never touch `org_check`/`run_gate` (measured); the entire feature's safety net is tests/test_org.py. Add the CLI-level exit-code test missing from the suite (existing CLI tests cover only exit 0/2):

```python
def test_org_cli_check_invalid_exits_1(tmp_path, monkeypatch, capsys):
    # fixture with {log} auto_check
    monkeypatch.setattr(org, "ORG_DIR", tmp_path)
    with pytest.raises(SystemExit) as exc:
        org.org_main(["check"])
    assert exc.value.code == 1
    assert "占位符不支持" in capsys.readouterr().out
```
**Constraint**: do **not** mark new tests `scenario` — `test_pytest_markers_filter` asserts `"2 passed"` for `-m scenario` on test_org.py; adding scenario-marked tests breaks it.

### F5 — Low — dead `_summarize` duplicate (org.py:194–202) and anti-drift assert placement
Verified: unreachable duplicate body exists; `quality.py` does **not** detect it (measured run #10). Out of scope per requirements, but R2 edits this exact function — deleting the dead block costs nothing and avoids future maintainer confusion. Separately, the design's "anti-drift `assert` in `run_gate`" is fragile: stripped under `python -O` and crashes with raw `AssertionError`. Prefer an explicit invariant test: `assert set(org.SUPPORTED_PLACEHOLDERS) == set(run_gate's params)` via monkeypatched `_execute_check` — test-pinned, not runtime-crashed.

### F6 — Low — doc contract drift
`AGENT_ORGANIZATION.md:234` documents 「（{task}/{code}/{file} 占位符；缺参 → skipped…）」 and :246 「缺参不伪装通过」. After R2, the contract should read 「缺参/不支持的占位符 → skipped，不伪装通过」. Recommend landing the doc update in the same commit as R3 (the :256 miscite correction already noted in requirements). No test needed; manual doc review suffices.

---

## 4. Prioritized scenario list (happy / boundary / error / race / recovery)

| # | Scenario | Path | Deterministic? |
|---|---|---|---|
| S1 | Shipped tree post-R3: 38/38 supported tokens, `org check` valid, exit 0 | happy | ✅ |
| S2 | `org gate evolution --code <dir-with-logs>` — `retro --actions {code}` runs, evidence non-empty (retro.py:95 dir branch) | happy | ✅ (fixture logs) |
| S3 | Supported substitution byte-identical: requirement gate with full task → assess/atomize/impact execute (existing `test_org_gate_requirement_ok`) | happy | ⚠️ real subprocess |
| S4 | Fixture with `{log}` → `org_check` valid=False, exactly one problem, message `evolution: auto_check 占位符不支持: {log}`; `--json` problems length 1; CLI exit 1 | boundary | ✅ |
| S5 | Check containing both `{log}` and `{task}` with empty task → precedence pinned (unsupported reason wins, exactly one result, no double-count) | boundary | ✅ |
| S6 | Multiple distinct unsupported tokens in one check → problem message lists all (deterministic order), run_gate skips once with joined/pinned reason | boundary | ✅ |
| S7 | Shell-syntax checks (`awk '{print $1}'`, `echo ${VAR}`, `sed 's/{code}//'`) → not flagged (F3 test) | boundary | ✅ |
| S8 | Fixture missing `role`/`role_template` (the design's own caught bug) → still detected as 缺少要素, not confused with placeholder class | error | ✅ |
| S9 | `run_gate` unknown node → `{"error": ...}` exit 2 (existing `test_org_gate_unknown_node`) | error | ✅ |
| S10 | `_execute_check` timeout/OSError paths unchanged (120s bound, shell=True) | error | ✅ (existing) |
| S11 | Concurrent `run_gate` calls on same node — R1/R2 add no module-level state; `org_check`'s `_known_commands()` read-only. No race surface introduced; optional 2-thread smoke test | race | ✅ |
| S12 | Partial landing (R1 without R3) → `make ci` `org:` target fails (Makefile:49) — migration order enforced; covered by AC4 green-state test, not by CI itself | recovery | ✅ |
| S13 | Rollback = single-commit revert; post-revert `org check` valid (manual verify step in migration plan) | recovery | manual |
| S14 | `pi-batch check --steps org` green post-change (selfcheck.py:38 subprocess step) | recovery | ✅ |

---

## 5. CI/manual-suite gaps, flake risks, fixtures, exit criteria

**Gaps**
- Feature has 0 test coverage at this revision; no CLI exit-1 test exists anywhere in test_org.py.
- `test_retro.py` (1 test) doesn't cover `--actions` dir scanning — R3's precondition is code-read-only. Add `test_retro_actions_scans_dir` (tmp dir with one `*.log`) asserting `--actions` yields exit 0 and category counts from the fixture log.
- No org coverage in evals/checks — accepted if F4's CLI test lands; otherwise note as standing gap.

**Flake risks**
- AC3-as-designed against the real evolution node (F1): `project verify` currently red, `advance` runtime grows with repo, `reflect` writes state. **Fixture node + `monkeypatch.setattr(org, "ORG_DIR", ...)` is mandatory.**
- New tests must stay on no-op/fast commands (120s `shell=True` subprocess bound is the flake ceiling).
- Do not add `scenario`-marked tests to test_org.py (`test_pytest_markers_filter` pins "2 passed").
- Node-count/stage assertions (`>= 16`, consecutive stages) — fixture dirs must be `monkeypatch`-isolated; never mutate shipped `agent-engineering/` in tests.

**Fixtures needed**
1. `valid_node_yaml` — all 15 REQUIRED_KEYS, `role_template: prompts/critic.md` (must exist or the template-exists check fires).
2. `unsupported_token_node` — same, `auto_checks: ["pi-batch retro --actions {log}"]`.
3. `shell_syntax_node` — `awk '{print $1}'` / `echo ${VAR:-x}` / `sed 's/{code}/y/'`.
4. `mixed_token_node` — `{log}` + `{task}` in one check (S5/S6 precedence).

**Exit criteria (all four ACs, measured)**
1. **AC1**: `org check` on fixture dir → exit 1, message `evolution: auto_check 占位符不支持: {log}`; shipped tree post-R3 still valid (run #3 command, exit 0).
2. **AC2**: `test_org_check_unsupported_placeholder_invalid` — `org_check()` on fixture #2 → `valid is False`, `any("占位符不支持" in p for p in problems)`.
3. **AC3**: `test_org_gate_unsupported_placeholder_never_passed` (F1 test) — `{log}` result `status == "skipped"`, `reason == "不支持的占位符 {log}"`, never `passed`; supported check still substitutes.
4. **AC4**: `python -m pytest tests/test_org.py -q` green (43 existing + ~6 new) **and** `make ci` green — the latter is the real regression net (Makefile:47 `org:` dependency) and the only gate that catches a partial R1-without-R3 landing.

Plus: `python quality.py --strict .` green (org.py modified), and recommended Low items: delete `_summarize` dead dup, replace runtime `assert` with test-pinned invariant, update AGENT_ORGANIZATION.md:234/:246 contract wording.
