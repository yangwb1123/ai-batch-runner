All verification complete. The implementation is fully in place and green; my review covers both the design as specified and the live implementation, with two demonstrated weaknesses. Here is the QA lead report.

---

# QA Lead Review — check-no-refusal Regression + Duplicate Elimination

**Snapshot caveat (evidence integrity):** the tree was **mutated by a live campaign (pid 1684832, `campaign --jobs 4 --max-directions 1`, up 4h17m, 14 procs) during this review**. Initial commands saw the pre-change state (23 validators, no test file, duplicate present); by review end the full implementation existed (24 validators, `tests/test_no_refusal.py`, copy deleted, AGENTS.md updated). All results below are labeled with the state they were measured against. Every claim in the design's Evidence Ledger (E1–E6, F1–F6, FIND-1–FIND-4) was independently confirmed before the mutation — the ledger is accurate for the pre-change tree.

## 1. Test inventory — commands actually run (2026-08-08, live tree)

| Command | Result | Purpose |
|---|---|---|
| `python3 -m pytest tests/test_no_refusal.py -q` | **27 passed** (0.69s) | New suite (design predicted ~19; 27 implemented incl. optional A5.1) |
| `python3 pi-batch.py check --steps refusal --skip-eval; echo $?` | exit 0, `CHECK [refusal]` ×6 lines, "all 1 gates passed" | A4.1 manual |
| `python3 pi-batch.py check --skip-eval` | exit 0, "all 8 gates passed" (refusal included) | FR-4 default set |
| `python3 pi-batch.py check --steps not_a_real_step --skip-eval; echo $?` | exit 2 (pre-change: exit 0) | D2 / FIND-2 closure |
| `python3 pi-batch.py check --steps " " --skip-eval; echo $?` | **exit 0, "all 0 gates passed"** | residual no-op (Finding M-2) |
| `python3 pi-batch.py check --steps eval --skip-eval; echo $?` | exit 0, 0 gates (preserved) | compat claim |
| `python3 -m pytest tests/test_org.py -k check` | 12 passed | `_run_step` signature + `test_check_cli_full` compat |
| `python3 -m pytest tests/test_entrypoints.py -q` | 6 passed | subprocess conventions |
| `python3 quality.py --strict .` | `quality: OK` | budget |
| Direct gate smokes (clean/marker/empty/ws/missing/usage) | rc 0/1/1/1/1/2, exact diagnostics | A1.x observable behavior |
| `runpy.run_path` extraction | `REFUSAL_MARKERS`=10, `main()` not executed | F3 |
| `python3 -m pytest tests/ -q` | **960 passed, 21 failed, 1 skipped** | full suite — see CI gap G-1 |
| `git diff scripts/check-no-refusal.py` | empty (byte-identical) | NG1 / DoD |
| `git diff examples/snaplink-platform/requirement-pipeline.yaml` | `python`→`python3` only | §6.2 optional, done |
| `git diff pbatch/selfcheck.py` | matches design §3.2 exactly (incl. `import tempfile`) | spec compliance |
| Purge-simulation (marker removed from script copy) | rc 1 despite purge — **masking proven** | Finding M-1 |

## 2. Requirement-to-test matrix (all verified on live tree)

| Acceptance | Test (implemented) | Status | Evidence |
|---|---|---|---|
| A1.1 every marker → exit 1 | `test_marker_refuses[×10]` | ✅ green | **masking hole for 2 of 10 markers — see M-1** |
| A1.2 empty + whitespace → 1 | `test_empty_output_refuses`, `test_whitespace_only_refuses` | ✅ green | `empty output` asserted |
| A1.3 missing → 1 | `test_missing_file_refuses` | ✅ green | `artifact missing` |
| A1.4 clean → 0 | `test_clean_artifact_passes` | ✅ green | `REFUSAL-CHECK: OK` |
| A1.5 no-arg → 2 | `test_usage_without_args` | ✅ green | `usage:` |
| A1.6 ≥10 markers | `test_marker_list_not_purged` | ✅ green | count-only; identity gap (M-1) |
| A1.7 `REFUSAL-CHECK` on refusal | folded into A1.1/1.2/1.3 | ✅ green | |
| A2.1 no duplicate | `test_no_duplicate_copy` | ✅ green | file deleted (`git status` D) |
| A2.2 8 refs, canonical form | `test_pipelines_reference_canonical_copy` | ✅ green | exactly 8, regex-matched (5+1+1+1) |
| A2.3 examples/ tree clean | `test_examples_tree_only_validate_cmd_refs` | ✅ green | only 8 validate_cmd lines remain |
| A3.1 registered | `test_registered_in_validators` | ✅ green | `refusalcheck` in `VALIDATORS` (24) |
| A3.2 canonical path, not examples/ | same | ✅ green | |
| A4.1 CLI lists gate | manual run | ✅ green | exit 0 + `CHECK [refusal]` |
| A4.2 e2e in CI | `test_pi_batch_check_lists_gate` | ✅ green | **lock-free** (check dispatches before `acquire_lock` — `cli.py:303`, `main()` order verified) |
| A4.3 fail-closed | 2 unit tests + unknown-step | ✅ green | `is False`; rc 2 |
| A5.1 AGENTS.md count | `test_agents_md_validator_count_matches` (implemented beyond design's optional) | ✅ green | AGENTS.md:70 = "24 个验证器"; `len==24` |
| FR-4 structural | `test_refusal_step_in_default_steps` | ✅ green | |
| Compat: `test_check_cli_full` | `tests/test_org.py` | ✅ green | "all 3 gates passed" preserved |
| NG1 frozen script | `git diff` empty | ✅ green | zero edits |

Migration order red→green sequence, rollback granularity (independent hunks), and the deletion-XOR-identity branch discipline are all coherent as specified.

## 3. Findings (severity-sorted)

**H-1 — High (environmental, pre-existing): `make ci` is RED while any pi-batch process runs.** 21 failures measured (`test_cli_auto_pipeline_detection*`, `test_cli_validate_named_gofmt`, …): subprocess pipeline-mode tests hit the single-instance lock (`pbatch/lock.py:167`, `.pi-batch.lock` in repo root, exit 5). The tool's own operating model is 7×24 unattended — a lock is *normally* held. The design's verification plan step 2 (`make ci`) cannot pass on this host until pid 1684832 exits.
- *Regression risk:* the new suite's gate-path is unaffected (check is lock-free), but a reviewer rerunning `make ci` mid-campaign will see 21 unrelated failures and may misattribute them to this change.
- *Exact test to add (separate direction):* pipeline-mode tests should invoke with `--no-lock` (T4 escape hatch exists) or the suite should run against an isolated state dir.
- *Acceptance:* `make ci` green while a background campaign holds the lock.

**M-1 — Medium: A1.1 marker-isolation hole — purge/rename of an overlapping marker is masked.** Demonstrated: removing `"FAILED — 输入缺失"` from a script copy while keeping `"输入缺失"` and `"FAILED — 输入"` leaves `test_marker_refuses["FAILED — 输入缺失"]` **green** (fixture `…标记：FAILED — 输入缺失。` still matches `输入缺失` → rc 1) and the count stays ≥10 → both guards pass. The design's own E1 note ("harmless double-hit") is wrong *for the test*: the hit is not harmless, it blinds the detector. A rename keeping the count at 10 evades FM1/FM2 entirely.
- *Exact test to add:* in `test_marker_refuses`, parse the printed `markers: [...]` list and assert the specific marker is present: `hits = ast.literal_eval(re.search(r"markers: (\[.*?\])", stdout).group(1)); assert marker in hits`. Optionally also assert the fixture contains no *other* marker.
- *Acceptance assertion:* `rc == 1 and marker in parsed_hits` for every marker, including the two overlapping ones.

**M-2 — Medium: `--steps " "` (whitespace-only) is a surviving no-op.** `pi-batch.py check --steps " " --skip-eval` → exit 0, "all 0 gates passed". Same class as FIND-2/A4.3 ("never a no-op"), closed only for *unknown* names, not for an *empty after-strip* wanted-set. Also `--steps ","` and `--steps "  ,  "` behave identically.
- *Exact test to add:* `test_check_rejects_blank_steps` — invoke with `--steps " "` → rc 2 (or explicitly defined semantics; fail-closed is consistent with D2).
- *Acceptance:* blank step name lists exit 2 with `unknown step`-style stderr.

**M-3 — Medium: no negative-boundary (false-positive) regression pin.** The gate is naive substring matching over common prose words (`输入缺失`, `未指定`, `无法继续`). Legitimate LLM artifacts describing the problem being fixed — e.g., "当输入缺失时返回 400" or "用户无法继续流程" — are **refused today** (both contain markers). Only one clean sentence is pinned (`这是一份正常的阶段产物，内容完整。`). The precision boundary is undocumented and untested; a future marker edit silently shifts it.
- *Exact test to add (pins current behavior, no script change):* `test_near_miss_prose_refuses` for `"用户无法继续流程"` (rc 1 today) and one intended-accept boundary doc (`test_clean_multi_paragraph_artifact_passes` with headers/code blocks). This makes the trade-off explicit and reviewable.
- *Acceptance:* near-miss prose refuses; multi-paragraph clean artifact passes.

**L-1 — Low: `_run_refusal_step` default script path is `__file__`-relative; sibling steps are cwd-relative.** From repo root (documented mode) both coincide. In a ported copy (AGENTS.md portability story) the new step resolves `site-packages/scripts/…` while others fail on `quality.py` — inconsistent failure modes, no new exposure class.

**I-1 — Info: `test_marker_list_not_purged` pins count (≥10), not identity.** Combined with M-1 this is the last guard; a same-count marker swap evades both. The hit-list assertion in M-1 also fixes this.

**I-2 — Info: `_markers()` runs at collection time via `runpy`** — if the canonical script is deleted (FM7), collection errors rather than a clean single failure. Acceptable; noted for diagnostics.

**I-3 — Info: pi-batch.yaml working-tree diff is 54 lines** — the `refusalcheck` hunk is additive, but the commit under review also carries other directions' validator work (concurrent campaign). Verify hunk isolation at commit time; nothing in this direction touches the 23 pre-existing entries.

## 4. Prioritized scenarios (happy / boundary / error / race / recovery)

1. **Happy:** clean artifact → 0 ✅; registry entry → step OK ✅; default `check` lists gate ✅; filtered `--steps refusal` ✅.
2. **Boundary:** empty/whitespace ✅; missing file ✅; usage ✅; **marker at file start / inside code fence / split across lines** — not covered (substring on whole text makes these trivially safe; low value); **non-UTF-8 artifact** (script's `errors="replace"` path) — untested, one-line test candidate; **single-char artifact** — untested, trivially safe; **marker-only file** (no prose) — untested, trivially covered by substring; **near-miss prose** (M-3) — untested, must add.
3. **Error:** no-arg ✅; bad path ✅; registry entry removed → step False ✅; script missing → step False ✅; script unreadable (permission) — untested, low value; timeout path (smoke `timeout=30`, tests `timeout=120`) — untested, safe by construction.
4. **Race:** gate is stateless — no shared mutable state, tmp dirs per test ✅; concurrent `pi-batch check` invocations — lock-free, verified ✅; **concurrent campaign vs test suite** — FAILS (H-1), must fix at suite level.
5. **Recovery:** refusal → validator fail → retry/regeneration loop is runner-level (`--retries`/rounds), gate is the detector only — no gate-side recovery path exists or is needed; `--reuse` never reuses validator-failed artifacts (documented mechanism) — no new interaction with this change; **mid-migration red states** (design steps 1–3) are intentional, rollback is hunk-revert — verified independent.

## 5. CI / manual-suite gaps, flake risks, exit criteria

**Gaps:**
- G-1 (High): lock isolation for pipeline-mode subprocess tests — currently red under any live pi-batch run (21 failures). Blocks the design's own verification plan on this host.
- G-2 (Medium): M-1 hit-list assertion; M-2 blank-`--steps` rejection; M-3 near-miss boundary pin.
- G-3 (Low): no full-default `pi-batch check` (no `--steps`) test in CI — exists only as manual DoD; `make ci` deliberately excludes it (verified Makefile `ci: check self-test test quality org`), consistent with design's wall-time claim.

**Flake risks:** low. All new tests are deterministic (fixed fixtures, no time/random, tmp_path isolation, `timeout=120` mirroring `test_entrypoints.py`); `test_pi_batch_check_lists_gate` measured ~0.2s of the 120s budget; parametrized ids truncated to 12 chars (unique). The only environment-dependent test is `test_refusal_step_pass_with_live_config` (depends on live registry+script) — intentional, not flaky. No conftest needed; `from pbatch import …` works under `python -m pytest` from root (FIND-4 honored).

**Fixtures needed:** none beyond `tmp_path`; zero new deps (yaml already hard dep, used only in A5.1).

**Exit criteria for this direction:**
1. `tests/test_no_refusal.py` green **plus** M-1 hit-list assertion added and green;
2. `--steps " "` exits 2 (M-2 closed) with test;
3. `make ci` green **on a lock-free host** (or after H-1 isolation lands); on this host, re-run after pid 1684832 exits;
4. `quality.py --strict .` green (✅ measured);
5. `git diff scripts/check-no-refusal.py` empty (✅ measured) and `examples/` grep = exactly 8 validate_cmd lines (✅ measured);
6. `test_org.py -k check` + `test_entrypoints.py` green (✅ measured).

**Verdict on the design:** sound, fail-closed, migration and rollback are well-formed, compatibility claims all verified. Recommend **conditional acceptance**: ship after closing M-1 (one assertion) and M-2 (one guard + test); schedule H-1 as a separate suite-isolation direction; add M-3 as a documented-boundary test at implementer's discretion. Findings are advisory; no files were modified by this review.
