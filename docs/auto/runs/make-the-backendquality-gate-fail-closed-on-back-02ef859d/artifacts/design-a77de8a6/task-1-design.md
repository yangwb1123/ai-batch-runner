Design complete — written to the pipeline's design-stage path `docs/auto/runs/make-the-backendquality-gate-fail-closed-on-back-02ef859d/artifacts/design-a77de8a6/task-1-design.md`.

## Verification result

All 6 evidence citations confirmed against HEAD, plus live reproductions (missing dir / empty dir / go.mod-only all exit 0 today). Two new facts shaped the design:

- **`--dir {cwd}` with today's union extensions scans this repo itself and finds 21 violations** (god-file/method-count warnings) — so manifest-driven *language restriction* is mandatory, not optional; a naive root-scan change would break `test_validator_registry.py`, `pi-batch health`, and the repo's own CI.
- **No stray manifests in any ancestor** of the repo root — bounded walk-up detection is safe here.

## Design summary

- **Script API** (`scripts/check-backend-quality.py`): `--dir` mode becomes manifest-aware — missing dir → exit 1 (`scan error: directory not found`); manifest (go.mod/Cargo.toml/pom.xml/package.json, walk-up ≤6 levels, fixed precedence) + 0 files → exit 1 (`manifest X found but 0 backend files scanned`); manifest + files → scan only the manifest's language (`.go`/`.rs`/`.java,.kt`/`.ts,.tsx,.js`); no manifest → exit 0 with explicit `no backend manifest detected; gate skipped` signal. New opt-in `--manifest <name>` flag (unknown names fall back to union exts, still fail-closed). Positional-file mode, exit codes {0,1}, and all JSON fields byte-compatible — additive `manifest`/`scan_error`/`status` fields only.
- **Registry** (`pi-batch.yaml:176`): `--dir {cwd}` (root resolved from layout) + `-o {cwd}/docs/backend/backend-quality.json` (out of `docs/ui/refactor/`).
- **Callers**: health.py, advance.py, org gates (agent-engineering yamls) need zero code changes — compatibility matrix with 8 invariants; one pre-existing advance.py `json.loads` trailing-line limitation noted but deliberately out of scope.
- **8 failure modes** (stray ancestor manifest, monorepo precedence, frontend-only package.json false-close, report feedback loop, budget slip…) each with mitigation.
- **6 migration steps**, each gated, ending in `make ci` + `quality.py --strict` + `pi-batch health` + `pi-batch check`.
- **Acceptance mapping**: AC-1…AC-5 bound to exact commands, exit codes, stderr substrings, and 10 named regression tests; guardrails pin Python detection out of scope (opt-in only).
