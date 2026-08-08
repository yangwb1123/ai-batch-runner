All verification complete. Report follows — every claim below was measured on the live tree (2026-08-08, snapshot caveat: five 7×24 campaigns are running and mutating this tree; the check-no-refusal change itself was verified live and is green).

---

# DevOps Engineer Review — ai-batch-runner build/delivery/deployment

## 0. Mandated asset check

**`ops/deploy/` is absent (Verified: `ls ops/` → no such directory, zero files).** This repo has never had the deployment-asset path the review prompt assumes. A prior devops review (`docs/snaplink-platform/phase2/reviews/devops-engineer-review.md:41`) already recorded the same finding — it is a persistent gap, not a regression. The `ops/deploy/k8s-distributed/config.yaml` referenced in `docs/snaplink-platform/v2/reviews/security-review-v2.md` belongs to the **snaplink SDK tree** (`/home/u1/snaplink`), not this repo. Deployability is therefore judged only from what actually exists: `pyproject.toml`, `.github/workflows/ci.yml`, `Makefile`, `docs/RUNNING_247.md`, `tests/test_packaging.py`, `dist/design-intelligence-pack/`.

## 1. Supported deployment-path and artifact inventory

| Path | Status | Evidence |
|---|---|---|
| **Repo-root execution** (`python pi-batch.py …` from this tree) | **Only fully-supported mode today** | Validators resolve `{cwd}/scripts/*`, `{cwd}/quality.py` (`pi-batch.yaml`, 24 entries, counted via `yaml.safe_load`); `config.TOOL_ROOT = pbatch/../..` exists but `{cwd}` is used in 13 of 24 entries |
| **pip / pipx / uv install** (`[project.scripts] pi-batch = pbatch.cli:main`) | **Partial — documented as deferred** | Wheel/sdist ship only `pbatch*` + 3 data files (`pyproject.toml` package-data; `tests/test_packaging.py` loader-audit invariant). `scripts/`, `quality.py`, `pi-batch.yaml`, `prompts/`, `pipelines/`, specs are **not shipped** → `refusalcheck`/`completion`/`pyquality`/UI gates fail closed in a pip-only consumer. Docstring names this deferred: "`{tool_root}` validators + rule_matcher registries + examples/ pipelines (owner: direction #2, support-matrix decision U1)" |
| **CI** (`.github/workflows/ci.yml`, PR + push to main, ubuntu-latest, Py 3.11) | Runs G1–G4, self-test, test, eval, coverage | **Omits `make quality` + `make org`**; never runs `make ci` aggregate; `pip install -e ".[dev]"` only (editable, artifact never built) |
| **dist/design-intelligence-pack/** | Manual export only | Built by `pbatch/export_template.py` (default out dir); `VERSION.json` embeds **absolute machine paths** (`/home/u1/ai-batch-runner/…`); not CI-built, not checksummed; `checkers: []` while scripts ship |
| **systemd / nohup operation** | Guidance only, not assets | `docs/RUNNING_247.md` has an example `.service` unit, cron/timer pattern, log rotation (5MB×3), exit codes 0/1/3/4/5/130, T4 lock, approval stages, webhook — but **no versioned unit files/env templates anywhere in the repo** |
| **Runtime provenance** | Present, tool-internal | Campaign start records `TOOL_VERSION` event (code digest + tool HEAD); `--reuse-fingerprint` sha256 sidecars; `pi-batch health` → **Verified live: HEALTHY 100.0 (A)** |
| **Gate/selfcheck of the reviewed change** | **Verified live** | `pi-batch.py check --steps refusal --skip-eval` → exit 0, `CHECK [refusal]` ×6 lines, "all 1 gates passed"; `scripts/check-no-refusal.py` tracked and byte-clean; examples/ duplicate removed (git ls-files shows only the canonical copy); design doc at `docs/requirements/scripts-check-no-refusal-design.md` |

## 2. Pipeline table

| Stage | Current evidence | Gap | Proposed gate | Owner |
|---|---|---|---|---|
| Local gates | `make check/self-test/quality/org`; `.githooks` pre-commit (filesize+style) / pre-push (`cli.py harness`) | **`make check` is RED: `pbatch/cli_batch.py` 501>500 lines (untracked, verified `git ls-files` empty, `cli.py check-filesize` FAIL)**; pre-push runs only harness, not quality/org/test | Commit-or-refactor cli_batch.py; align pre-push with `make ci` | devops (agent-engineering/release.yaml, stage 14) |
| CI | workflow G1–G4 + self-test + test + eval + coverage | `quality` (G5) + `org` never run in CI; CHECKS_REGISTRY.md claim "CI 与 pre-push 执行此集" is **inaccurate** (CI ≠ `make ci` in both directions: CI adds eval/coverage, misses quality/org) | Add `make quality` + `make org` steps or single `make ci` step; fix registry doc | devops |
| Packaging | `tests/test_packaging.py` sdist proxy runs inside `make test` → CI (Verified: file exists, builds from isolated tmp copy, exact-member assertions) | **Wheel never built/verified**: `test_wheel_ships_data_files` skips (`wheel` absent from dev extras — pyproject confirms); no install smoke; no version/tagging (direction #3 deferred) | Add `wheel` to dev extras; CI: build wheel + fresh-venv install + smoke `check --steps refusal`; adopt U1 support-matrix decision | devops |
| Artifact/provenance | `dist/design-intelligence-pack/VERSION.json` (exported_at 2026-08-06, absolute paths) | Manual export, no CI job, no sha256/git-sha manifest, `checkers: []` metadata vs shipped scripts | CI export job + provenance manifest; or stop treating pack as deliverable | devops |
| Deploy/ops assets | RUNNING_247.md guidance; **no `ops/deploy/`** | No versioned unit files/env templates (drift un-auditable — prior review P3 already flagged); no backup/restore for `.pi-batch/`, `docs/auto/state.jsonl`, session JSONL | `ops/deploy/systemd/*.service` + env templates, `systemd-analyze verify`, backup/restore procedure doc | devops |
| Health/monitoring | `pi-batch health` (Verified: 100.0 A), log rotation, webhook + events-file (T7) | **Health score ignores own hard gates**: reports A/100 while `make check` fails (health gates = designintelligence/knowledge/backendquality only; quality.py budget is 1000 lines/file vs checks' 500) | Add gate-aggregate to health or document scope; wire webhook to alerting | devops / sre |
| Regression | ~990 tests collected (`health`), 960 passed/21 failed/1 skipped (qa_lead measurement) | 21 pre-existing failures (`test_pi_batch.py`/`test_local_fixes.py`) reproduce under any live campaign lock (T4, exit 5); `make ci` blocked on this host | Suite lock isolation (`--no-lock`/isolated state dir) — qa_lead H-1, separate direction | devops |

## 3. Findings (severity-sorted)

**H-1 — Repo's own gate set is red on the current tree.** Location: `pbatch/cli_batch.py` (501 lines, untracked) fails G1 filesize (`make check` → Error 1, verified). Production impact: no release claim can stand on a tree that fails its own first gate; the tool's dogfooding story (gate before promote) is violated. Remediation: commit or split the module (it is a documented dogfooding output per AGENTS.md). Validation: `make ci` green on a lock-free host.

**H-2 — CI is not the declared gate set (gate drift).** Location: `.github/workflows/ci.yml` vs `Makefile ci:` (`check self-test test quality org`). CI omits `quality` + `org`; CHECKS_REGISTRY.md ("CI 与 pre-push 执行此集") and AGENTS.md ("make ci = the full gate set (what CI runs)") are both inaccurate. Impact: org-spec integrity (17 nodes), capability registry, and graph extraction can silently rot while CI is green; pre-push is weaker still (harness only). Remediation: run `make ci` in CI (or add the two missing targets) and fix the docs. Validation: break a spec node → CI must fail.

**H-3 — pip artifact is incomplete and unverified; pip-installed CLI cannot run its own validator registry.** Location: `pyproject.toml` (packages.find `pbatch*`, package-data 3 files), `pi-batch.yaml` (13 `{cwd}/scripts…` validators), `tests/test_packaging.py:212` (wheel test skip). Impact: `pip install .` in an empty project yields a `pi-batch` whose `refusalcheck`, `completion`, `pyquality`, UI/backend gates and default pipelines all fail closed on missing files — or, worse, a user infers deployability and gets broken gates in production. This is a **documented decision (U1) with recorded owner**, so severity is bounded — but it blocks any "pip install is a supported deployment path" claim. Remediation: U1 resolution; add wheel to dev extras + CI wheel-build/install smoke; ship scripts/ under `{tool_root}` or package as data. Validation: `pip wheel .` → fresh venv → `pi-batch check --steps refusal` in a non-repo cwd → must fail closed with a clear diagnostic, and CI must pin that behavior.

**M-1 — Machine-specific absolute path in committed config.** Location: `pi-batch.yaml` `completion_go: python /home/u1/ai-batch-runner/scripts/check-completion-report.py {output}` (verified). Impact: breaks on any other host/CI runner; the `{tool_root}` mechanism exists (config.py:48, runner.py:487/698, pipeline.py:633) and is unused here. Remediation: `{tool_root}/scripts/check-completion-report.py`. Validation: grep pi-batch.yaml for `/home/` → zero hits.

**M-2 — No release/version identity or reproducible build.** Verified: zero git tags; `version = "0.1.0"` hardcoded; deps unpinned (`PyYAML>=6.0`, `pytest>=8.0`); no lockfile; CI on floating `ubuntu-latest` + fixed-ish 3.11. Runtime provenance exists (TOOL_VERSION digest), but there is no build-time provenance. Remediation: tag-based releases, `git describe`-derived version, constraints file, pin CI image. Validation: two builds from same commit produce identical sdist (compare hashes).

**M-3 — No CI secrets/SCA scanning.** Verified: `.github/` contains only `workflows/ci.yml`; no dependabot, gitleaks, pip-audit. Compensating control exists at artifact time (T12f `secretscan` grep validator, verified in registry) but nothing scans the repo/CI supply chain. Remediation: pip-audit step + dependabot or gitleaks. Validation: inject a known-bad token into a test commit → CI fails.

**M-4 — Operational assets not versioned; no backup/restore.** Verified: no `ops/deploy/`; the only unit file is a doc example; state (`docs/auto/state.jsonl`, `.pi-batch/memory/*.jsonl`, session JSONL) is append-only by design but has no backup/restore procedure. 7×24 campaigns are the production workload — a disk failure loses run state with no documented recovery. Remediation: `ops/deploy/` with units + env templates (prior review P3 agrees), backup procedure, restore drill. Validation: `systemd-analyze verify` on versioned units; restore drill on a copy.

**L-1 — `dist/design-intelligence-pack/VERSION.json` weak provenance.** Absolute source paths, no checksums, `checkers: []` while scripts ship. Remediation: CI export + sha256/git-sha manifest or retire. Validation: manifest verifies.

**L-2 — Health score overstates health.** Verified live: `pi-batch health` → HEALTHY 100.0 while `make check` fails. Remediation: include G1/G5 or label scope. Validation: health drops below A when filesize gate fails.

**Open tracked items (from the reviewed direction's own QA):** `--steps " "` whitespace-only no-op still exits 0 (qa_lead M-2, reproduced live by qa_lead) and marker-rewrite drift is unpinned (M-1) — both belong to the selfcheck/health path this review covers; they are advisory-conditional acceptance items, not blockers for the deployment story itself.

## 4. Release and rollback procedure (proposed — none exists today)

**Preflight (lock-free host, since T4 lock breaks pipeline-mode tests):**
1. `make ci` green (H-1 cleared) + `quality.py --strict .` green; record Python version, PyYAML version, `git rev-parse HEAD`.
2. `make test` + `make eval` green (eval additionally in CI).
3. Build: `pip wheel .` after adding `wheel` to dev extras; install into a fresh venv; smoke `pi-batch check --steps refusal --skip-eval` (expect exit 0) and `pi-batch health` (expect HEALTHY) from repo root.
4. Confirm `git diff scripts/check-no-refusal.py` empty (NG1 byte-identity, if the refusal change is in the release).

**Migration:** this tool has **no schema migrations** — state is append-only JSONL (`docs/auto/state.jsonl`, memory index, daily-budget, session JSONL). The compatibility contract is: new versions must append, never rewrite; the `TOOL_VERSION` event (digest + HEAD) invalidates stale reuse fingerprints on upgrade (documented in RUNNING_247.md) — this is the correct mechanism and needs no DB step.

**Deploy:** copy repo to target path (repo-root mode is the supported path; pip mode awaits U1); systemd unit `Type=simple`, `WorkingDirectory=<project>`, `Restart=always`, `RestartSec=30`, log to a rotating file (built-in 5MB×3); single-instance lock is the admission control — never run two instances against one worktree (exit 5 is the correct refusal; use `--wait-lock` for queuing, separate worktrees for parallelism).

**Probe (post-start):** `pi-batch check` (default set, expect "all 8 gates passed"), one smoke task through a real agent with a validator attached (e.g. `refusalcheck`), verify `--reuse` revalidates a prior artifact (T1 contract) — this is the readiness probe; there is no other.

**Post-deploy checks:** tail log for exit codes (0/1/3/4/5/130) and validator signatures; watch `.pi-batch/` growth and disk; quota/rate-limit rejection counts (should be rejected, never persisted); webhook/events-file if configured.

**Rollback:** revert tree to previous commit (the reviewed design's own migration discipline — independent commit-sized hunks — applies); restart service; prior artifacts remain on disk and are revalidated by the old code's validators before reuse (T1), so rollback is file-level and safe. No data migration exists to undo. **Multi-replica/canary: explicitly not applicable and not recommended** — the tool is a single-writer-per-worktree system by design (O_EXCL lock, shared hot state = worktree + lock; parallel work is isolated via campaign worktrees/branches). Blue/green here means two worktrees, which is already the supported parallelism model; canary adds nothing to a deterministic, stateless-per-invocation gate.

## 5. Missing assets / operator decisions blocking a production claim

1. **`ops/deploy/`** — the prompt-specified path has zero files; no versioned service units, env templates, or IaC of any kind (blocking).
2. **CI gate parity** — quality + org absent from CI; CHECKS_REGISTRY/AGENTS.md claims inaccurate (blocking a *reliable* green signal, not the code).
3. **Wheel build/install verification + U1 support-matrix decision** — pip-install remains a partial deployment path with a recorded owner but no decision (blocking any pip deployment claim).
4. **Release identity** — no tags, no versioned artifact provenance, no reproducible environment pins (blocking release claims).
5. **Backup/restore procedure** for `.pi-batch/`, `docs/auto/state.jsonl`, session logs (blocking 7×24 production-operations claims).
6. **Secrets/SCA scanning in CI** (blocking supply-chain claims; partially compensated by artifact-time `secretscan`).
7. **Health-probe scope honesty** — `pi-batch health` A/100 coexists with red `make check`; an operator-facing score must aggregate or label its gates (blocking operator trust in the probe).
8. **Selfcheck no-op residue** (`--steps " "` → "all 0 gates passed", qa_lead M-2) and marker-drift pin (qa_lead M-1) — open advisory items on the exact change this batch shipped; both are 3-line fixes already specified by QA.

**Bottom line:** the reviewed change (refusalcheck gate + registry + selfcheck step) is verified live, byte-clean on its frozen script, and its validator chain works end-to-end. The deployment story of the *tool itself* is where the production claim fails: no `ops/deploy/`, no CI parity with `make ci`, no wheel verification, no release identity, red local gates — each fix is small and owner-mapped above; none of them touches the reviewed feature's blast radius.
