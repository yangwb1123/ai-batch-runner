# Requirements Specification: Reuse config-driven failure signatures in `ai/run-review.py`

- **Module**: `ai`
- **Direction ID**: `reuse-config-driven-failure-signatures-instead-o-e01b9b60`
- **Source analysis**: `docs/auto/analyses/ai-1f0b642b.json`
- **Status**: Proposed (AI-generated; must be verified against evidence before implementation)

## 1. Summary

`ai/run-review.py` maintains its own frozen copy of the agent failure-signature
table (`_AGENT_ERROR_PATTERNS`, 39 regexes) and a private
`agent_failure_reason()` that consumes it. The canonical, config-driven
implementation already exists in `pbatch/config.py::agent_error_patterns()`
(per-agent override > global override > built-in defaults) and is used by
`pbatch/runner.py::agent_failure_reason()`. The review runner ignores that
machinery entirely: every signature added or fixed for a new agent adapter
(claude/codex/…) must be edited twice, and until it is, provider-error replies
are silently saved as `stage-NN.out.md` artifacts in review runs.

This spec requires eliminating the local copy and delegating all failure
signature matching to the config-driven registry, with the effective agent
binary name propagated so per-agent overrides (`agent.agents.<bin>.error_patterns`)
take effect — the same mechanism `pbatch/cli.py` already uses for `--agent-bin`.

## 2. Verified evidence

Every citation below was checked against the current repository.

| # | Evidence | Verification |
|---|---|---|
| E1 | `ai/run-review.py:405-448` — `_AGENT_ERROR_PATTERNS = (...)` contains exactly **39 hard-coded regexes** (12 provider-error codes, 27 network/connectivity, 2 CLI banners) | Confirmed by source read and programmatic count (`re.compile` count in the table = 39; these are the only 39 `re.compile` occurrences in the file) |
| E2 | `ai/run-review.py:451-463` — private `agent_failure_reason()` iterates `_AGENT_ERROR_PATTERNS`; returns reason string or `""` | Confirmed |
| E3 | `ai/run-review.py:533` — `_invoke_review_agent()` calls the local `agent_failure_reason(rc, text)`; on a reason it prints `Stage {stage} REJECTED: …; output NOT saved to {out_file}` and returns `None`, so `run_stage` returns 1 and no artifact is written | Confirmed (lines 533-541) |
| E4 | `pbatch/runner.py:89-102` — canonical `agent_failure_reason()`; line 100: `for pattern in config.agent_error_patterns():` — **config-driven, zero local regexes** | Confirmed |
| E5 | `pbatch/config.py:206-227` — `agent_error_patterns(bin_name="")`: precedence `agent.agents.<bin>.error_patterns` > `agent.error_patterns` > built-in `_DEFAULT_ERROR_PATTERNS`; reads `config.AGENT_BIN` at call time (line 211) so a runtime `--agent-bin` override is honored; explicit empty list disables rejection; non-list config warns and falls back to defaults | Confirmed |
| E6 | Built-in defaults are currently **identical** to the review-runner copy: 39 patterns in `pbatch/config.py:150-204` vs 39 in `ai/run-review.py:405-448` — identical today, drifting by construction (two independent copies) | Confirmed by programmatic count (39 = 39) |
| E7 | `pbatch/config.py:145-149` — `_agent_section(bin_name)` exact-match lookup into `agent.agents`; absent section returns `{}` (falls through to global/default) | Confirmed |
| E8 | `pbatch/cli.py:363` and `pbatch/campaign_exec.py:229` — `config.AGENT_BIN = args.agent_bin` set at runtime; `agent_error_patterns()` then resolves per-agent overrides without passing `bin_name`. This is the established in-repo pattern for honoring `--agent-bin` | Confirmed |
| E9 | `ai/run-review.py:485` (`cmd = [args.agent_bin or AGENT_BIN, …]`), `:561` (`agent_bin = args.agent_bin or AGENT_BIN`), `:695` (`--agent-bin` CLI arg), `:216` (`AGENT_BIN = _load_agent_bin()` from `pi-batch.yaml agent.bin`) — the review runner already computes the effective agent binary and threads it through `_invoke_review_agent(stage, args, out_file, cmd, agent_bin, timeout)` | Confirmed |
| E10 | `ai/run-review.py:55-62` — already imports `from pbatch import config as batch_config` and `from pbatch.runner import …` with `sys.path` set; delegation requires **no new dependency and no new import path** | Confirmed |
| E11 | `pi-batch.yaml:25` — commented global knob `# error_patterns: …` (G line doc); `pi-batch.yaml:35` — commented per-agent knob `# agents: claude: … error_patterns: …` — both documented but honored only by `pbatch`, never by the review runner today | Confirmed |
| E12 | `tests/test_run_review.py` exists; rejection behavior is already covered by `test_run_stage_rejects_provider_error_output` (:117), `test_run_stage_rejects_quota_output` (:129), `test_run_stage_rejects_offline_output` (:151), `test_run_stage_rejects_empty_output` (:140), `test_run_stage_rejects_timeout` (:168) — all using `load_runner()` + fake agent shell scripts + `_Args(agent_bin=…)` | Confirmed |
| E13 | Config discovery: `pbatch/config.py:27-41` — `PBATCH_SCRIPT_DIR` env (set by the pi-batch.py shim) > package dir > cwd; a test can isolate an override via monkeypatching `batch_config._AGENTS_CFG` (module-level, `config.py:141-143`) or a temp `pi-batch.yaml` via `PBATCH_SCRIPT_DIR` | Confirmed |
| E14 | Precedent for eliminating drifting duplicates in this repo: `scripts/check-no-refusal.py` was promoted/eliminated (commit `1d4e78a` "feat(lock): --wait-lock queueing + promote check-no-refusal + lessons doc") | Confirmed in git history |

**Drift consequence (E1/E5/E6)**: any pattern added to `pbatch/config.py` for a
new provider/adapter (e.g. `"anthropic.*rate limit"`, `"context window
exceeded"` for claude) does **not** apply to `ai/run-review.py --agent-bin
claude`. A provider-error reply that `pi-batch.py` would reject (exit-0 + banner)
is saved as `stage-NN.out.md` in review runs and can be chained into later
stages as if it were real analysis.

## 3. Goals

- **G1** — Remove the frozen duplicate: `ai/run-review.py` holds **zero local
  copies** of the failure-signature table and zero local pattern-matching logic.
- **G2** — Delegate all signature matching to `pbatch/config.py::agent_error_patterns()`
  so a signature exists in exactly one place (the config registry).
- **G3** — Honor config overrides in review runs: global `agent.error_patterns`
  and per-agent `agent.agents.<bin>.error_patterns` (including the empty-list
  disable knob), keyed by the effective agent binary (`--agent-bin` or
  `agent.bin`).
- **G4** — Preserve existing rejection semantics exactly: non-zero exit, empty
  output, and signature hits reject the stage with the same messages and
  **never** write the output file (fail closed).

## 4. Non-goals (out of scope — do not expand)

- No changes to `pbatch/config.py` semantics, `pbatch/runner.py`, or the
  default pattern set content (the 39 defaults stay as-is).
- No rework of stage/session/validation machinery in `ai/run-review.py`.
- No unification of `_load_agent_bin()` (`ai/run-review.py:201-216`) with
  `pbatch/config.py` agent-bin resolution — only the *signature* path is in
  scope.
- No changes to `pi-batch.yaml` defaults; the commented knobs remain commented
  documentation.

## 5. Functional requirements

### REQ-1 — Delete the local table and matcher (G1, G2)
`ai/run-review.py` must no longer define `_AGENT_ERROR_PATTERNS` (lines
405-448) nor the local `agent_failure_reason()` (lines 451-463). The file must
contain no failure-signature regex table and no `re.compile` of provider/CLI
signatures. If `import re` becomes unused, remove it.

**Verifiable**: `grep -n "_AGENT_ERROR_PATTERNS" ai/run-review.py` returns
nothing; `grep -c "re\.compile" ai/run-review.py` returns 0.

### REQ-2 — Delegate to the config registry (G2)
Stage-result rejection in `_invoke_review_agent()` (`ai/run-review.py:533`)
must call `batch_config.agent_error_patterns(<effective_bin>)` (or a thin
wrapper) instead of the deleted local matcher. The non-signature checks
(returncode ≠ 0 → `"agent exited {rc}"`; empty/blank output → `"agent produced
no output"`) are kept verbatim, matching `pbatch/runner.py:89-102` semantics.

**Verifiable**: code review — the only signature source referenced by
`ai/run-review.py` is `pbatch/config.py::agent_error_patterns`.

### REQ-3 — Propagate the effective agent binary (G3)
The per-agent name passed to `agent_error_patterns()` must be the same
`args.agent_bin or AGENT_BIN` value already computed at `ai/run-review.py:561`
and used to build the command at `:485`. Two acceptable implementations:

1. pass `agent_bin` explicitly: `batch_config.agent_error_patterns(agent_bin)`
   (the `bin_name` parameter exists precisely for this, E5); or
2. mirror `pbatch/cli.py:363`: set `batch_config.AGENT_BIN = agent_bin` before
   calling `batch_config.agent_error_patterns()`.

Both honor `agent.agents.<bin>.error_patterns` (exact name match, E7), global
`agent.error_patterns`, built-in defaults, and empty-list disable — in that
precedence, unchanged from `pbatch` behavior.

**Verifiable**: with `agent.agents.custom.error_patterns` configured, a run
using `--agent-bin custom` matches the per-agent patterns; with no per-agent
section for the bin name, global override applies; with neither, the built-in
39 apply.

### REQ-4 — Preserve rejection behavior and messages (G4)
The `REJECTED … output NOT saved` path and return codes (`run_stage` → 1,
`_invoke_review_agent` → `None`) must be byte-identical in observable behavior
to today: signature hit prints the same `Stage {stage} REJECTED: agent reported
provider failure ({pattern})` line and writes no `stage-NN.out.md`.

**Verifiable**: all existing rejection tests in `tests/test_run_review.py`
(`test_run_stage_rejects_provider_error_output`, `…_quota_…`, `…_offline_…`,
`…_empty_…`, `…_timeout_…`) pass unchanged; plus `make ci` green.

### REQ-5 — Backward compatibility without config (G2, G4)
With no `pi-batch.yaml` overrides (fresh checkout / no config), the effective
pattern set is the built-in 39 defaults — behavior identical to today,
including the anti-false-positive property (generic words like `error`/`timeout`
never match; review prose about timeouts/unauthorized responses is not
misclassified).

**Verifiable**: `test_run_stage_saves_legitimate_mention_of_error_words`
(`tests/test_run_review.py:193`) passes unchanged.

### REQ-6 — Regression tests (G3)
Add a case to `tests/test_run_review.py` proving per-agent `error_patterns`
override is honored end-to-end:

- inject `agent.agents.<name>.error_patterns = ["<custom banner>"]` into the
  config the module under test sees (isolated: monkeypatch
  `batch_config._AGENTS_CFG`, or a temp `pi-batch.yaml` selected via
  `PBATCH_SCRIPT_DIR`, E13 — never the real repo file);
- run `run_stage` with `_Args(agent_bin=<name>, …)` and a fake agent script
  that exits 0 printing only `<custom banner>` (a string absent from the
  built-in 39, so only the override can reject it);
- assert `rc == 1` and `stage-00.out.md` does not exist;
- positive control: the same banner with **no** override (or a different bin
  name) must **not** reject — proving the override, not the defaults, did the
  work.

**Verifiable**: new test passes; full `tests/test_run_review.py` passes.

## 6. Acceptance criteria (preserved from the direction, made testable)

1. **Per-agent override honored** — `agent.agents.<custom>.error_patterns`
   configured; run `python ai/run-review.py --agent-bin custom`; a matching
   provider banner must reject the stage with the existing `REJECTED …
   output NOT saved` line and no `stage-NN.out.md` file written.
   → Automated by REQ-6; manual check is the same scenario against the real
   `pi-batch.yaml`.
2. **Zero local copies** — grep the pattern table out of `ai/run-review.py`
   and `pbatch/config.py` and diff: tables must be identical **or** the runner
   must delegate with zero local copies. This spec mandates the second branch:
   the review runner must contain no table at all (REQ-1/REQ-2), making drift
   structurally impossible.
   → Automated by REQ-1 verifiable checks.
3. **Regression suite** — `tests/test_run_review.py` passes with the new
   per-agent override case (REQ-6) and all pre-existing rejection tests
   unchanged (REQ-4).

## 7. Implementation guidance (non-normative)

- The diff is small and localized to `ai/run-review.py:405-463` + `:533`:
  delete the table and matcher, replace the `agent_failure_reason(rc, text)`
  call with an equivalent that uses `batch_config.agent_error_patterns(agent_bin)`
  (option 1 of REQ-3 keeps the existing `agent_bin` parameter flow, E9).
- Keep the returncode/empty-output checks local (they match
  `pbatch/runner.py:89-102` and are not config-driven).
- `pbatch/runner.py::agent_failure_reason` is *not* imported by
  `ai/run-review.py` today and is not required; delegating to
  `batch_config.agent_error_patterns()` is sufficient and keeps the review
  runner's distinct output-size/timeout handling untouched.
- After the change run: `python -m pytest tests/test_run_review.py` and
  `make ci`.

## 8. Risks and constraints

- **Risk: per-agent name mismatch** — `_agent_section()` does an exact name
  match (E7). `--agent-bin` must be passed through unmodified (REQ-3); a
  path-like or versioned bin name will simply not match a section, falling
  back to global/defaults — same behavior as `pi-batch.py`, so no new hazard.
- **Risk: test isolation** — `pbatch.config` is imported once; the new test
  must restore any monkeypatched `_AGENTS_CFG`/`AGENT_BIN` state so the other
  20+ tests in the file are unaffected.
- **Constraint** — no change to the 39 default patterns; any behavioral
  difference in rejection must come from config overrides, never from an
  accidental regex edit during the move.
- **Precedent** — this is the second drift-elimination of its kind in the repo
  (E14, check-no-refusal); same review bar applies.

## 9. Definition of done

- [ ] REQ-1..REQ-6 implemented; zero local signature table in `ai/run-review.py`
- [ ] New per-agent override test added and passing
- [ ] All pre-existing `tests/test_run_review.py` tests pass unchanged
- [ ] `make ci` green
- [ ] Manual acceptance check 1 executed against a real config override
