All verification complete. Nothing staged, no commits made. Here is the change report.

---

# Change Report — uicheck/uigeometry/uiquality 扫描目录可配置 (`ui_scan_dir`)

**Direction**: `ui-specs-672fd14a` (docs/auto/requirements/ui-specs-672fd14a-requirements.md + design docs/auto/designs/ui-specs-672fd14a-design.md)

## 1. Requirement assessment

Ran `pi-batch assess "<direction title> + <problem>"` — domain manifest loaded automatically: **frontend domain** (ui-specs/), prescription **demo-tier** (6 rules: visual-core, geometry, design-intelligence ×3), workflow **L0_direct**, productization **L0_local_feature**. **Persistence-design gate: not applicable** — the direction touches no core data (config + validator plumbing only); per the assessor's克制 prescription the required rules are the visual/geometry manifest, which this tooling change does not violate (it adds no UI output).

## 2. Files / symbols changed

**Core implementation** (already in tree from the campaign implement stage — I verified every touchpoint live and closed the remaining evidence-flagged gaps):

| File | Symbol / change |
|---|---|
| `pbatch/cmd_expand.py` **(new)** | `ui_scan_dir()` (config `_BATCH_CFG` lookup, empty/whitespace → None) + `expand_cmd()` — single placeholder-expansion entry point for `{ui_scan_dir}/{output}/{cwd}/{tool_root}`; defensive `log.error` on unresolved placeholder |
| `pbatch/runner.py:487/:698` | fresh-save + reuse validator paths → `expand_cmd()` |
| `pbatch/repo_gates.py:31` | stage-end repo gates → `expand_cmd()` (incidentally closes `{tool_root}` gap, zero behavior change) |
| `pbatch/cli_batch.py:408` | batch-end repo gates → `expand_cmd(spec.cmd, None, wd)` (`output=None` semantics preserved) |
| `pi-batch.yaml` | `uicheck`/`uigeometry`/`uiquality` commands `--dir {cwd}/src` → `--dir {ui_scan_dir}`; top-level `ui_scan_dir:` documentation; comments updated |
| `scripts/check-ui-spec.py` | `_warn_zero_scan()` — stderr warning when `--dir` project scan yields 0 files; exit code stays 0 |

**Gaps closed this session** (from qa_lead / testing_reviewer / backend_engineer / design-gate evidence):
- `examples/frontend-implementation-pipeline.yaml:23`, `examples/frontend-engineering-review.yaml:8` — **L2**: stale comments teaching hand-editing `--dir` → point at `ui_scan_dir`
- `pi-batch.yaml` top-level comment — **F5**: overstatement "all gates warn" → corrected to uicheck-warns / uigeometry-uiquality-silent wording
- `scripts/check-ui-spec.py` — **F8**: `--exclude`-all false-positive documented in `_warn_zero_scan` docstring

## 3. Tests added (4 new this session; 7 total direction tests)

| Test | File | Covers |
|---|---|---|
| `test_expand_cmd_defensive_branch_logs_unresolved` | tests/test_ui_check.py | **testing F1 (the open Medium)**: FM5 placeholder-in-value → caplog pins `unresolved {ui_scan_dir}` |
| `test_single_expansion_entry_source_anchor` | tests/test_validator_registry.py | **F4/F-7**: zero hand-rolled `.replace("{cwd}")`/`.replace("{tool_root}")` outside cmd_expand.py (pipeline.py:633 exempted as documented stage-command non-goal) |
| `test_stage_repo_gate_expands_ui_scan_dir` | tests/test_local_fixes.py | **F3**: repo-scope gate through `run_stage_repo_validators` — failing-command detail carries expanded path, never the literal |
| `test_batch_end_repo_gate_expands_ui_scan_dir` | tests/test_local_fixes.py | **F3**: cli_batch site expands `{ui_scan_dir}`/`{cwd}`, keeps literal `{output}` (output=None semantics) |

Also extended: **F2** — `test_validator_commands_parse` now expands via the helper and asserts `{ui_scan_dir}`/`{tool_root}` not residual (QA F-1 residual half); **F7** — `"warning" not in result.stdout` negative assertion in `test_dir_scan_zero_files_warns_on_stderr`.

## 4. Command results

| Gate | Result |
|---|---|
| `pytest tests/test_ui_check.py tests/test_validator_registry.py` | **27 passed** (19 + 8) |
| `pytest tests/test_local_fixes.py` (incl. 5 repo-gate tests) | 95 passed, 8 failed — **all 8 = singleton-lock environmental** |
| `pytest tests/test_ui_geometry.py tests/test_frontend_quality.py` | 42 passed (earlier run) |
| `pytest tests/test_pi_batch.py -k validate/repo/reuse` | 23 passed, 5 failed — all 5 lock-environmental |
| `make ci` | **1043 passed / 21 failed — 21/21 lock-environmental** (22 `pid=1547473 … exit 5` mentions; secondary FileNotFoundErrors are CLI-never-ran effects). Matches the recorded 1039+21 baseline plus my 4 new tests. `make` aborted at `test`; remaining steps run separately: |
| `make check` / `make self-test` / `make quality` / `make org` | **all green** (exit 0; check FAIL lines are the gates' own expected-catch self-tests) |
| `python quality.py --strict .` | **OK** |
| `python pi-batch.py check` | **9 gates passed, 42/42 eval PASS** |
| Byte-identity probe | unset `ui_scan_dir` → `expand_cmd` output **byte-identical** to the historical literal chain |
| Live warning probe | `--dir <empty>` → stderr warning + exit 0 + stdout pure JSON (anchors `OK (0 file(s) scanned)` / `files_scanned: 0` intact) |

## 5. Evidence-backed deviations

1. **Helper in `pbatch/cmd_expand.py`, not `config.py`** (design §2.2 said config.py) — documented deviation: config.py sits at 487/500 lines (filesize budget), cmd_expand.py has no budget pressure. Approved by backend_engineer (Info finding), dependency direction cmd_expand → config is acyclic.
2. **`make ci` full-green is not an in-campaign exit criterion** (QA F-4): the running campaign (pid 1547473, age ~4.7h, `pi-batch.py campaign --retry-failed --reuse`) holds the singleton lock; all 21 failures reproduce its exact signature and 0 involve this direction. Per the design-gate verdict this is explicitly accepted with reproduced evidence; `make ci` must be re-run outside the lock window for the full-green confirmation.
3. **F-7 source anchor exempts `pbatch/pipeline.py:633`** — the stage post-command surface (`{tool_root}` only, no validator commands), declared non-goal in the design's F1 section.
4. Non-goals honored: no default scan-root change, no uigeometry/uiquality warnings, no `backendquality` changes, no CLI/scan-semantics changes, `--json` stdout contract untouched.

**No commits made** (0 staged; working-tree modifications only, consistent with the in-progress campaign state).
