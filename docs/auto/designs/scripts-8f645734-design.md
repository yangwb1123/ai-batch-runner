# Design — 统一校验器 CLI 面与 --json 的 stdout 契约（--skip 缺失 + JSON 后跟摘要行污染机器输出）

| Field | Value |
|---|---|
| Module | `scripts` |
| Direction id | `8f645734` |
| Source | `docs/auto/requirements/scripts-8f645734-requirements.md` (evidence-verified) |
| Status | Design (independently verified) |
| Change scope | `scripts/check-backend-quality.py` + `scripts/check-frontend-quality.py` + `pbatch/health.py` + 4 test files |

---

## 1. Verification of the evidence (untrusted claims → checked)

Every citation was re-verified against the live repository, including live
reproductions of both pollution paths and the unknown-flag mis-parse.

| Evidence claim | Verdict | Notes |
|---|---|---|
| `check-backend-quality.py` hand-written `parse_argv` :396-413, no `--skip` | ✅ **Confirmed** | :396-413; only `--dir/--manifest/-o/--strict/--json`; unknown args → `targets.append(arg)` (:410). |
| backend `DEFAULT_EXCLUDES` :64-76 contains `generated`, `migrations` | ✅ **Confirmed** | :64-75 (closing brace at :75); `generated`/`migrations` at :70. |
| backend OK summary :466 / skipped summary :464 lack `file=sys.stderr` | ✅ **Confirmed** | `_finish` :463-466; both bare `print(...)` → stdout. Violation-path summaries (:453-461) already go to stderr. |
| backend `print(payload)` :447 on stdout | ✅ **Confirmed** | `_emit_report` :447 (`if want_json:`). |
| `check-frontend-quality.py` hand-written `parse_argv` :220-234, no `--skip` | ✅ **Confirmed** | :220-234; only `--dir/--strict/--json/-o`; unknown → targets. |
| frontend `DEFAULT_EXCLUDES` :38-51 contains `e2e` | ✅ **Confirmed** | :41. |
| frontend OK summary :281 lacks `file=` | ✅ **Confirmed** | `print(f"UI-QUALITY: OK ...")` :281 → stdout; violation summaries :276-279 already stderr. |
| `check-ui-spec.py:244-253` correct stdout contract | ✅ **Confirmed** | :244 `print(payload)`, :245 comment "stdout carries ONLY the JSON report; summary goes to stderr", :246-251 summaries `file=sys.stderr` + early return :252. Also has `--exclude` (:154-161, `_VALUE_FLAGS` :151) — same add-exclusion semantics, different flag name. |
| `pbatch/health.py:48-50` special case, :55-57 `raw_decode` | ✅ **Confirmed** | :48 comment verbatim; :49-50 `if name != "check-backend-quality": cmd += ["--skip", SKIP]`; :55-57 `stdout.index("{")` + `JSONDecoder().raw_decode`. |
| `pbatch/advance.py:97-108` third degraded consumer | ✅ **Confirmed** | `_run_checker` :103-108 `json.loads(result.stdout or "{}")` with `except Exception → {"files_scanned": 0, ...}`. Backend runs on this repo → skipped path pollutes → silently `{}`. |
| `check-design-intelligence.py` `--skip` (argparse :274-276), segment semantics :218-226 | ✅ **Confirmed** | `_collect_files` :218-226 `any(part in skipped for part in p.parts)` — exact segment membership, dir parts only. |
| `check-knowledge-freshness.py:225` supports `--skip` | ✅ **Confirmed** | :225 argparse `--skip` (also `--skip-modules` :228). |
| `pi-batch.yaml:131` designintelligence validator uses `--skip` | ✅ **Confirmed** | :131 `--skip .dart_tool,test,build,ephemeral`; additionally :132 backendexperience uses `--skip` — one more family member than the spec cites. |
| Polluted stdout live (backend skipped path) | ✅ **Reproduced** | `--dir . --json` → trailing `BACKEND-QUALITY: no backend manifest detected; gate skipped`; `json.loads` → `JSONDecodeError: Extra data: line 10 column 1 (char 139)`. |
| Polluted stdout live (backend OK path) | ✅ **Reproduced** | Fixture `go.mod` + clean `src/main.go` → trailing `BACKEND-QUALITY: OK (1 file(s) scanned)`; `json.loads` → `JSONDecodeError`. |
| Polluted stdout live (frontend OK path) | ✅ **Reproduced** | `--dir examples --json` → trailing `UI-QUALITY: OK (0 file(s) scanned)`; `json.loads` → `JSONDecodeError`. |
| Unknown flags silently mis-parsed as file targets | ✅ **Reproduced** | frontend: `--skip scratch` → `files_scanned: 3`, violations include `'--skip'` and `'scratch'` as "missing target" files. backend: `--skip scratch` on violation fixture → `files_scanned` 0→2, both phantom entries violations. Neither rejected nor honored. |
| Violation path already pure (only OK/skipped polluted) | ✅ **Confirmed** | Code reading: all violation/rejection summaries have `file=sys.stderr`; live `json.loads` on violation fixtures parses (exit 1 path). |
| health `--json` baseline shape | ✅ **Reproduced** | `keys: [checkers, governance, healthy, quality_violations, score, specs, tests]`; `checkers: {designintelligence: 0, knowledge: 0, backendquality: 0}`; healthy True. |
| All 3 checkers health.py depends on have pure stdout (precondition for `raw_decode` removal) | ✅ **Verified** | design-intelligence & knowledge-freshness: `json.loads` PURE on OK path, empty-dir path, and knowledge's `--skip-modules` path. Backend purity is the fix itself (AC1). |
| Test conventions | ✅ **Confirmed** | `test_backend_specs.py` `clean_repo` fixture (mkdtemp under repo root, dodging stray `/tmp/go.mod`, :27-31), `_run` helper :34-36; `test_frontend_quality.py` `_run`/`_write`/tmp_path; `test_tools_health.py` has no test pinning `raw_decode` (removal breaks nothing); 91 tests in the 4 affected files currently green. |
| No external callers of changed internals | ✅ **Verified** | No test imports `parse_argv`/`resolve_dir_scan`/`_finish`; only consumers of the two checkers are `health.py`, `advance.py`, `pi-batch.yaml` validators (:171/:181, exit-code + `-o` based), and `agent-engineering/*.yaml` prompt templates (invocation-only). |

**Material refinements vs. the source spec:**
1. Backend mis-parse reproduction differs numerically from the appendix
   (0→2 rather than 2→4) because a `.py` file under a `go.mod` manifest is
   not a backend source (exts = `{.go}`) — same mechanism, same phantom
   `'--skip'`/`'scratch'` entries. Test fixtures must use `.go` files.
2. `DEFAULT_EXCLUDES` is a mutable set literal in both checkers; the design
   must build the union as a **new set** (`DEFAULT_EXCLUDES | extra`), never
   mutate the constant (module-level state would leak across `--skip` runs).
3. ui-spec's summary-to-stderr is conditioned on `want_json` only (not
   `want_json or report_path`): `-o`-only mode falls through to the human
   flow. The backend/frontend fix uses the same condition — this preserves
   the "`-o` 文件写入逐字节不变" constraint.

---

## 2. API changes

### API-1: `--skip` flag (both checkers)

New CLI flag, family-consistent with `check-design-intelligence.py` /
`check-knowledge-freshness.py` / `check-backend-experience.py`:

```
--skip NAMES    comma-separated path fragments to exclude (e.g. scratch,proto)
                appended (union) to DEFAULT_EXCLUDES; segment-exact match;
                only affects --dir directory scans
```

**Semantics (inherited from family + existing `scan_directory`):**
- Exclusion set = `DEFAULT_EXCLUDES ∪ {s for s in NAMES.split(",") if s}` —
  **union, never replace** (replacement would silently re-enable scanning of
  `tests/`, `docs/`, `node_modules/`, ... — the exact trap AC3's negative
  control guards against).
- Match rule = existing `scan_directory` predicate
  `any(part in excludes for part in path.parts[:-1])`: **directory-segment
  exact membership**, any depth. `--skip scratch` excludes `a/scratch/b.go`
  but not a root file literally named `scratch.go`.
- `--skip ""` → empty set → no-op (idempotent).
- Explicit file targets are never excluded (unchanged).
- Backend manifest detection / walk-up / forced `--manifest` untouched;
  `--skip` filters only the directory scan in `resolve_dir_scan`.

**Backend changes** (`scripts/check-backend-quality.py`):

```python
def parse_argv(argv: list) -> tuple:
    """(targets, directory, strict, want_json, report_path, forced_manifest, skip)."""
    targets, directory, strict, want_json, report_path = [], None, False, False, ""
    forced, skip = None, ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--dir", "--manifest", "-o", "--skip") and i + 1 < len(argv):
            value = argv[i + 1]
            i += 1
            if arg == "--dir":
                directory = Path(value)
            elif arg == "--manifest":
                forced = value
            elif arg == "--skip":
                skip = value
            else:
                report_path = value
        elif arg == "--strict":
            strict = True
        elif arg == "--json":
            want_json = True
        else:
            targets.append(arg)
        i += 1
    return targets, directory, strict, want_json, report_path, forced, skip
```

```python
def resolve_dir_scan(directory: Path, forced: str, skip: str = "") -> tuple:
    """(files, manifest name, status, scan_error); skip = extra excludes."""
    ...
    files = scan_directory(directory, exts, DEFAULT_EXCLUDES | {
        s for s in skip.split(",") if s})
```

**Frontend changes** (`scripts/check-frontend-quality.py`):

```python
def parse_argv(argv: list) -> tuple:
    """(targets, directory, strict, want_json, report_path, skip)."""
    targets, directory, strict, want_json, report_path, skip = [], None, False, False, "", ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--dir" and i + 1 < len(argv):
            directory = Path(argv[i + 1])
            i += 1
        elif arg == "--strict":
            strict = True
        elif arg == "--json":
            want_json = True
        elif arg == "-o" and i + 1 < len(argv):
            report_path = argv[i + 1]
            i += 1
        elif arg == "--skip" and i + 1 < len(argv):
            skip = argv[i + 1]
            i += 1
        else:
            targets.append(arg)
        i += 1
    return targets, directory, strict, want_json, report_path, skip

# main():
    if directory is not None:
        paths.extend(scan_directory(
            directory, DEFAULT_EXTS,
            set(DEFAULT_EXCLUDES) | {s for s in skip.split(",") if s}))
```

### API-2: `--json` stdout purity (both checkers)

Under `--json`, stdout must contain **exactly one JSON document** (trailing
newline allowed). All human summaries — OK, skipped, empty, scan_error,
violation rejection — go to stderr. Non-`--json` mode is byte-identical.

The violation-path summaries already use `file=sys.stderr`; only the two
fall-through lines change, conditioned on `want_json` (matching ui-spec's
condition exactly — `-o`-only mode keeps human summaries on stdout):

```python
# backend _finish (signature gains want_json; single call site at :495):
def _finish(status, scan_error, violations, n_files, want_json=False) -> int:
    ...
    if status == "skipped":
        print("BACKEND-QUALITY: no backend manifest detected; gate skipped",
              file=sys.stderr if want_json else None)
        return 0
    print(f"BACKEND-QUALITY: OK ({n_files} file(s) scanned)",
          file=sys.stderr if want_json else None)
    return 0

# frontend main (:281):
    print(f"UI-QUALITY: OK ({len(paths)} file(s) scanned)",
          file=sys.stderr if want_json else None)
```

`print(..., file=sys.stderr if want_json else None)` — `None` = default
stdout, so the non-json path is byte-identical by construction.

### API-3: `health.py` patch removal

`pbatch/health.py` `_checker_violations` (:43-58): delete the special case and
the tail-tolerant parse; all three checkers get `--skip` uniformly and are
parsed with plain `json.loads`:

```python
def _checker_violations(name: str) -> int:
    script = ROOT / "scripts" / f"{name}.py"
    if not script.exists():
        return -1
    cmd = [sys.executable, str(script), "--dir", str(ROOT),
           "--json", "--skip", SKIP]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode not in (0, 1):
        return -1
    try:
        return json.loads(result.stdout).get("total", -1)
    except ValueError:  # JSONDecodeError is a ValueError subclass
        return -1
```

Unchanged: timeout 120s, returncode ∈ {0,1} gate, `-1` sentinel,
`health_report` shape, `_health_score`, `healthy` computation.

---

## 3. Compatibility constraints

| # | Constraint | How preserved |
|---|---|---|
| C1 | Non-`--json` human output byte-identical (summaries stay on stdout) | `file=sys.stderr if want_json else None`; guard test asserts the OK line is on stdout in non-json runs |
| C2 | `-o` report file bytes unchanged; `-o`-only runs keep human summaries on stdout | Report writing code untouched; condition on `want_json` only |
| C3 | Exit codes unchanged (0 pass / 1 reject, fail-closed on scan_error/empty/violations) | `_finish` / main return logic untouched |
| C4 | Violation detail lines stay on stderr (runner feeds stderr into retry prompts) | Already stderr; untouched |
| C5 | `--skip` = union, never replace; explicit file targets unaffected; `--skip ""` no-op | Union construction in both mains; target scan path untouched |
| C6 | Backend manifest walk-up / forced `--manifest` / strict-mode grading unchanged | Only `scan_directory` call gains the union set |
| C7 | `health --json` report keys/shape/values unchanged (this repo: `backendquality: 0`, healthy True) | Report builder untouched; AC4 pins shape + `backendquality` int |
| C8 | `check-ui-spec.py` untouched (AC5 regression guard only) | Out of scope |
| C9 | `advance.py`, `pi-batch.yaml` validators, `agent-engineering/*.yaml` untouched | Out of scope; advance.py benefits automatically (see F7) |

---

## 4. Failure modes

| # | Mode | Detection / mitigation |
|---|---|---|
| F1 | Summary accidentally rerouted to stderr in non-json mode (e.g. unconditional `file=sys.stderr`) | C1 guard test asserts `"BACKEND-QUALITY: OK"`/`"UI-QUALITY: OK"` present in **stdout** of a non-`--json` run |
| F2 | `--skip` implemented as replacement of `DEFAULT_EXCLUDES` | AC3 negative control: `--skip e2e` must yield `files_scanned == 2` (default), never 3+; replace-implementations inflate the count by re-scanning `test/`, `spec/`, `node_modules/` |
| F3 | `--skip` given without a value (trailing arg) | Falls to `targets.append(arg)` → visible "missing target" violation in the JSON report (exit 1). Documented, not silent; not an error (minimal-change scope, no argparse migration). **F1 adjudication (gate)**: a *flag-like value* after a value flag (`--skip --json`/`--skip --strict`) is NOT covered by this row — the hand-rolled parser swallowed it (silent human mode, exit 0 false pass on clean input). HARDENED: shared value-taking branch rejects any dash-leading value except bare `-` with `error: <flag>: expected one argument (got <v>)` → exit 2 — verified to match the argparse family (check-backend-experience/design-intelligence/knowledge-freshness all exit 2 live; the QA "argparse 家族同款" claim was empirically wrong). Same guard covers `--dir --strict` (was visible scan-error exit 1, now usage-error exit 2) and `--manifest --json`. Pinned by `test_backend_flaglike_value_rejected` / `test_frontend_flaglike_value_rejected`; bare `-` stays legal per family convention. |
| F4 | Future checker regression reintroduces trailing stdout lines → health `json.loads` fails | Fail-closed: `-1` sentinel → `checkers[name] == -1` → `healthy: False` (all(v == 0) at health.py:142) → visible alarm in the health report. Verified today that all 3 checkers are pure on OK/empty/modules paths, so no false alarm now |
| F5 | Behavior change: health previously tolerated leading non-JSON via `stdout.index("{")` | Accepted per contract (stdout = exactly one JSON doc). The `-1` + `healthy: False` path (F4) makes any future impure checker an alarm instead of a silent misread |
| F6 | Backend fixture pitfalls in tests (stray `/tmp/go.mod` walk-up false-detect; `.py` not scanned under `go.mod` manifest) | Backend fixtures use `clean_repo`-style mkdtemp **under the repo root** and `.go` sources only (the existing convention, test_backend_specs.py:27-31) |
| F7 | `advance.py` metrics change: backend `files_scanned`/totals become non-zero after the fix (previously silently `{}`) | Intended benefit; no code change in advance.py; not asserted by tests (out of scope) |
| F8 | Module-level `DEFAULT_EXCLUDES` mutation leaking between runs | Union built as a new set expression (`DEFAULT_EXCLUDES | {...}`); the constant is never mutated |
| F9 | `--skip scratch` segment-semantics surprise (doesn't exclude a root file named `scratch.go`) | Matches family (`design-intelligence._collect_files`); tests use directory-shaped fixtures so the mechanism is exercised as documented |

---

## 5. Migration steps

1. **`scripts/check-backend-quality.py`**
   - `parse_argv` (:396-413): add `"--skip"` to the value-taking tuple; return 7-tuple.
   - `main` (:471): unpack `skip`; pass to `resolve_dir_scan` call (:478).
   - `resolve_dir_scan` (:154): add `skip: str = ""` param; union into the `scan_directory` call (:163).
   - `_finish` (:450): add `want_json=False` param; `file=` conditionals on the two fall-through prints (:464, :466).
   - `main` (:495): pass `want_json` to `_finish`.
   - Update module docstring usage examples (:28-30) with `[--skip NAMES]` (R5).
2. **`scripts/check-frontend-quality.py`**
   - `parse_argv` (:220-234): add `--skip` branch; return 6-tuple.
   - `main` (:243-246): unpack `skip`; union into `scan_directory` call.
   - `main` (:281): `file=` conditional on the OK summary.
   - Update docstring (:23-24) (R5).
3. **`pbatch/health.py`** `_checker_violations` (:43-58): delete special case (:48-50), always append `--skip SKIP`; replace :55-57 with plain `json.loads` + `except ValueError`. Drop the now-obsolete comment at :48 and :55-56.
4. **Tests** — add per §6; run the 4 affected files first.
5. **Gates** — `make ci` (387+ tests + 25 eval) + `python quality.py --strict .`.
6. **Optional R5** — one-line doc note in `AGENTS.md` checker section or docstrings.
7. **No rollout concerns** — pure CLI contract fix inside one repo; no data migration, no dependency changes, no flag removals. `--skip` on the two checkers is *additive*; no existing invocation passes it, so nothing breaks. `health.py` invocation shape is unchanged (`pi-batch health`).

---

## 6. Testable acceptance mapping

All new tests follow existing conventions (subprocess `_run` helper,
`json.loads` assertions, repo-internal fixtures).

| Acceptance | Test (file) | Concrete assertions |
|---|---|---|
| **AC1** — backend `--dir . --json` stdout parses with `json.loads` (no tail lines) | `test_backend_json_stdout_contract` (`tests/test_backend_specs.py`) | Three `clean_repo` fixtures: (a) **ok** — `go.mod` + clean `src/main.go` → exit 0, `json.loads(stdout)` OK, `payload["status"] == "ok"`, `payload["total"] == 0`, `"BACKEND-QUALITY"` not in stdout, `"BACKEND-QUALITY: OK"` in stderr; (b) **skipped** — empty dir, no manifest → exit 0, `payload["status"] == "skipped"`, `"gate skipped"` in stderr; (c) **violation** — `go.mod` + `src/a.go` with `// TODO` → exit 1, `json.loads` OK, `payload["total"] >= 1`, details in stderr |
| **AC2** — backend `--skip generated,migrations` skips excluded dirs | `test_backend_skip_flag` (`tests/test_backend_specs.py`) | Fixture `go.mod` + `src/a.go` + `scratch/b.go` (clean): default `files_scanned == 2`; `--skip scratch` → `files_scanned == 1` (mechanism; `scratch` ∉ DEFAULT_EXCLUDES); `--skip generated,migrations` → `files_scanned == 2` (idempotence — both already in DEFAULT_EXCLUDES :70); `--skip ""` → `files_scanned == 2` (no-op); each invocation exit 0 + `json.loads` OK |
| **AC3** — frontend `--skip e2e` works | `test_frontend_skip_flag` (`tests/test_frontend_quality.py`) | Fixture `src/a.tsx` + `scratch/b.tsx` + `e2e/c.tsx` (each `const x: any = 1`): default `files_scanned == 2` (e2e already excluded :41); `--skip scratch` → 1; `--skip e2e` → 2 (flag accepted, e2e stays excluded — **negative control**: a replace-implementation yields 3); `--skip scratch,e2e` → 1; assert `files_scanned <= 2` every run |
| **AC4** — health.py raw_decode + `--skip` branch removable, `health --json` unchanged | `test_health_checker_plain_json_contract` (`tests/test_tools_health.py`) | `_checker_violations("check-backend-quality")` equals a direct subprocess `--dir ROOT --json --skip SKIP` + `json.loads` total (both 0 on this repo); `health_main(["--json"])` exit 0 + `json.loads` OK + `sorted(report.keys()) == ["checkers","governance","healthy","quality_violations","score","specs","tests"]` + `isinstance(report["checkers"]["backendquality"], int)`; source guard `"raw_decode" not in health.py text` |
| **AC5** — ui-spec `--json` no regression | `test_json_clean_path_stdout_pure` (`tests/test_ui_check.py`) | Clean fixture `--all --json` → exit 0, `json.loads(stdout)` OK, `payload["total"] == 0`, `"UI-SPEC"` summary in stderr; all existing ui-spec tests untouched and green |
| R2 (frontend half of the stdout contract — supplied AC1 lists only backend) | `test_frontend_json_ok_path_pure` (`tests/test_frontend_quality.py`) | Clean fixture `src/clean.tsx` → `--json` exit 0, `json.loads(stdout)` OK, `"UI-QUALITY: OK"` in stderr, no `"UI-QUALITY"` in stdout |
| C1 guard (non-json byte-identical) | `test_backend_non_json_unchanged` (`tests/test_backend_specs.py`) | ok fixture without `--json` → stdout contains `"BACKEND-QUALITY: OK (1 file(s) scanned)"`, no `{` JSON, exit 0 |
| C2 guard (`-o`-only unchanged) | `test_backend_o_flag_summary_on_stdout` (`tests/test_backend_specs.py`) | ok fixture `-o report.json` (no `--json`) → stdout keeps the summary line; report file parses with `json.loads` |
| CI gates | `make ci` + `python quality.py --strict .` | 387+ regression tests + 25 eval + org quality all green |

Estimated new tests: 8 (2 backend contract/guard + 2 backend skip/guards, 2
frontend, 1 health, 1 ui-spec). No existing test asserts the polluted
behavior (verified: `test_frontend_quality.py` only covers the violation
path; `test_tools_health.py` pins nothing about `raw_decode`), so no test
needs modification — only additions.

---

## 7. Scope guard

In: the two checkers' `--skip` + `--json` stdout contract, `health.py` patch
removal, the 4 test files, optional doc note. Out (unchanged): `check-ui-spec.py`,
`check-design-intelligence.py`, `check-knowledge-freshness.py`,
`check-backend-experience.py`, `check-ui-geometry.py`, `advance.py`,
`pi-batch.yaml` validators, argparse migration of `parse_argv`, exit-code
semantics, `DEFAULT_EXCLUDES` contents, `-o` report bytes, human-mode bytes.
