# Requirements Specification — Gate placeholder validation: unsupported `{log}` silently false-passes the evolution node gate

| Field | Value |
|---|---|
| Module | `agent-engineering` |
| Direction id | `36bd39dc` |
| Source analysis | `docs/auto/analyses/agent-engineering-36bd39dc.json` (direction #1) |
| Status | Proposal (evidence-verified) |
| Scope | This direction only — no expansion |

---

## 1. Summary

`agent-engineering/evolution.yaml:31` declares `- pi-batch retro --actions {log}`,
but the org gate machinery only knows the placeholders `{task}` / `{code}` / `{file}`.
The unsupported token `{log}` is never validated (static layer) and never substituted
or skipped (runtime layer) — it is passed through to the shell verbatim, where
`pi-batch retro` treats it as a literal path, scans zero files, exits 0, and the
evolution node gate reports `passed`. This violates the documented contract
「缺参不伪装通过」and constitutes a fail-closed trust-boundary defect.

The fix has three parts: (R1) `org check` rejects unsupported placeholder tokens
fail-closed, (R2) `run_gate` never executes or marks `passed` a check containing an
unsupported token, and (R3) the repo's own `evolution.yaml:31` is corrected so the
shipped tree stays valid (required by AC4, since `make ci` runs `python pi-batch.py org check`).

---

## 2. Evidence verification

Every citation in the direction was re-verified against the current repository.

| Cited evidence | Verification result |
|---|---|
| `agent-engineering/evolution.yaml:31` — `- pi-batch retro --actions {log}` | ✅ **Confirmed**, exact match. Line 31 of the `auto_checks` block (lines 30–34). Only `{log}` in the entire tree; all other 37 placeholder uses are `{task}`×22 / `{code}`×12 / `{file}`×3 (Appendix A). |
| `pbatch/org.py:166` — "run_gate only substitutes {task}/{code}/{file}" | ✅ **Confirmed with refinement.** `run_gate` (line 139) builds `params = {"{task}": task, "{code}": code_dir, "{file}": file_path}` at **line 149**. The substitution loop `cmd = cmd.replace(key, value)` sits at **lines 164–166** inside `_execute_check` (line 161). Either way: `{log}` is never a substitution key. |
| `pbatch/org.py:57` — `_check_exists` validates only the command name, never placeholder tokens | ✅ **Confirmed, exact.** `def _check_exists(check, commands)` spans lines 57–67: for `pi-batch X …` it checks only `parts[1] in commands` (line 62–63); the remaining argument tokens are never inspected. `retro` is a known subcommand (`pbatch/cli.py:306`), so the check passes. |
| `pbatch/org.py:161` — `_execute_check` | ✅ **Confirmed, exact.** Def at line 161; substitution 164–166; executes via `subprocess.run(shell=True, timeout=120)` (169–171); `ok = result.returncode == 0` (173); `status = "passed" if ok else …` (174–175). |
| `docs/AGENT_ORGANIZATION.md:256` — 「缺参不伪装通过」 | ⚠️ **Miscited line number — content confirmed.** The file is **247 lines**; the contract text sits at **line 246**: `auto_checks = 该节点的质量门禁（org gate 可执行化；缺参不伪装通过）`. The supported placeholder set and skipped semantics are documented at **line 234**: `（{task}/{code}/{file} 占位符；缺参 → skipped；…）`. |

Supporting verification (live, this repository):

```
$ python pi-batch.py retro --actions '{log}'
== run retro: 1 file(s) ==
  task_durations     {'count': 0}
== evidence (first lines per category) ==
$ echo $?
0
```

The literal `{log}` is used as a path/glob, zero evidence is produced, exit code is 0 —
so `_execute_check` classifies it `passed` (org.py:173–175) and `_summarize` yields
`gate_passed: True` (org.py:193). Separately:

```
$ python pi-batch.py org check        # current behavior
valid: True                            # --json shows node_count 17, no problems
```

`org check` today reports the tree valid despite the unsupported placeholder.

---

## 3. Root-cause analysis

Two independent layers both fail to validate placeholder tokens:

**Layer A — static validation (`org_check` / `_check_exists`, org.py:57–67, 70–111).**
`_check_exists` inspects only the first token(s): for `pi-batch X …` it tests
`X ∈ _SUBCOMMANDS`; for other commands it tests the first token against the known
command set or filesystem. Argument tokens such as `--actions {log}` are never
examined. Consequently `org check` (org.py:70, called by `_cmd_check` org.py:408,
exit 1 on invalid) reports `valid: True` for a check that can never run correctly.

**Layer B — runtime gate (`run_gate` / `_execute_check`, org.py:139–177).**
The missing-parameter guard (org.py:152–157) checks only the *supported* keys:
`missing = [key for key, value in params.items() if key in str(check) and not value]`.
`{log}` is not a key of `params` (line 149), so it is neither skipped as 缺参 nor
substituted — the literal token flows into `cmd` (164–166) and into the shell.
`pi-batch retro --actions {log}` (retro.py:244–268; `targets nargs="+"`, "log files
or directories") scans zero `*.log` files, exits 0 → `status = "passed"` → the gate
reports passed. The evolution node gate therefore *fabricates a pass* for a command
that inspected nothing.

**Contract violation.** `docs/AGENT_ORGANIZATION.md:246` states the gate must not
fake a pass on missing parameters; line 234 fixes the supported set to
`{task}/{code}/{file}`. `{log}` is not among them, yet both layers silently accept it.

---

## 4. Scope boundary

**In scope:**
- Fail-closed placeholder validation in `org_check`/`_check_exists` (static layer).
- Defensive non-execution / no-false-pass in `run_gate`/`_execute_check` (runtime layer).
- Correction of the single defect site `agent-engineering/evolution.yaml:31`
  (required — see AC4, `make ci` runs `python pi-batch.py org check`).
- Regression tests in `tests/test_org.py`; `make ci` green.

**Out of scope (explicitly not part of this direction):**
- Adding a new `{log}` parameter (or any new placeholder) to `run_gate` / the
  `org gate|run` CLI / `run_flow` — the supported set stays `{task}/{code}/{file}`.
- Changing `retro`'s semantics (scanning zero files and exiting 0 is retro's own
  design; the defect is the gate layer delegating to it with a dead placeholder).
- The two other directions in the same analysis file (role-template drift,
  `run_flow` swallow of node errors / `_summarize` unreachable duplicate code at
  org.py:194–202) — noted as context only.

---

## 5. Requirements

**R1 (static, fail-closed) — `org check` rejects unsupported placeholder tokens.**
`org_check` (org.py:70) must treat an `auto_checks` entry as invalid when it contains
a placeholder token of the form `{name}` (regex `\{[A-Za-z_][A-Za-z0-9_]*\}`) whose
name is not in the supported set `{task, code, file}`. The problem entry must identify
the node id, the offending check, and the token, e.g.
`evolution: auto_check 占位符不支持: {log}`. `report["valid"]` must be `False`, and
the `org check` CLI (org.py:408) must exit nonzero (it already exits 1 on
`valid=False`). The supported set must be a single module-level constant shared with
the runtime layer (D1), not a duplicate literal.

**R2 (runtime, no false pass) — `run_gate` never executes or marks `passed` a check
containing an unsupported placeholder token.**
Before the missing-parameter guard (org.py:152) and before execution, `run_gate` must
scan each check for `{name}` tokens outside the supported set. On detection the check
must NOT be executed and must NOT be marked `passed`; it must be recorded as
`status: "skipped"` with `reason: "不支持的占位符 {log}"` (consistent with the
existing 缺参 → skipped contract documented at docs/AGENT_ORGANIZATION.md:234; the
direction explicitly permits "skipped with reason or failed" — see D3 for the pinned
choice and rationale). Supported placeholders keep their current behavior exactly:
empty value → skipped 「缺少参数」; non-empty → substituted and executed.

**R3 (self-healing) — `agent-engineering/evolution.yaml:31` no longer uses `{log}`.**
The line `- pi-batch retro --actions {log}` must be replaced with a check using only
supported placeholders. Recommended: `- pi-batch retro --actions {code}` — `retro`
accepts directories (retro.py:246) and scans `*.log` recursively, so the evolution
node keeps its 失败日志→改进动作 review capability, and `{code}` is the closest
supported parameter (project root where run logs live). Alternative acceptable fix:
delete the line. Constraint: no new placeholder, no new `org gate` CLI parameter,
no other `auto_checks` line may change. After R3, `python pi-batch.py org check` must
report `valid: True` on the shipped tree (required by the `org` gate in `make ci`).

**R4 (regression tests) — `tests/test_org.py` gains the three tests specified in
AC2, AC3 and the CLI-behavior test in AC1** (test plan in §7), following the file's
existing idioms (`monkeypatch.setattr(org, "ORG_DIR", tmp_path)`, full 9-要素 fixture
YAML as in `test_org_check_detects_bad_command`, tests/test_org.py:54–64).

**R5 (optional, non-blocking) — documentation.** Add one line to
`docs/AGENT_ORGANIZATION.md` §3 (near line 234) noting that `org check` rejects
unsupported placeholder tokens fail-closed. The existing contract text (lines
234, 246) is otherwise already accurate and needs no change.

---

## 6. Acceptance criteria (supplied, made testable)

The four supplied acceptance checks are preserved; each is pinned to a deterministic
assertion. All are automatable in `tests/test_org.py` except AC4's `make ci` leg.

**AC1 — mechanism + CLI fail-closed.**
> `python pi-batch.py org check` reports evolution auto_check with unsupported
> placeholder {log} and exits nonzero (new fail-closed check)

Testable form: with a fixture `ORG_DIR` containing a valid 9-要素 node whose
`auto_checks` includes `pi-batch retro --actions {log}`:
- `org.org_check()` returns `valid is False` with a problem containing `{log}` and
  the node id (covers "reports evolution auto_check with unsupported placeholder");
- the CLI exits nonzero: invoke `_cmd_check` with patched `ORG_DIR` and assert
  `pytest.raises(SystemExit)` with `value.code == 1` (or run
  `python pi-batch.py org check` as a subprocess with `ORG_DIR` fixture and assert
  exit code 1 — the latter also exercises the real entrypoint).
- Note: on the *shipped tree after R3*, `org check` must exit 0 (that is AC4's
  `make ci` org-gate leg); the nonzero requirement applies to configs containing
  `{log}`.

**AC2 — static unit test.**
> tests/test_org.py gains a test: org_check() on a fixture node with auto_checks
> ['pi-batch retro --actions {log}'] returns valid=False

Pinned assertion:
```python
report = org.org_check()          # ORG_DIR monkeypatched to fixture
assert report["valid"] is False
assert any("{log}" in p and "evolution" in p for p in report["problems"])
```

**AC3 — runtime unit test.**
> tests/test_org.py gains a test: run_gate('evolution', task=..., code_dir=...)
> never marks the {log} check 'passed' (it is skipped with reason or failed)

Pinned assertion (behavior pinned to skipped-with-reason, D3):
```python
report = org.run_gate("evolution", task="t", code_dir="c")   # fixture node id=evolution
entry = next(r for r in report["results"] if "{log}" in r["check"])
assert entry["status"] == "skipped"          # never "passed"
assert "不支持的占位符" in entry["reason"] and "{log}" in entry["reason"]
```
The test must also assert the check was not executed (status "skipped" proves it,
since execution of `{log}` yields "passed" — the defect being fixed).

**AC4 — regression gate.**
> `python -m pytest tests/test_org.py -q` and `make ci` stay green

`make ci` includes the `org` gate (`python pi-batch.py org check`, Makefile:47–50),
which after R1/R3 passes only if the shipped tree is valid — this is why R3 is a
hard requirement of this direction and why the AC1 nonzero-exit test must use a
fixture directory rather than the repo's own `agent-engineering/`.

---

## 7. Test plan (`tests/test_org.py`)

| # | Test | Assertion | Models |
|---|---|---|---|
| T1 | `test_org_check_detects_unsupported_placeholder` (AC1+AC2) | `valid is False`; problem names node id + `{log}`; CLI exit 1 | `test_org_check_detects_bad_command` (line 54) for fixture style; `test_org_cli_check_json` (line 82) for CLI style |
| T2 | `test_org_gate_unsupported_placeholder_skipped` (AC3) | fixture node id `evolution`; entry status `skipped`; reason contains `不支持的占位符` + `{log}`; no result for that check is `passed` | `test_org_gate_skipped_when_param_missing` (line 135) |
| T3 | `test_auto_checks_reference_real_commands` (existing, line 39) must keep passing | repo tree valid after R3 | — |

Also run: `python -m pytest tests/test_org.py -q`, `python quality.py --strict pbatch/org.py`,
`make ci`.

---

## 8. Design decisions

**D1 — single source of truth for the placeholder set.** Define a module-level
constant, e.g. `SUPPORTED_PLACEHOLDERS = ("{task}", "{code}", "{file}")` in
`pbatch/org.py`, referenced by both `org_check` (R1) and `run_gate` (R2). The root
cause of this defect is that the two layers each carried independent, implicit
knowledge of the placeholders (params dict at org.py:149 vs. `_check_exists`'s
token-blind check); a shared constant prevents future drift.

**D2 — token detection.** Use `re.findall(r"\{[A-Za-z_][A-Za-z0-9_]*\}", check)`.
A token is unsupported iff it matches and is not in `SUPPORTED_PLACEHOLDERS`.
Current inventory (Appendix A) shows this is precise: only `{log}` is affected; no
auto_check uses shell-variable syntax that could collide.

**D3 — runtime behavior pinned to `skipped` with reason.** The direction allows
"skipped with reason or failed". This spec pins **skipped with reason**
(`不支持的占位符 {log}`): it matches the acceptance's primary phrasing and stays
consistent with the existing documented contract for non-executable checks
(缺参 → skipped, docs:234), keeping one code path and one status vocabulary. The
authoritative fail-closed enforcement is the static layer (R1/AC1/AC2: `org check`
exit 1, run by `make ci` and `pi-batch check`), which makes the runtime path
unreachable for shipped configs; for stale or foreign configs the runtime layer
guarantees the check is never executed and never marked `passed` (AC3). ("failed"
remains an acceptable alternative per the direction if the implementer prefers
gate-blocking; if chosen, T2's assertion changes to `status == "failed"` and reason
non-empty — the direction explicitly permits both, but only one may be implemented.)

**D4 — evolution.yaml fix maps `{log}` → `{code}`.** `retro_main` accepts
directories (retro.py:246–248: `targets`, "log files or directories"; scans via
`Path(t).rglob("*.log")`). `{code}` preserves the node's review capability with a
supported placeholder and zero new parameters. Deleting the line is the accepted
fallback; any other change to `evolution.yaml` is out of scope.

**D5 — no new status value, no `gate_passed` semantic change.** R2 reuses the
existing `skipped` status; `_summarize` (org.py:185) and `_cmd_gate`/`_cmd_run`
outputs need no format changes. (The unreachable duplicated body in `_summarize`
at org.py:194–202 belongs to a different direction and is out of scope here.)

---

## 9. Impact and risks

- **Blast radius:** `pbatch/org.py` only (`_check_exists`/`org_check` +
  `run_gate` + new constant) and `agent-engineering/evolution.yaml` one line.
  All other nodes' auto_checks use only supported placeholders (Appendix A), so R1
  introduces no new problems on the shipped tree (verified by existing test
  `test_auto_checks_reference_real_commands` and AC4).
- **Runtime behavior change:** none for valid configs. Invalid configs are now
  rejected statically and never false-pass at runtime.
- **CI coupling:** R1 alone would break `make ci` (org gate) until R3 lands; R1–R3
  must be implemented in the same change (AC4).
- **Failure mode if skipped:** an operator running `org gate`/`org run` against a
  stale config sees the check marked `·` (skipped) with reason recorded in JSON and
  the check text printed (org.py:248–250 prints `check[:70]`, which includes
  `{log}`); the static check remains the blocking enforcement point.

---

## Appendix A — placeholder inventory (`agent-engineering/*.yaml` auto_checks)

| Token | Count | Nodes | Supported? |
|---|---|---|---|
| `{task}` | 22 | api, code_review, domain, orchestrator, product, release, requirement, security, ux_designer, performance, evolution, data, … | ✅ |
| `{code}` | 12 | architecture, data, developer, performance, ux_designer, evolution, code_review, … | ✅ |
| `{file}` | 3 | code_review, developer, testing | ✅ |
| `{log}` | 1 | **evolution (line 31)** | ❌ — the defect |

## Appendix B — key code excerpts (current, line numbers verified)

```python
# pbatch/org.py:57-67 — Layer A defect: token-blind existence check
def _check_exists(check: str, commands: set) -> bool:
    parts = str(check).split()
    if not parts:
        return False
    first = parts[0]
    if first == "pi-batch":
        return len(parts) > 1 and parts[1] in commands   # never inspects {log}
    ...

# pbatch/org.py:149, 152-157 — Layer B defect: only supported keys are guarded
params = {"{task}": task, "{code}": code_dir, "{file}": file_path}
missing = [key for key, value in params.items()
           if key in str(check) and not value]           # {log} never "missing"

# pbatch/org.py:164-166, 173-175 — literal {log} reaches the shell; exit 0 → "passed"
cmd = cmd.replace(key, value)                            # only supported keys
...
ok = result.returncode == 0
status = ("passed" if ok else ...)                       # retro {log} → passed
```

```yaml
# agent-engineering/evolution.yaml:30-34 — the defect site
auto_checks:
- pi-batch retro --actions {log}      # line 31 — unsupported placeholder
- pi-batch advance --dir {code}
- pi-batch reflect --task {task}
- pi-batch project verify
```

```text
# docs/AGENT_ORGANIZATION.md:234, 246 — the violated contract
（{task}/{code}/{file} 占位符；缺参 → skipped；reflect 低分 → attention 不阻塞；…）
auto_checks = 该节点的质量门禁（org gate 可执行化；缺参不伪装通过）
```
