Requirements specification complete and verified.

**Deliverable:** `docs/auto/runs/make-the-backendquality-gate-fail-closed-on-back-02ef859d/artifacts/requirements-10762e10/requirements.md` (187 lines, at the pipeline-expected path for this direction).

**Evidence verification — all 6 citations confirmed against HEAD:**

| Cited | Verified |
|---|---|
| `pi-batch.yaml:176` | ✅ `--dir {cwd}/src` + `-o {cwd}/docs/ui/refactor/backend-quality.json` (uicheck family dir) |
| `scripts/check-backend-quality.py:86-98` | ✅ `scan_directory()` returns `[]` for missing dir (rglob yields nothing, no error) — contrast: positional file targets get a `missing target` check at :354, the `--dir` path doesn't |
| `scripts/check-backend-quality.py:381-384` | ✅ `OK (0 file(s) scanned)` + `return 0` at 384–385 |
| `tests/test_backend_specs.py:92` | ✅ `test_backend_checker_empty_dir_passes` asserts exit 0 |
| `tests/test_validator_registry.py:92-97` | ✅ "this repo has no src/ so the gates must pass" |
| `examples/backend-engineering-review.yaml:8` | ✅ "把 pi-batch.yaml 的 backendquality --dir 改成后端源码根目录" |

**Live reproductions:** missing dir, empty dir, and go.mod-with-0-files all exit 0 with `OK (0 file(s) scanned)` — including the exact acceptance-2 input.

**Key design finding that bounds the whole fix:** the repo root has only `pyproject.toml` — none of the four acceptance-listed manifests — so "no backend manifest → explicit pass (exit 0)" keeps `test_validator_registry.py:92` and `pi-batch health` (exit codes 0/1 only) green without changes. The spec pins the manifest list verbatim, excludes Python detection as a non-goal (adding it would fail-close on this repo itself), and maps each acceptance item to a concrete exit code + message-substring check.
