All verification complete. I have independently confirmed every material claim against live code, configs, and artifacts. Here is my review.

---

# Architecture Constitution Review — Gate Placeholder Validation (`{log}` false-pass)

**Scope reviewed:** design artifact `docs/auto/runs/gate-placeholder-validation-unsupported-log-plac-3b7b1b9f/artifacts/design-a77de8a6/task-1-design.md`, requirements `docs/auto/requirements/agent-engineering-36bd39dc-requirements.md` (332 lines), and live code: `pbatch/org.py` (org_check/_check_exists/run_gate/_execute_check/_summarize), `pbatch/retro.py`, `agent-engineering/evolution.yaml`, `Makefile`, `pbatch/selfcheck.py`, `docs/AGENT_ORGANIZATION.md`.

**Checks actually run (this review):** `python pi-batch.py org check --json` (valid: true, 17 nodes); shell execution of `pi-batch retro --actions {log}` (exit 0, "1 file(s)", zero evidence — silent false-pass reproduced); `pi-batch retro --actions /nonexistent-dir-xyz` (exit 0 — retro never fails on missing input); placeholder inventory scan of all `agent-engineering/*.yaml` (38/38 tokens: `{task}`×22, `{code}`×12, `{file}`×3, `{log}`×1 — exactly as claimed); `wc -l` of the design artifact (17 lines, not 425); `_summarize` duplicate body at org.py:194–202 (dead code confirmed); selfcheck.py:38 + Makefile:47–50 coupling confirmed.

## 1. Change-Amplification Map

Business change: *"the evolution node gate must not silently false-pass an unsupported placeholder."*

| Layer | Touch points | Count |
|---|---|---|
| `pbatch/org.py` | D1 constant + R1 scanner in org_check loop + R2 skip branch + anti-drift assert | 1 file, 4 sites |
| `agent-engineering/evolution.yaml` | line 31: `{log}` → `{code}` (R3, mandatory) | 1 line |
| `tests/test_org.py` | +T1, +T2 (T3 = existing, must keep passing) | +2 tests |
| Tables / services / APIs / deploy units | none | 0 |
| CI gates behaviorally affected (no edits) | `make ci` org target (Makefile:47–50), `pi-batch check` (selfcheck.py:38) | 2 |

**Verdict on amplification:** excellent — 3 files, one deploy unit, zero data/services. The change is fully encapsulated in the module that owns the contract. **However:** R1 and R3 are *deploy-coupled* — R1 alone breaks `make ci` (scanner flags `{log}` in shipped evolution.yaml); R3 alone leaves the bug. The design correctly pins this to one atomic commit (AC4). This is a temporary coupling, acceptable if landed atomically.

## 2. Findings Table

| # | Sev | Constitution § | Evidence | Impact | Recommendation |
|---|---|---|---|---|---|
| F1 | **High** | prompts/README evidence standard; §7/§8 | Design artifact is **17 lines** (2,212 B) but asserts it is "complete … (425 lines)" containing an F1–F7 failure-mode table, migration steps, and T1a/T1b/T2/T3 acceptance mapping. **None of that exists on disk** — `find docs/auto -name "*design*"` shows only this file; the requirements doc carries T1/T2/T3, not T1a/T1b/T2/T3. The design-stage prompt required API changes, compatibility constraints, failure modes, migration steps, acceptance mapping — 3 of 5 are absent in body | The implementation stage receives a summary whose referenced mitigations (over-blocking, partial-landing CI break, shell collisions) and migration/rollback order are unverifiable claims; gate cannot check the design it is asked to pass; implementation cannot be validated against non-existent pinned assertions | **Blocking:** regenerate the design body at the artifact path (failure modes table, ordered migration+rollback, acceptance mapping) before implementation; or explicitly demote the summary to design and carry F1–F7/migration into the implementation prompt. Validation: `wc -l` > 100, `grep -c "F[0-9]"` ≥ 7, `grep T1a` present |
| F2 | Medium | §13 (entropy/局部最优), §16 | R1 regex `\{[A-Za-z_][A-Za-z0-9_]*\}` is a substring scan over the whole shell command: **zero collisions today** (verified 38/38 tokens), but `${VAR}`, `awk '{print $1}'` would false-positive if ever added to auto_checks → CI over-blocking (the design's own F-table claims this risk but documents no mitigation on disk) | Future auto_check with shell braces breaks `make ci` with a misleading "unsupported placeholder" problem | Pin negative tests (`awk '{print $1}'`, `${VAR}`, `find … {}` must NOT be flagged); optionally exclude `$`-prefixed braces or require whitespace-delimited tokens; comment the caveat on the constant |
| F3 | Medium | §6 coupling octet (temporal/deployment) | R1↔R3 atomicity: `make ci` (Makefile:47–50) and `pi-batch check` (selfcheck.py:38) run `org check`; evolution.yaml:31 still holds `{log}` until R3 | Partial landing breaks CI or leaves the bug; two independent-looking edits are temporally coupled | Land in one commit with ordered verify (R3→R1→`make ci`), as designed; add a one-line note in the commit body |
| F4 | Low | §8 (failure honesty), §14 | `retro_main` returns 0 unconditionally (retro.py:244–291) — verified: nonexistent path → exit 0, empty evidence. Post-R3, `retro --actions {code}` on a log-less dir still exits 0: the evolution gate becomes honest but vacuous | Gate signal weakness persists (out of scope per requirements, but adjacent root cause) | Follow-up: retro exits non-zero (or prints `WARNING: 0 log files scanned`) when nothing was scanned — closes the same silent-pass class for all future `{x}` typos |
| F5 | Low | §13 (entropy) | `_summarize` duplicate unreachable body (org.py:194–202) — pre-existing, explicitly out of scope | Dead code in the very file being touched | Optional: delete the duplicate in the same change (9 lines, zero risk) |
| F6 | Low | §16 | Anti-drift `assert` in run_gate is disabled under `python -O`; assert-only drift detection is weaker than a test | Drift between constant and params could go undetected in -O deployments | Prefer a unit test asserting `params.keys() == SUPPORTED_PLACEHOLDERS` (T-suite addition) over the assert |
| F7 | Info | §9 | AC3 skip semantics: `gate_passed = failed == 0`, so a skipped check still passes the node gate — consistent with existing missing-param semantics and AC3 ("skipped with reason or failed"); static R1 is the authoritative fail-closed layer | A hand-edited/stale config silently skips at runtime (visible in results JSON + printed counts) | Accept; document that R1 is the enforcement point, runtime skip is the honest fallback |

## 3. Hard-to-Reverse Decision Register

| Decision | Reversibility | Migration cost | Blast radius | Evidence |
|---|---|---|---|---|
| `SUPPORTED_PLACEHOLDERS` shared constant (D1) | **High** — internal module constant | Low | Local | org.py; no external consumers |
| R1 static gate in `org check` (CI behavior change) | **High** — revert = delete scanner; but while live it becomes a contributor contract | Low | Module→repo-wide CI | Makefile:47–50, selfcheck.py:38 |
| R2 runtime skip branch | **High** | Low | Local | run_gate; behavior pinned by AC3 test |
| R3 `evolution.yaml:31` one-line change | **High** | Low | Local | verified retro accepts dirs (rglob, retro.py:95/263) |
| Regex grammar as de-facto token contract | **High** | Low | Module (CI) | F2 — needs negative pinning |

**No PKs, no public API, no event formats, no service boundaries, no tenant model** — the change contains zero hard-to-reverse decisions. This is textbook constitutional practice (§2: delay commitment, keep reversible).

## 4. Constitution's 15 Questions (concise)

1. **Why this design?** Root cause = two independent blind layers (token-blind `_check_exists` org.py:57–67; `run_gate` params only for supported keys, org.py:149–166); a single source of truth + two enforcement layers kills the defect class.
2. **Most likely to change?** The placeholder set; future auto_checks' shell syntax; the token grammar.
3. **Hardest decision to reverse?** None (see register) — R1's CI semantics is the closest, still a revert.
4. **Who owns this data?** `agent-engineering/*.yaml` → org.py loaders; the token contract → the new constant.
5. **Who owns the module?** Single tooling module (pbatch/org.py), single repo.
6. **A dependency fails, who's affected?** Per-node gate isolation: only that node's gate; bounded by 120 s subprocess timeout, 200-char evidence capture.
7. **Max blast radius?** CI failure on the whole repo (make ci / pi-batch check) — no production data or runtime service impact.
8. **At capacity?** Subprocess timeout (120 s) is the hard bound; no queues/connection pools involved.
9. **Silent bad data?** The defect being fixed *was* silent false-pass (verified: exit 0, zero evidence). Post-fix: static layer fails closed; residual = F4 (retro vacuous pass) and F7 (skip-passes node).
10. **Old/new coexist?** Single CLI, atomic commit; no dual-version surface.
11. **Data migration?** One YAML line; no data.
12. **Future deletion?** Revert the commit; scanner is additive.
13. **Who sees problems?** `org check` problem lines + exit 1 in CI logs; gate skipped reasons in results JSON.
14. **Manual recovery?** Edit YAML to a supported token or extend the constant; re-run gate — no DBA-style intervention needed.
15. **Team can maintain? Yes — one constant, one regex, two small branches; no hidden magic post-fix (the hidden magic — shell brace expansion of `{log}` — is precisely what R1 makes visible). Simpler alternative? Only R3 (fix one line) would be simpler but leaves both blind layers for the next token — the constant+scanner is the justified anti-recurrence cost.**

## 5. Constitution Checklist Results

1. Change amplification: **excellent** (3 files / 0 tables / 0 services), change encapsulated ✓
2. Reversibility: **all high** ✓
3. Organization fit: low cognitive load; the shell-brace-expansion gotcha (root cause) becomes visible via R1 ✓
4. Modular monolith vs microservices: N/A — single package; **no distributed-monolith risk** ✓
5. Data ownership: config owned by org.py; the derived placeholder inventory has a rebuild mechanism (scanner + assert), not just docs ✓
6. Coupling octet: code ✓ / data N/A / temporal+deployment = R1↔R3 atomicity (F3, managed) / order N/A / semantic ✓ (`{code}` → retro dir expectation matches) / organizational N/A / runtime ✓ (bounded) 
7. Blast radius: CI-level only; per-node bulkheads, bounded subprocesses; no retry storms (retro is read-only) ✓
8. Consistency model: N/A (no distributed data) — correctly untouched
9. Contract evolution: placeholder set formalized; new tokens become a deliberate 3-touch act (constant+params+tests), removal immediately flagged by scanner ✓
10. Evolutionary architecture: minimal, YAGNI-compliant; no speculative scale; assumptions recorded ✓

---

**VERDICT: FAIL - design artifact evidence integrity: the artifact (17 lines) claims a 425-line design with F1–F7 failure-mode table, migration steps, and T1a/T1b/T2/T3 acceptance mapping that exist nowhere on disk — 3 of 5 required design deliverables (failure modes, migration, acceptance mapping) are absent in body, so the implementation cannot be validated against pinned mitigations and the gate cannot review the risk analysis it is asked to pass; regenerating the design body is required. The architecture decisions themselves (D1/R1/R2/R3) pass the constitution: minimal blast radius, fully reversible, explicit contract, fitness-function enforcement — the failure is remediable without redesign.**
