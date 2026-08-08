QA review complete — written to `docs/auto/runs/make-uicheck-uigeometry-uiquality-dir-configurab-31146de9/artifacts/adversarial_review-9c87f3a7/meta/qa_lead.md`. This is a pre-implementation design review (adversarial_review stage; `ui_scan_dir`/`expand_cmd` exist nowhere in code yet).

## Summary

**Verdict**: design is evidence-sound, but the test plan needs 1 High + 3 Medium additions before implement.

**1. Measured commands** (this revision): `test_ui_check.py` → 12 passed (all 3 anchors green); `test_validator_registry.py` → 7 passed; `test_pi_batch.py -k validate_cmd` → **1 FAILED** (`test_cli_validate_cmd_gate`, exit 5 — singleton lock held by the campaign itself, environmental); live simulation of the post-implement registry command → exit 0 with `directory: "{ui_scan_dir}"` in JSON.

**2. Matrix**: R1–R4 all **Partial** — unit-level test specs are exact and assertable, but call-site wiring, the registry suite, and 6 of 8 rows of the design's own failure-mode table have zero tests.

**3. Findings** (severity-ordered):
- **F-1 High**: `tests/test_validator_registry.py` is absent from the design's plan; after implement, `test_project_scan_validators_ok_on_empty_dir` scans the *literal* `{ui_scan_dir}` dir (proved by simulation) and `test_validator_commands_parse` ignores the new placeholder — both stay green while going vacuous. The exact F1 leak the design prevents in the runner gets re-created in the test suite. Fix: extend both tests (expand via the new helper; assert `{ui_scan_dir}`/`{tool_root}` not residual).
- **F-2 Medium**: none of the 4 call sites (`runner.py:487/:698`, `repo_gates.py:31-32`, `cli_batch.py:408`) is driven by a test. T3.3 only re-runs the expanded string manually. Discriminating integration test spec given (violating file in `lib/` → `success is False`; a literal placeholder would vacuously pass).
- **F-3 Medium**: FM3 metachars / FM4 empty-whitespace / FM5 placeholder-in-value untested — exact parameterized `expand_cmd` assertions + `caplog` for the defensive `log.error` given.
- **F-4 Medium**: AC4 "make ci 全绿" is **unmeasurable in-campaign** — observed red test from lock contention (pid 1547473, age 3.9h). Exit criteria must require `make ci` outside the lock window.
- **F-5 Low**: T2.1 needs `"warning" not in result.stdout` (substring overlap with the stdout summary); T1.1's `_BATCH_CFG` monkeypatch is a no-op for `_resolve_validators` (import-time `VALIDATOR_SPECS`) — it belongs to T1.2 only.
- **F-6/F-7 Low/Info**: `--exclude`-all warning false positive (document); automate the §4 FM7 grep anchor as a 10-line source test pinning F1 permanently.

**4. Scenarios**: happy (default byte-identity, lib layout, absolute path) / boundary (empty, whitespace, spaces, metachars, `output=None`) / error (typo'd dir, missing dir, leakage) / race (**none introduced** — pure function, validators stay file-scope, N2 deferral untouched) / recovery (exit-0 warning doesn't trigger retry storm; `--reuse` revalidates with *current* config).

**5. Gaps/flakes/fixtures**: registry suite (F-1), wiring tests (F-2), FM-table tests (F-3), lock flake (F-4, observed); fixtures: reuse `_make_project` + new `lib/` tree + tmp-config pattern from `test_pi_batch.py:794-838` (`fake_agent`/`load_batch`); exit criteria: T1.1–T4.2 + F-1 fixes, `make ci` outside lock, `quality.py --strict`, F-7 anchor present.
