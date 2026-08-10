Review written to `docs/auto/runs/make-uicheck-uigeometry-uiquality-dir-configurab-31146de9/artifacts/adversarial_review-9c87f3a7/meta/testing_reviewer.md`.

---

## VERDICT: PASS - test plan is discriminating where it matters; AC1–AC4 rows have exact non-vacuous assertions; remaining gaps (1 Medium: unpinned defensive branch; 2 call sites without placeholder-specific tests; doc accuracy) do not block the direction.

Every claim below was re-verified live this session, not inherited:

- `pytest tests/test_ui_check.py tests/test_validator_registry.py -q` → **25 passed** (18+7)
- `pytest tests/test_ui_geometry.py tests/test_frontend_quality.py -q` → **42 passed**
- `pytest tests/test_pi_batch.py -k "validate"` → 17 passed / 3 failed; `-k "repo or stage_validate or reuse"` → 8 passed / 2 failed — **all 5 failures are the singleton lock** (error text names `pid=1547473 argv=pi-batch.py campaign --retry-failed --reuse`, the campaign running me; `ps` confirms age 4.5h)
- `quality.py --strict .` → OK; `pi-batch.py check` → **9 gates passed, 42/42 eval PASS**
- Live: nonexistent `--dir` → stderr warning + exit 0 (FM2 visibility holds); **uigeometry/uiquality on empty dir → silent exit 0** (comment overstates — confirmed)
- `grep '.replace("{cwd}"' pbatch/` → exactly 1 site (the helper); git diff of all 4 call sites → all via `expand_cmd`, `output=None`/`{tool_root}` semantics preserved

## Findings

| # | Sev | Defect pattern | Missing test | Exact test that would catch it |
|---|---|---|---|---|
| F1 | Med | FM5 placeholder-in-value defensive branch reachable but unpinned (no caplog) | caplog test for `unresolved {ui_scan_dir}` | `monkeypatch.setattr(config, "_BATCH_CFG", {"ui_scan_dir": "{ui_scan_dir}"}); with caplog.at_level("ERROR"): expand_cmd("--dir {ui_scan_dir}", None, "/proj"); assert "unresolved {ui_scan_dir}" in caplog.text` |
| F2 | Low | QA F-1 half-fixed: `test_validator_commands_parse` normalizes only `{output}`/`{cwd}` — a `{ui_scan_dir}` residual passes silently | residual assert in parse test | after normalization: `assert "{ui_scan_dir}" not in resolved, name` |
| F3 | Low | `repo_gates.py:31`/`cli_batch.py:408` have no placeholder-specific test (runner sites are covered) | repo-scope validator with `{ui_scan_dir}` through `run_stage_repo_validators` (pattern: `test_local_fixes.py:1354-1377`) | `validate_cmd="echo {ui_scan_dir} >&2; exit 0", scope="repo"` → captured output contains expanded path, never the literal |
| F4 | Low | F-7 source anchor absent — "single expansion entry" enforced by grep only | 10-line source test | for each `pbatch/*.py` except `cmd_expand.py`: no `'.replace("{cwd}"'` / `'.replace("{tool_root}"'` in src |
| F5 | Low | yaml top-level comment claims all 3 gates warn on zero files; **uigeometry/uiquality are silent** (measured) — operator misled about vacuous-pass visibility | doc fix (uicheck-only wording) or add warnings (scope decision) | N/A |
| F6 | Low | `examples/frontend-implementation-pipeline.yaml:23`, `frontend-engineering-review.yaml:8` still teach hand-editing `--dir` | doc fix → `ui_scan_dir` | N/A |
| F7 | Low | T2.1 lacks `"warning" not in result.stdout` (QA F-5; covered indirectly) | negative assertion | add to `test_dir_scan_zero_files_warns_on_stderr` |
| F8 | Info | `--exclude`-all → warning false positive undocumented | comment in `_warn_zero_scan` | N/A |

## Risk-coverage matrix

| Risky path | Covered? | Proof |
|---|---|---|
| Literal `{ui_scan_dir}` → shell → vacuous green | **Partial** (structural + 3 tests; no F-7 anchor, F2) | single replace site; exact-string tests; T3.3 `files_scanned > 0` on real `lib/` tree is the discriminator (default `{cwd}/src` there → 0 → fails) |
| Default byte-identity | Yes | `--dir /proj/src` exact; `-o {output}` literal with `output=None` |
| Relative/absolute/whitespace/metachar value | Yes | 5 exact cases incl. `'my lib'` quoting |
| Typo'd/nonexistent dir | Yes (script-level, live) | warning + exit 0; same path as T2.1 |
| Warning channel: stderr/exit0/`--json` stdout purity | Yes | T2.1/T2.2/T2.3 + `test_json_clean_path_stdout_pure` |
| Zero-file vacuous pass visible | **Partial** — uicheck warns; uigeometry/uiquality silent (F5) | live runs |
| Runner wiring (487 fresh / 698 reuse) | Yes | 17 runner-level tests pass, both sites share helper |
| repo_gates / cli_batch wiring | **No direct test** (F3) | only `cmd="true"` repo-scope tests |
| FM5 defensive branch | Code works (probe), untested (F1) | live probe |
| Race/async/dup-submit/timeout/409/permission/unmount/rollback/pagination/tz/partial success | **N/A** — no such paths in this change: pure function, no concurrency, file-scope unchanged, N2 deferral untouched | diff-verified |

## Honesty audit

- **Executed vs claimed**: backend_engineer's core claims (4 sites, single replace site, 18+7 green, quality OK, `check` 9 gates + 42 evals) — **all re-verified by re-running**; no discrepancy.
- **Not executed, with reason**: full `pytest` "1039/21" and `make ci` — blocked by the campaign's own lock (pid 1547473 confirmed in failure text and `ps`). My 5 observed failures share that exact signature; full count not independently reproducible in-campaign. **`make ci` must be run outside the lock window** — it cannot be an in-campaign exit criterion.
- **No fabrication**: completion gate exercised both directions (honest accept / fabricated reject); no skipped tests; no "理论上应该通过" phrasing in the implementation artifact.
