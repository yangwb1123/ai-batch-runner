# Requirements Specification — check-no-refusal: Regression Coverage + Duplicate Elimination

- **Module**: `scripts/`
- **Direction**: "Add regression tests for the fail-closed refusal gate check-no-refusal.py and eliminate its drifting duplicate copy"
- **Source analysis**: `docs/auto/analyses/scripts-8f645734.json`
- **Date**: 2026-08-08
- **Status**: Proposal — evidence-backed; subject to reviewer confirmation before implementation

---

## 1. Evidence Verification

Every claim in the direction was re-verified against the repository on 2026-08-08. All cited evidence is **confirmed**:

| # | Claim | Verification result | Evidence |
|---|-------|---------------------|----------|
| E1 | `scripts/check-no-refusal.py` implements a fail-closed refusal gate | **Confirmed**. Exit 0 + `REFUSAL-CHECK: OK` only for non-empty, marker-free artifacts; exit 1 for missing file / empty (whitespace-only) output / any of 10 refusal markers (`REFUSAL-CHECK: output refuses to work (markers: …); failing closed`); exit 2 for no-arg usage. | `scripts/check-no-refusal.py:1-44` |
| E2 | Second byte-identical copy exists at `examples/snaplink-platform/scripts/check-no-refusal.py` | **Confirmed**. `diff` reports **IDENTICAL**; both files 1414 bytes. | `examples/snaplink-platform/scripts/check-no-refusal.py` |
| E3 | Zero test coverage — no reference in `tests/` or `checks/` | **Confirmed**. `grep -rn "no_refusal\|no-refusal" tests/ checks/` → no matches. | grep (exit 1) |
| E4 | Not registered in the `pi-batch.yaml` validator registry | **Confirmed**. Registry (lines 122–180) contains 23 entries (`quick`, `gofmt`, `build`, `config`, `root`, `pyquality`, `designintelligence`, `backendexperience`, `knowledge`, `secretscan`, `gobuild`, `gotest`, `gorepo`, `clirepo`, `completion_go`, `uispacing`, `uicolor`, `uistyle`, `uicheck`, `uigeometry`, `uiquality`, `backendquality`, `completion`) — no check-no-refusal entry. *(Direction cited `pi-batch.yaml:128`; that line is a comment inside the validators section — the substance of the claim holds.)* | `pi-batch.yaml:122-180` |
| E5 | Gate is load-bearing via `validate_cmd` in example pipelines | **Confirmed**. 8 references across 4 pipelines, all `{cwd}/scripts/check-no-refusal.py`: `audit-revision-pipeline.yaml:37,56,72,93,112` (5 stages), `audit-fix-pipeline.yaml:38`, `requirement-pipeline.yaml:44`, `audit-v21-pipeline.yaml:41`. | 4 pipeline files |
| E6 | A broken marker list would propagate fake PASS artifacts | **Confirmed by construction**. `{cwd}` substitutes the task/stage working dir (`pbatch/runner.py:487`, `pbatch/pipeline.py:604`); stages in these pipelines set no `cwd`, so `{cwd}` = process cwd. README documents running them from the repo root → resolves to the canonical copy. A marker typo would silently let refusal artifacts pass. | `examples/snaplink-platform/README.md`; `pbatch/runner.py:487` |

**Additional verified facts relevant to the fix** (not in the original direction):

- **F1** — `pi-batch check` (`pbatch/selfcheck.py:39-55`) currently runs steps `quality, registry, org, capabilities, graph, tools, knowledge, eval` and does **not** enumerate the validator registry. The acceptance criterion "listed by `pi-batch check`" therefore requires a small selfcheck extension.
- **F2** — `pbatch/config.py:419-430` exposes `VALIDATORS = _load_validators()` (loaded from pi-batch.yaml at import), giving tests a stdlib-importable way to assert registration.
- **F3** — The 10 refusal markers: `"输入缺失", "未指定", "输入未提供", "无法继续", "FAILED — 输入", "FAILED — 输入缺失", "input missing", "no input provided", "cannot proceed", "上游能力未映射"` (`scripts/check-no-refusal.py:14-20`). Note `"FAILED — 输入"` is a substring of `"FAILED — 输入缺失"` — harmless, no behavior impact.
- **F4** — `requirement-pipeline.yaml:44` uses `python {cwd}/…` while the other three pipelines use `python3 {cwd}/…` — a latent interpreter inconsistency (see §6 note).
- **F5** — README (`examples/snaplink-platform/README.md` 快速开始) instructs running these pipelines from the main repo root, i.e. `{cwd}` resolves to the canonical copy. The example copy is reachable **only** if a pipeline is run with cwd inside `examples/snaplink-platform` — a documented-never case, making deletion safe.
- **F6** — Tests convention: `tests/` is a pytest suite (`make test` = `python -m pytest tests/`, 387 cases; `pyproject.toml` testpaths `["tests", "checks"]`); subprocess-style tests exist (`tests/test_entrypoints.py` uses `subprocess.run`). `quality.py --strict .` budget (file ≤1000 lines, function ≤50 lines, complexity ≤15) applies to the new test file and any selfcheck edit.

---

## 2. Problem Statement (verified)

`scripts/check-no-refusal.py` is the fail-closed guard that converts an LLM's honest "cannot proceed" artifact (missing input, upstream failure) into a gate failure instead of a PASS. It is load-bearing for 8 `validate_cmd` sites in 4 snaplink example pipelines. Today:

1. It has **zero regression tests** — a typo in the marker list, a broken exit-code branch, or a purged marker is undetectable.
2. It exists as a **byte-identical second copy** under `examples/snaplink-platform/scripts/` with no documented consumer (pipelines resolve `{cwd}/scripts/…` to the canonical copy when run per README) — a drift trap.
3. It is **not registered** in the `pi-batch.yaml` validator registry, so it is invisible to `pi-batch check` and to the registry-based `validate:` mechanism.

---

## 3. Goals and Non-Goals

### Goals
- G1. Make every observable behavior of the gate a regression test (markers, empty, missing, clean, usage).
- G2. Eliminate the drift hazard of the duplicate copy (one canonical source of truth).
- G3. Register the gate in the validator registry and surface it in `pi-batch check`.

### Non-Goals (explicitly out of scope)
- NG1. No change to the gate's marker list, exit codes, or detection semantics. The script's contract is frozen by this spec; the tests pin it.
- NG2. No changes to any validator other than the addition described in FR-3. In particular, the pre-existing stale `config: "python cli.py config-validate"` entry (no such command exists in `cli.py`) is **not** in scope.
- NG3. No rewiring of the example pipelines' stage logic, prompts, or outputs. Their `validate_cmd` strings are touched only as needed by FR-2.
- NG4. No changes to gate behavior of other checkers, no new marker detection heuristics.

---

## 4. Requirements

### FR-1 — Regression suite for the gate (`tests/test_no_refusal.py`)

**Requirement.** Add `tests/test_no_refusal.py` to the pytest suite (`make test` / `make ci`), exercising the gate as a black box via subprocess: `[sys.executable, "scripts/check-no-refusal.py", str(path)]` with cwd = repo root.

**Acceptance (derived verbatim from direction acceptance #1).** The test file asserts:

- **A1.1** — exit code **1** for an artifact containing **each** marker in the gate's marker list. The marker list must be derived from the script's source (`REFUSAL_MARKERS` via `runpy.run_path`), not hard-coded, so the test auto-syncs with the list; a parametrized case per marker, each fixture containing exactly one marker (plus surrounding prose so the file is otherwise clean).
- **A1.2** — exit code **1** for an **empty** output file, and for a **whitespace-only** output file (both hit the `text.strip()` empty branch).
- **A1.3** — exit code **1** for a **missing** file (asserts the `artifact missing` branch).
- **A1.4** — exit code **0** for a **clean** artifact (non-empty, marker-free prose).
- **A1.5** — exit code **2** for the no-argument usage invocation (pins the usage branch of the contract).

**Additional testable constraints (same file):**

- **A1.6** — the derived marker list has **≥ 10 entries** (guards against accidental marker purging, which A1.1 alone cannot detect since parametrization shrinks silently).
- **A1.7** — every refusal output (`exit 1`) contains `REFUSAL-CHECK` (pins the diagnostic contract that pipeline logs rely on).

### FR-2 — Single canonical copy (eliminate the drifting duplicate)

**Requirement.** Eliminate `examples/snaplink-platform/scripts/check-no-refusal.py` (verified byte-identical, no documented consumer; E2, F5). The canonical copy remains `scripts/check-no-refusal.py`.

**Acceptance (derived verbatim from direction acceptance #2 — this spec selects the deletion branch; the fallback branch is defined in §6.1 in case reviewers reject deletion):**

- **A2.1** — a test asserts `examples/snaplink-platform/scripts/check-no-refusal.py` **does not exist**.
- **A2.2** — a test asserts every `check-no-refusal` reference in the four example pipelines (`audit-revision-pipeline.yaml`, `audit-fix-pipeline.yaml`, `requirement-pipeline.yaml`, `audit-v21-pipeline.yaml`) is a `validate_cmd` of the form `… {cwd}/scripts/check-no-refusal.py {output}` — i.e., resolves to the canonical copy whenever the pipeline is run from the repo root (the documented invocation mode, F5). The test parses the YAML files and greps the `validate_cmd` strings; **8 references total**.
- **A2.3** — after deletion, `grep -rn "check-no-refusal" examples/` matches only the pipeline `validate_cmd` strings (no remaining script file anywhere under `examples/`).

### FR-3 — Registration in the validator registry

**Requirement.** Register the gate as a named validator in `pi-batch.yaml`:

```yaml
validators:
  # …
  refusalcheck: "python {cwd}/scripts/check-no-refusal.py {output}"
```

(Entry name `refusalcheck`; style follows the existing `completion`/`pyquality` entries. The canonical script path is mandatory — never a path under `examples/`.)

**Acceptance (derived verbatim from direction acceptance #3, first half):**

- **A3.1** — a test asserts `"refusalcheck" in pbatch.config.VALIDATORS` (the registry loader at `pbatch/config.py:419-430`).
- **A3.2** — the same test asserts the registered command references `scripts/check-no-refusal.py` and not any path under `examples/`.

### FR-4 — Gate listed by `pi-batch check`

**Requirement.** Extend `pbatch/selfcheck.py` so `pi-batch check` runs a `refusal` step (name registered in `_default_steps`, filterable via `--steps refusal`) that:

1. Verifies the `refusalcheck` registry entry exists in `pi-batch.yaml` (load via `pbatch.config.VALIDATORS`);
2. Verifies the canonical script file exists;
3. Smoke-runs the gate: a temp clean artifact must exit 0, a temp artifact containing a marker must exit 1, a missing path must exit 1 — and prints a per-check `CHECK [refusal] …` line (same reporting style as the existing `_run_step`).

**Acceptance (derived verbatim from direction acceptance #3, second half):**

- **A4.1** — `python pi-batch.py check --steps refusal --skip-eval` exits **0** and its output contains a `CHECK [refusal]` line.
- **A4.2** — a test in `tests/test_no_refusal.py` invokes that exact command via subprocess and asserts exit code 0 and the `CHECK [refusal]` output (end-to-end proof that the gate is "listed by `pi-batch check`"; mirrors the subprocess style of `tests/test_entrypoints.py`).
- **A4.3** — a deliberate failure mode is visible: if `refusalcheck` were removed from `pi-batch.yaml`, the step (and therefore A4.2) must fail — the step must be fail-closed, never a no-op print.

### DOC-1 — Documentation count stays truthful

**Requirement.** `AGENTS.md` §组件清单 describes `pi-batch.yaml` as "6 个验证器（含 `pyquality`）". After FR-3 the count changes; update that line to reflect the new count and mention `refusalcheck`. No other documentation changes are required (historical docs `docs/LESSONS.md`, `docs/RETRO-2026-08.md`, `docs/snaplink-platform/PLATFORM_MAINTENANCE_GUIDE.md` are retrospective records — leave untouched).

**Acceptance.**

- **A5.1** — the count in `AGENTS.md` matches the actual number of entries in `pi-batch.yaml` `validators:` (testable by parsing both files; the existing test suite runs `quality`/`org` gates that re-read AGENTS.md but a dedicated assertion is not required — a reviewer read-through suffices for this one line).

---

## 5. Concrete Test Matrix (target for `tests/test_no_refusal.py`)

| Test | Fixture | Expected |
|---|---|---|
| `test_clean_artifact_passes` | non-empty prose, no markers | exit 0, output contains `REFUSAL-CHECK: OK` |
| `test_marker_refuses` (parametrized × all markers, derived from source) | one marker + surrounding prose | exit 1, output contains `REFUSAL-CHECK` and `failing closed` |
| `test_marker_list_not_purged` | — | `len(REFUSAL_MARKERS) >= 10` |
| `test_empty_output_refuses` | empty file | exit 1, `empty output` |
| `test_whitespace_only_refuses` | `"\n\t  \n"` | exit 1, `empty output` |
| `test_missing_file_refuses` | nonexistent path | exit 1, `artifact missing` |
| `test_usage_without_args` | no args | exit 2, `usage:` |
| `test_no_duplicate_copy` | — | A2.1 (example copy absent) |
| `test_pipelines_reference_canonical_copy` | — | A2.2 (8 references, `{cwd}/scripts/check-no-refusal.py`) |
| `test_registered_in_validators` | — | A3.1 + A3.2 (`refusalcheck` in `VALIDATORS`, canonical path) |
| `test_pi_batch_check_lists_gate` | — | A4.2 (`pi-batch check --steps refusal --skip-eval`, exit 0, `CHECK [refusal]`) |

All subprocess calls bounded (timeout ≤ 120 s, matching `tests/test_entrypoints.py`). Temp files via pytest `tmp_path`. No network, no LLM, no dev-dependencies beyond pytest (already required by the suite).

---

## 6. Implementation Notes (non-binding guidance)

- **6.1 Fallback for FR-2 if reviewers reject deletion.** If the maintainers decide the example copy must stay (e.g., the example directory is treated as a self-contained template), replace A2.1 with a **byte-identity** test: `scripts/check-no-refusal.py` and `examples/snaplink-platform/scripts/check-no-refusal.py` must be identical (`Path.read_bytes()` equality). Exactly one branch (deletion XOR identity) must be chosen and the test file documents which. This spec recommends the deletion branch (E2/F5: zero documented consumers; the pipelines' `{cwd}` resolution already targets the canonical copy).
- **6.2 `requirement-pipeline.yaml:44` uses `python` while the other three pipelines use `python3`.** One-token normalization to `python3` is recommended while FR-2 touches these files; it is optional and does not affect acceptance.
- **6.3 `{cwd}` resolution.** `pbatch/runner.py:487` substitutes `{cwd}` with `task.workdir()`; stages without `cwd:` use `os.getcwd()` (`pbatch/pipeline.py:604`). Any future invocation of the example pipelines from inside `examples/snaplink-platform/` would resolve `{cwd}/scripts/…` to a now-deleted path — this is the documented-never case (F5); no code change is required.
- **6.4 Optional dogfooding (not required).** The example stages could later switch from raw `validate_cmd:` to `validate: refusalcheck` to demonstrate the registry mechanism; excluded from acceptance to keep scope minimal.
- **6.5 Quality gates.** The new test file and the `selfcheck.py` edit must pass `quality.py --strict .` (function ≤ 50 lines, file ≤ 1000 lines) and `make ci`.

---

## 7. Verification Plan

```bash
# 1. Targeted regression suite
python -m pytest tests/test_no_refusal.py -v

# 2. Full CI (387 + new cases)
make ci

# 3. Self-check lists the gate
python pi-batch.py check --steps refusal --skip-eval   # exit 0, "CHECK [refusal]"

# 4. Organization quality budget
python quality.py --strict .
```

**Definition of Done**: all tests in §5 pass; `pi-batch check` output includes the `refusal` step; `grep -rn "check-no-refusal" examples/` returns only pipeline `validate_cmd` strings; `AGENTS.md` validator count updated; no marker/exit-code behavior of the script changed.

---

## 8. Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Deletion of example copy breaks an undocumented invocation from inside `examples/snaplink-platform/` | Low (README documents repo-root invocation; grep shows no other consumer) | A2.2 pins the reference form; §6.1 fallback (identity test) preserves both copies if reviewers disagree |
| Test suite grows brittle via hard-coded marker list | Medium | A1.1 derives markers from script source; A1.6 pins the minimum count |
| `selfcheck` step becomes a no-op print | Low | A4.3 demands fail-closed behavior; A4.2 exercises it end-to-end in CI |
| AGENTS.md count drifts again | Low | Single line, one-time fix (DOC-1) |
