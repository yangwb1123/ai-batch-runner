# Architecture Review — Reuse config-driven failure signatures in `run-review.py`

*Role: senior architect, advisory only. Checks that ran for this revision: full reads of `ai/run-review.py:395–575`, `pbatch/config.py:1–80,137–230`, `pbatch/runner.py:75–105`, `pbatch/cli.py:355–370`, `pi-batch.yaml:18–45`, `tests/test_run_review.py`, `tests/test_local_fixes.py:1220–1270`; executed pattern-equivalence probe (39 vs 39, 15 probe strings); pytest baseline of the two affected files; `git log` for precedent `1d4e78a`.*

**Note on sources:** the role prompt's canonical `docs/architecture/DIRECTORY_MAP.md` does not exist in this tree (standalone adaptation). Nearest canonical refs: `docs/ARCHITECTURE.md` (layering/kernel contract) and `docs/agent-context/context-map.yaml` (own-repo routing). Reported below as Info; I did not silently substitute.

## 1. Scope, assumptions, verified architecture summary

**Scope.** Replace the frozen 39-regex duplicate table `_AGENT_ERROR_PATTERNS` (`ai/run-review.py:405–448`) and its private matcher `agent_failure_reason()` (`:451–463`) with delegation to the canonical config-driven registry `pbatch/config.py:206 agent_error_patterns(bin_name="")`. No config schema, CLI, or artifact changes; no default-pattern changes; no review-runner session/validation machinery changes.

**Assumptions.** This repo is the standalone mirror of upstream `ai-dev` (AGENTS.md adaptation note), so `ai/run-review.py` + `pbatch/` are one tool ecosystem; run-review is a first-class consumer of `pbatch.config` (already imports it at `:58` and uses `batch_config.PROMPT_MAX_BYTES`/`OUTPUT_MAX_BYTES`).

**Verified architecture summary** (all design citations re-checked, **Verified** unless noted):

| Claim | Status |
|---|---|
| 39 = 39 patterns; sets equal; matching order preserved | **Verified** — executed both tables; normalized sets identical; 15 probe strings (incl. multi-line `ERROR:`, `fatal` in prose, case variants) → 0 mismatches |
| Nuance: inline `(?im)` flags in `config.py` vs `re.IGNORECASE|re.MULTILINE` in run-review; `pattern.pattern` renders `(?im)^fatal:` | **Verified** — compiled behavior identical; only the rejection-message rendering differs, and it becomes *identical to what `pbatch/runner.py:100` already prints* (runner uses the same `pattern.pattern`). No test asserts pattern text (`tests/test_run_review.py` asserts rejection, not message) |
| `_invoke_review_agent` already receives `agent_bin` (`:516`), call at `:533`; `run_stage` resolves it at `:561`/`:565` (`args.agent_bin or AGENT_BIN`) | **Verified** |
| `import re` (`:44`) provably unused after deletion | **Verified** — only `re.` usages in file are lines 407–448 |
| `pbatch/runner.py:89–102` canonical matcher delegates to `config.agent_error_patterns()`; identical message format | **Verified** |
| `config.py:206` precedence per-agent > global > defaults; empty list disables (`:225–226`); reads `AGENT_BIN` at call time; `_compile_patterns` lru_cache is content-addressed (key = pattern tuple) → monkeypatch of `_AGENTS_CFG`/`_AGENT_CFG` can't serve stale compilations | **Verified** — `_agent_section` at `:145`; cache keyed on tuple content |
| `pi-batch.yaml:24–36` commented global + per-agent knobs honored only by `pbatch` today | **Verified** |
| `pbatch/cli.py:363` `config.AGENT_BIN = args.agent_bin` as established wiring pattern | **Verified** |
| 25 existing tests untouched by the change | **Verified** — 21 (`tests/test_run_review.py`) + 4 (`TestPerAgentErrorPatterns`, `tests/test_local_fixes.py`); neither file calls run-review's local matcher directly; baseline run: **25 passed** |
| Precedent `1d4e78a` | **Partial** — commit is *promotion* of `check-no-refusal.py` to a shared generic asset, not "elimination" (requirements wording imprecise); the architecture lesson (consolidate duplicated tooling into one shared mechanism) still holds |

**Layering/dependency direction.** The change introduces no new dependency: run-review → `pbatch.config` already exists. It moves run-review from "copied invariant" (a second owner of the failure-signature table, drift-by-construction) to "delegating consumer" of the registry — strictly better ownership, zero new coupling. The registry's read path (`config.py` discovery: `PBATCH_SCRIPT_DIR` → package dir → cwd) is the same path the runner uses; run-review sets `PBATCH_SCRIPT_DIR` to its repo root at import (`:55–56`), so bin and patterns resolve from the same file in normal operation.

## 2. Findings

| # | Severity | Finding | Evidence | Impact | Recommendation |
|---|---|---|---|---|---|
| F1 | **Medium** | Duplicate failure-signature table; config knobs documented but inert for the review runner | `ai/run-review.py:405–448` vs `pbatch/config.py:153–203`; `pi-batch.yaml:24–36` (commented knobs, never read by run-review) | Equal today (39=39) but diverges on any future single-table edit; users configuring `error_patterns` for claude/codex get rejection behavior in `pi-batch.py` but not in the review loop | **Proceed with Option A** (F1 is the defect being fixed; design is sound) |
| F2 | **Medium** | Real behavior change: global `error_patterns: []` now disables review-runner rejection (frozen 39 previously always applied) | `pbatch/config.py:225–226` (`if not patterns: return ()`); run-review `:533` rejection is a safety gate (failure prose never saved as review artifacts) | A permissive config silently downgrades artifact-integrity protection in the review loop | Keep the capability (intended, consistent with runner); must be in the commit message; add one doc line in `ai/README.md`/`RUNNING_247.md`; new test must cover empty-list-disable for `run_stage` (design already includes it) |
| F3 | **Low** | FM1: exact-name mismatch falls back silently; bin resolved by run-review's own `_load_agent_bin()` (`:201–216`, repo-root read) while patterns resolve via config discovery — coincides normally, diverges only under an externally preset `PBATCH_SCRIPT_DIR` | `ai/run-review.py:55–56,201–216`; `pbatch/config.py:26–48` | Same hazard class as `pi-batch.py` today; no new risk; declared non-goal | Accept; optional follow-up: `config.py` warning when `bin_name` matches no `agent.agents.<bin>` section (affects both runners — owner decision) |
| F4 | **Low** | Dead code + unused import persist until change lands; local matcher name will be renamed to private `_agent_failure_reason` | `ai/run-review.py:44,405–463` | ~47 lines of second-owner state | Delete as part of the change; keep the wrapper private |
| F5 | **Info** | Doc drift: role prompt references missing `docs/architecture/DIRECTORY_MAP.md`; requirements' "elimination" vs commit's "promotion" wording | tree scan; `git show 1d4e78a` | Navigation/canonical-reference gap | Owner: fix pointer or add the map; no code impact |
| F6 | **Info** | Test-count reconciliation: "25" = 21 + 4 across two files | `pytest --collect-only` | None — AC3 is meaningful and executable as stated | No action |

No Critical/High findings. No required fixes beyond the change itself.

## 3. Decision options

| Option | Description | Trade-offs | Verdict |
|---|---|---|---|
| **A — explicit `bin_name` pass-through** | Delete local table + matcher + `import re`; add `_agent_failure_reason(rc, output, agent_bin)` wrapper delegating to `batch_config.agent_error_patterns(agent_bin)`; call at `:533` | + Honors `--agent-bin` override correctly (verified threading `:485/:561/:565`); + zero new coupling (import exists); + −47 lines; + behavior provably identical with no config. − FM2/FM1 semantics must be documented | **Preferred** |
| B — mutate `config.AGENT_BIN` then call `agent_error_patterns()` (cli.py:363 pattern) | Process-global mutation from a library consumer | − Implicit ordering coupling: any concurrent/interleaved read of `AGENT_BIN` sees the wrong value; − doesn't cover run-review's own `_load_agent_bin()` divergence; − global state side-effect for a 11-line win | Rejected — agree with design |
| C — import `runner.agent_failure_reason` | Reuse runner's matcher verbatim | − Signature takes no `bin_name` → ignores `--agent-bin` CLI override unless B is also done; − drags runner call-site semantics into review loop | Rejected — agree with design |

Build-vs-buy: N/A — in-repo reuse of the existing registry; no external dependency justified. No extension beyond the refactor is warranted: the only verified gap (F1) is exactly what the design closes.

## 4. Prioritized implementation sequence

**M0 — baseline (pre-change).** `make ci` + `python quality.py --strict .` green; record baseline.

**M1 — refactor `ai/run-review.py`.** Delete `_AGENT_ERROR_PATTERNS` (`:405–448`), local `agent_failure_reason` (`:451–463`), `import re` (`:44`); add `_agent_failure_reason(rc, output, agent_bin)` delegating to `batch_config.agent_error_patterns(agent_bin)`; update call at `:533`. Commit message must state FM2 (global `error_patterns: []` now disables review-runner rejection).

**M2 — tests.** Add `test_run_stage_honors_per_agent_error_patterns_override` to `tests/test_run_review.py` with sub-cases: (a) per-agent override via `_AGENTS_CFG` monkeypatch + a banner string absent from the 39 (positive control), (b) positive control with a *different* bin name (proves `--agent-bin` threading), (c) global override, (d) empty-list disable. Mirror the monkeypatch style of `tests/test_local_fixes.py:1248–1262` (content-addressed cache makes this safe — verified).

**M3 — permanent guard (recommended).** Add `checks/test_run_review_dedup.py` asserting `_AGENT_ERROR_PATTERNS` absent and `re.compile` count = 0 in `ai/run-review.py`; auto-wired via `make ci` (`pytest tests/ checks/`). Precedent: `checks/test_knowledge_freshness.py`.

**M4 — verification.** `python -m pytest tests/test_run_review.py tests/test_local_fixes.py -q` (25 existing + new sub-cases), `python quality.py --strict .`, `make ci`, plus the grep checks below.

**Compatibility plan.** No config-schema, CLI, message-format (proven identical), or artifact changes; no-config behavior byte-identical. Rollback = revert single commit.

**Risks.** FM2 silent disable (mitigated: commit message + docs + test (d)); cache staleness (mitigated by content-addressed key, verified); banner `pattern.pattern` text change in rejection messages (cosmetic, matches runner — no test asserts it).

**Executable acceptance checks.**
```bash
grep -n "_AGENT_ERROR_PATTERNS" ai/run-review.py          # expect exit 1
grep -c "re\.compile" ai/run-review.py                    # expect 0
python -m pytest tests/test_run_review.py tests/test_local_fixes.py -q   # 25 existing + new pass
python quality.py --strict .
make ci
```

## 5. Unknowns needing owner/product decisions

1. **FM2 documentation scope** — confirm the empty-list-disable capability for the review runner should be documented in `ai/README.md`/`RUNNING_247.md`, and whether an explicit log line should fire when signature rejection is disabled (silent downgrade of artifact-integrity protection).
2. **M3 permanent guard** — required (wired into `make ci`) or left as one-off grep AC2?
3. **FM1 mismatch warning** — in scope as a follow-up (config.py-level warning, affects both runners) or deferred?
4. **Doc pointer** — who restores/fixes `docs/architecture/DIRECTORY_MAP.md` referenced by role prompts?

**Bottom line:** the design is verified sound — all citations hold, equivalence is proven by execution, Option A is the right call, and the acceptance mapping is executable as written. Approve for implementation; fold F2 and F3 mitigations into M1/M2.
