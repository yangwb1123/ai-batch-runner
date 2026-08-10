All evidence verified. I now have a complete picture: live bug reproduction, the exact code paths, the budget gates, and the artifact trail. Here is the architecture review.

---

# Architecture Review — Gate placeholder validation (`{log}` false-pass fix)

**Role:** senior architect, advisory only — no files modified.

## 1. Scope, assumptions, and verified architecture summary

**Scope:** one campaign direction (`docs/auto/analyses/agent-engineering-36bd39dc.json` #1) affecting `pbatch/org.py` (org_check static validation + run_gate runtime execution), `agent-engineering/evolution.yaml`, and `tests/test_org.py`. The design proposes D1 (shared `SUPPORTED_PLACEHOLDERS` constant + regex scanner), R1 (static fail-closed scan in `org_check`), R2 (runtime skip for unsupported tokens before the 缺参 guard), R3 (same-commit correction of `evolution.yaml:31`).

**Assumptions / caveats (evidence integrity):**
- **Verified** — the bug is real and reproduced live: `python pi-batch.py org check` → `valid` exit 0 on the shipped tree despite `{log}`; `python pi-batch.py retro --actions '{log}'` → "1 file(s) scanned, count 0", exit 0 → `_execute_check` marks `passed` → `gate_passed=True` (org.py:173–175, 193). The literal token flows to `shell=True` verbatim.
- **Verified** — placeholder inventory: `{task}`×22, `{code}`×12, `{file}`×3, `{log}`×1 (38/38); `{log}` is the sole unsupported token. `AGENT_ORGANIZATION.md` is 247 lines — the `:256` citation is a miscite; content at :246, contract at :234. `make ci` → `org:` target (Makefile:49–50) and `pbatch/selfcheck.py:38` both call `org check` — the AC4 coupling is real. `retro.py:95` accepts dirs via `rglob("*.log")` — R3's `--actions {code}` is executable.
- **⚠️ Discrepancy** — the design evidence claims the artifact is "(425 lines)" but the on-disk `task-1-design.md` is **17 lines** (a summary pointer). The detailed design exists only in the pipeline evidence stream, not in the durable artifact. Additionally the `adversarial_review` stage **PASSED but produced zero artifacts** (`artifacts/adversarial_review-9c87f3a7/meta/` is empty). This review partially fills that gap, but the pipeline evidence chain is weaker than the state machine implies.
- `docs/architecture/DIRECTORY_MAP.md` (referenced by the prompt) **does not exist** in this tree; `docs/ARCHITECTURE.md` used as canonical.

**Layer mapping (per docs/ARCHITECTURE.md):** the change is confined to the governance/verification layer (`pbatch/org.py` = Decision Kernel + gate execution). It touches **no** Kernel Contract ABI (Task/Event/Evidence/Artifact schemas untouched), **no** Capability Registry entry, **no** CLI interface change (all function signatures unchanged: `org_check`, `run_gate`, `_check_exists`), and **no** infrastructure/protocol layer. Ownership is single-module (`agent-engineering` specs + `pbatch/org.py`). Dependency direction: `org.py` already imports upward from `cli._SUBCOMMANDS` (deferred import) and `.impact` — a pre-existing layering quirk, unchanged by this design.

## 2. Findings (sorted by severity)

| # | Sev | Finding | Evidence | Impact | Recommendation |
|---|-----|---------|----------|--------|----------------|
| F1 | **High** | **The design as specified cannot satisfy AC4**: `pbatch/org.py` is **488 lines**; D1+R1+R2 add ≈ 17–22 lines → ~505–510, exceeding the **500-line filesize gate** (`checks/config.py:42`, `engineering.yaml` `filesize.max_lines: 500`; org.py is *not* in the ignore list, unlike pipeline.py/runner.py/cli.py). `make check`/`make ci` runs `python cli.py check` (Makefile:13–14, 47). | Verified: `wc -l pbatch/org.py` = 488; `python checks/filesize.py pbatch/org.py` passes today at 488. | AC4 ("`make ci` stays green") fails on landing; the direction's own acceptance is unreachable as designed. | Either (a) extract the placeholder contract + scanner into a small `pbatch/placeholders.py` (single source of truth imported by both static and runtime paths — cleaner layering, org.py stays under budget, scanner unit-testable), or (b) fold the zero-risk removal of the 13-line dead `_summarize` duplicate (org.py:198–210) into the same commit (net ~+4 → ~492). Option (a) preferred; (b) if the scope guard is held strictly. |
| F2 | Medium | **Runtime layer is not fail-closed**: R2 marks unsupported tokens `skipped`, but `gate_passed = failed == 0` (org.py:193) — skipped never blocks. `org gate`/`org run` executed without a prior `org check` (common in 7×24 automation) still green-lights a config defect; only the static CI layer fails closed. The requirements frame this as a "trust-boundary defect in a fail-closed system," yet the fix leaves one of the two layers open (their own F1 "stale-config skip"). | org.py:193 `"gate_passed": failed == 0`; design R2 "skipped with pinned reason"; AC3 wording "skipped with reason **or** failed". | Automation running `org gate`/`org run` on stale/forked yaml gets exit 0 + `passed` counts with a visible-but-non-blocking skip. | Treat unsupported tokens as **`failed` with reason** (config defect, unlike 缺参 which is a caller omission) → `gate_passed=False`. 2-line change, still satisfies AC3's "or failed" branch. Keep 缺参 as skipped. |
| F3 | Medium | **Evidence/artifact integrity**: (a) design artifact is 17 lines while the deliverable claims 425 — implementers reading the durable file get only a summary; the memory index hashes the 17-line file; (b) `adversarial_review` passed with an **empty** artifact dir, so `design_gate` aggregates no cross-review evidence. | Verified: file lengths; run dir contains only pipeline.yaml, DECISIONS.md, requirements.md, task-1-design.md; meta/ dir empty. | Later stages (`implement`, `design_gate`) and any audit rely on the evidence stream; risk of drifted claims (like the 425-line citation) propagating. | Repair artifact emission in the campaign executor (write full design body to the artifact path; fail or warn on empty meta stage output). Re-run design_gate after this review. |
| F4 | Low | **Over-blocking false positives** for legitimate shell braces: scanner `\{[A-Za-z_][A-Za-z0-9_]*\}` matches e.g. `awk '{print}'`, `sed 's/x/{y}/g'` in future auto_checks. Today zero false positives (verified: 38/38 tokens are real placeholders). | Verified inventory; regex semantics. | Future valid auto_check gets red CI with a confusing "占位符不支持" message. | Acceptable fail-closed behavior; document an escape hatch (e.g., quoted `\{}` or allowlist comment) in the contract doc; pin scanner behavior with unit tests. |
| F5 | Low | **`_summarize` dead code** (13 lines after `return`, org.py:198–210) sits in the very function R2 touches; requirements scoped it out ("context only"). | Verified: unreachable duplicate block. | Confuses readers; blocks F1 option (b); sibling campaign direction already flags it. | Remove in the same commit if F1 option (b); otherwise leave as explicitly tracked debt. |
| F6 | Low | **Portability contract change**: toolset is designed to be copied into other projects (AGENTS.md §1); the new hard rule "only `{task}`/`{code}`/`{file}`" applies to any third-party `agent-engineering/*.yaml`. | AGENTS.md portability section; design R1 semantics. | External yamls with custom tokens get red `org check` after upgrade. | Desirable (fail closed), but document the contract change at `AGENT_ORGANIZATION.md:234` (the placeholder contract line) in this change. |
| F7 | Info | **Sibling directions collide in the same file**: analysis #2 ("org run/flow not fail-closed… `_summarize` unreachable duplicate") and #3 (role-spec drift, `render_role_prompt`) both touch `org.py`; parallel campaign worktrees will conflict. | `docs/auto/analyses/agent-engineering-36bd39dc.json` (3 directions). | Merge pain / partial-landing CI break. | Land this direction first, then sequence #2; or merge serially with re-run of `make ci`. |
| F8 | Info | `run_gate` substitutes user-supplied values into `shell=True` commands (pre-existing; `org gate --task …`). Out of this direction's scope. | org.py:149, 164–171. | Operator-controlled local CLI — bounded. | Note for a future hardening direction; no action now. |

**Required fixes:** F1, F2, F3(a)/(b) as pipeline-process repair. **Optional:** F4, F5, F6. **Explicit non-goals (endorsed):** new `{log}` parameter threading, `retro` semantics, `_summarize` refactor, sibling directions.

## 3. Decision options

| Option | Description | Trade-offs | Verdict |
|---|---|---|---|
| **O1 (design as-is)** | Constant + scanner inline in `org.py`, R1/R2/R3, same commit | Smallest diff, minimal blast radius; but **exceeds the 500-line budget → AC4 red** unless paired with dead-code removal; constant lives in only one module (fine today) | Reject as-is; accept only with F1 mitigation |
| **O2 (preferred)** | New `pbatch/placeholders.py`: `SUPPORTED_PLACEHOLDERS` + `scan_tokens()` + anti-drift assert; R1/R2 import it; R2 unsupported → `failed`; R3 same commit; doc contract update | +1 module dep (org.py deps still ≤15, `capabilities check` fine); cleanest single-source-of-truth; org.py stays under budget with margin; scanner unit-testable in isolation; slight scope growth (~40 new lines) | **Preferred** — smallest viable option that satisfies all four ACs and closes both layers fail-closed |
| O3 | Runtime-only fix (R2 without R1) | Smallest possible; but CI stays blind to new tokens — the trust boundary remains open | Reject (defeats the direction's premise) |
| O4 | Config-driven placeholder registry / new subcommand | Extensible, but over-engineering for one constant in a frozen-kernel repo (docs/ARCHITECTURE.md restraint principle) | Reject |

## 4. Prioritized implementation sequence

**M0 — tests first (red):** add fixture-based tests to `tests/test_org.py` modeled on `test_org_check_detects_bad_command` (tmp_path + monkeypatch ORG_DIR): T1a `org_check()` on fixture with `pi-batch retro --actions {log}` → `valid=False` with problem containing `占位符不支持` and `{log}`; T1b shipped-tree `org_check()` → `valid=True` (post-R3); T2 `run_gate()` never marks the `{log}` check `passed` and `gate_passed=False`; plus a guard test that 缺参 still skips for `{task}` with empty task. *(Design note: the CLI-level exit-1 test needs a fixture ORG_DIR via monkeypatch — there is no `--org-dir` CLI flag, and adding one would be scope creep; function-level + `_cmd_check` SystemExit assertions are the right size.)*

**M1 — static layer + self-healing (one commit):** D1 constant + scanner; R1 in `org_check`'s auto_checks loop (additive, no `_check_exists` rewrite — correct choice: keeps "命令不存在" and "占位符不支持" diagnostics distinct); R3 `evolution.yaml:31` → `pi-batch retro --actions {code}` in the **same commit** (AC4 coupling — Makefile:47–50, selfcheck.py:38). Verify: `python pi-batch.py org check` exit 0; fixture exit 1.

**M2 — runtime layer:** R2 branch in `run_gate` **before** the 缺参 guard (order matters — `{log}` must never reach the shell); per F2, status `failed` with reason `不支持的占位符 {log}` (fallback: skip-with-reason if owner overrules); anti-drift assert `params.keys() == SUPPORTED_PLACEHOLDERS` in `run_gate`. Verify byte-identical behavior for supported placeholders (缺参 guard still fires for `{task}`).

**M3 — documentation + full gates:** update the placeholder contract at `AGENT_ORGANIZATION.md:234` (supported set + fail-closed behavior + escape hatch for shell braces); portability note in AGENTS.md if warranted.

**Compatibility plan:** no interface changes; shipped tree valid before/after; rollback = single-commit revert (R1's new failure only triggers on unsupported tokens, so reverting R3 alone is safe); migration cost ~one commit, ordered verify steps per design F1–F7 mitigations (endorsed).

**Risks:** budget collision (F1 — mitigated by O2); partial landing breaks `make ci` (same-commit rule); over-blocking (F4 — documented escape); sibling-direction conflicts (F7 — sequence).

**Executable acceptance checks:**
1. `python pi-batch.py org check && echo $?` → 0 on shipped tree; fixture node with `{log}` → exit 1 with `占位符不支持: {log}`.
2. `python -m pytest tests/test_org.py -q` — new T1a/T1b/T2 pass; existing 20+ tests green.
3. `run_gate` on fixture: `{log}` check never `passed`; `gate_passed=False`; `{task}` with empty task still `skipped 缺少参数`; `{code}` with a dir executes unchanged.
4. `python checks/filesize.py pbatch/org.py` → pass (≤500) — **the gate F1 would otherwise break**.
5. `make ci` green + `python quality.py --strict .` green (AGENTS.md §4).
6. `grep -rn '{log}' agent-engineering/` → zero hits after R3.

## 5. Unknowns needing owner/product decisions

1. **Runtime semantics for unsupported tokens** — `skipped` (design, per AC3's permissive wording) vs `failed` (my recommendation, true fail-closed at runtime). Owner sign-off required; affects automation behavior on stale yaml.
2. **Budget resolution** — extract `pbatch/placeholders.py` (O2) vs fold the dead `_summarize` removal into scope (F1 option b). Tension with the explicit scope guard.
3. **R3 semantic shift** — evolution node's auto_check changes from "scan a `{log}` file" to "scan the `{code}` dir recursively for `*.log`". Confirm that matches the evolution node's intent (and whether `--failures-only` should be added); the direction explicitly excludes new `{log}` threading, so this semantic approximation needs explicit acceptance.
4. **Escape hatch policy** — document-only vs regex refinement for future legitimate shell-brace tokens.
5. **Doc contract update** — include the `AGENT_ORGANIZATION.md:234` amendment in this change or a follow-up (cheap; not required by AC1–AC4).
6. **Pipeline process repair** — who owns fixing artifact emission (17-line design artifact; empty adversarial_review dir) before the `design_gate`/`implement` stages run, and whether design_gate must be re-run after this review lands.

**Bottom line:** the direction is sound, the bug is real, and R1/R3 as designed are correct and appropriately scoped. The design must be revised for F1 (500-line budget — AC4 is unachievable as written) and F2 (runtime layer should fail, not skip); F3 is a pipeline-process defect that should be fixed before `implement` consumes the evidence. With O2 + the F2 change, this is a clean, low-risk, single-commit landing.
