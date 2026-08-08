All facts verified. Writing the review.

---

# DevOps Engineer Review — ai-batch-runner build/delivery/deployment

## Evidence-conflict note (README rule 5)

The `task-1-design` evidence injected into this prompt is the **backendquality** design (`docs/auto/runs/make-the-backendquality-gate-fail-closed-on-back-02ef859d/artifacts/design-a77de8a6/task-1-design.md`). The **actual design staged by this run's pipeline** is the close-wheel CI-gap design (`docs/auto/runs/close-wheel-testing-ci-gap-and-clean-up-stale-bu-ab5559b2/artifacts/design-a77de8a6/task-1-design.md`, verified via `pipeline.yaml` `from_outputs: design` and the run's `DECISIONS.md`). I review the staged design; the injected one is flagged because the backendquality registry change (`pi-batch.yaml:176` → `--dir {cwd}` + `docs/backend/backend-quality.json`) is **already present in the working tree, uncommitted** — relevant to finding H-4.

## Checks that actually ran (this session, HEAD `75a7a27` 2026-08-08 07:01:35 -0800)

`make ci` in the live worktree (**21 failed / 995 passed**); `pytest tests/test_pi_batch.py` twice (**13 failed**, reproducible); `make ci` equivalent at **clean HEAD in an isolated worktree** (1 test failure — quality-gate assert; `tests/test_pi_batch.py` 100% pass there); `cli.py check`, `quality.py .`, `pi-batch check` (9 gates pass), `pi-batch health`, `pi-batch org check`, `pbatch eval` (42 pass), packaging suite (`7 passed` with wheel 0.47/build 1.5 present; `6 passed, 1 skipped` otherwise — reproduced both ways), git remotes/tags/status/ignored-tree audit, `docs/RUNNING_247.md` template audit, deep scan for IaC manifests.

## 1. Supported deployment-path and artifact inventory

Deployment model: **pip-installable Python CLI** (`[project.scripts] pi-batch = "pbatch.cli:main"`, `pyproject.toml`). No service, daemon, container, or hosted runtime.

| Artifact / path | Status (Verified this session) |
|---|---|
| `ops/deploy/` | **Missing** — no `ops/` directory exists at all (prompt-claimed location; nothing to verify) |
| Wheel / sdist of ai-batch-runner | **Not produced anywhere** — `dist/` holds only `design-intelligence-pack/` (knowledge pack, gitignored); no `python -m build` in CI or scripts |
| Git tags / releases | **None** (`git tag -l` empty); version hardcoded `0.1.0` (`pyproject.toml:7`); no `__version__` in `pbatch/` |
| CI | `.github/workflows/ci.yml` (only workflow; no `timeout-minutes`/`concurrency`): PR + push-to-main, Python 3.11, gates G1–G4 + self-test + test + eval + coverage. **Never runs `quality` or `org`** |
| Publish (PyPI/TestPyPI) | **Missing** — no publish workflow, no trusted publishing |
| Delivery remotes | `origin` (Gitea) + `github` — the `github` remote URL embeds a **live PAT in `.git/config`** (see H-2) |
| Runtime deployment assets | Docs only: nohup + systemd template in `docs/RUNNING_247.md:428-463`; **stale path** `WorkingDirectory=/home/u1/workspace/demo/snaplink` (`RUNNING_247.md:438`); no unit/timer files shipped |
| IaC (Docker/k8s/helm/terraform) | **None** (searched, excluding `examples/` and other projects — per instruction, example manifests are **not** deployability evidence) |
| State to protect | `.pi-batch/memory/sessions.index.jsonl` (395 KB), `docs/auto/state.jsonl`, `docs/archive/` — append-only JSONL; **no backup/restore strategy documented** (`backup` appears in `RUNNING_247.md` only as `--log-backups`) |
| Current tree hygiene | Dirty: **47 modified, 169 untracked, 19 deleted** files vs HEAD; see H-4 |

## 2. Pipeline table

| # | Stage | Current evidence | Gap | Proposed gate | Owner |
|---|---|---|---|---|---|
| 1 | Source control | githooks pre-commit/pre-push; `git_commit: true` per stage | No tag/branch policy, no signed releases; **live PAT in remote URL (H-2)**; 19 uncommitted deletions of run archives | Credential hygiene check in pre-push; signed tags for releases | Maintainer |
| 2 | Engineering gates G1–G4 + self-test | CI runs them — Verified | None for these gates | Existing | Maintainer |
| 3 | Regression suite | CI runs `make test`; HEAD suite green **only in lock-free env** | **CLI subprocess tests fail under the repo's own 7×24 lock (H-3)**; wheel test silently skips without dev-extras fix (this design's core, R1) | Test isolation (`--no-lock`/lock-free cwd); dev extras `wheel>=0.42 build>=1.0` (validated: 7 passed) | Maintainer (campaign implementer) |
| 4 | Quality + org | `make quality`/`org` exist; **absent from CI workflow**; HEAD fails quality (iris-ui, 3 violations, C-1) | Red at HEAD; drift both directions (`make ci` omits eval/coverage) | CI runs `make ci` + eval + coverage; fix iris-ui violations | Maintainer |
| 5 | Build (wheel/sdist) | Only in-process test builds; no retained artifact | No reproducible artifact ever produced | New CI `build` job: `python -m build` → upload + attest | Maintainer |
| 6 | Install smoke | AC-4 (fresh-venv `pip install .` + `pi-batch classify`) is **manual only** | Built-wheel install never tested in CI | CI job: install wheel in fresh venv → `pi-batch check/health/classify` | Maintainer |
| 7 | Publish | None | No release mechanism (0 tags) | Tag-driven trusted publish (TestPyPI → PyPI); no PAT publishing | Maintainer |
| 8 | 7×24 ops | Docs only; `pi-batch health` works (Verified) | No unit files, no state backup/restore, stale systemd path | Corrected unit template + logrotate + backup runbook + restore drill | Operator |
| 9 | Rollback | No retained artifacts | Rollback = reinstall from git | Retain tagged artifacts; runbook §4 | Maintainer |

## 3. Findings (severity-sorted)

**C-1 (Critical) — No green revision exists; `make ci` fails at committed HEAD.** Verified: in a clean worktree at HEAD, `python quality.py .` → 3 violations (`projects/iris-ui/scripts/iris-ui-spec.py:167` complexity 20 > 15, `:263` 51 lines > 50; introduced by commit `5e69337`), `cli.py check` → `CHECK: failed: quality`, and `tests/test_agent_engineering.py::test_check_subcommand_passes` fails on the quality assert. CI (which runs `make test` incl. that test) would be red on push-to-main. *Impact*: release blocker for any production claim; the repo's own hard gate is red at HEAD independent of this campaign. *Remediation*: fix iris-ui violations (or explicitly budget them) in a committed revision; unify CI with `make ci`. *Validation*: clean-worktree `make ci` exits 0.

**H-2 (High) — Live GitHub PAT in `.git/config` remote URL** (`remote.github.url = https://yangwb1123:ghp_***@github.com/...`; verified via `git config --get-regexp`). Not in any tracked blob (git-grep clean; only the redacted `ghp_***` marker appears in a committed review artifact), so remote consumers aren't exposed — but any process/backup/shell on this host can exfiltrate write credentials to the delivery remote. *Impact*: credential compromise → malicious pushes. *Remediation*: revoke now; replace with SSH key or credential helper. *Validation*: remote URL shows no token; revocation confirmed in GitHub settings.

**H-3 (High) — Test suite is lock-hostile: `make ci` fails whenever a campaign runs in-tree, which is the documented 7×24 model.** Verified: live worktree (`.pi-batch.lock` held by `campaign-r2`, pid 1547473) → `tests/test_pi_batch.py` **13 failed / 78 passed**, full `make ci` 21 failed — failures are CLI subprocess tests receiving `ERROR: Another pi-batch instance is running … exit 5` (`pbatch/lock.py` O_EXCL lock in process cwd, never broken on age). Control: same file at HEAD in a lock-free worktree → **100% pass**. *Impact*: local gate runs in the operational environment are meaningless; implement-stage acceptance ("make ci green") is unachievable in-tree. *Remediation*: CLI subprocess tests pass `--no-lock` or run with lock-free `cwd`; run gates in CI or a scratch worktree. *Validation*: `make ci` passes with a live campaign lock held.

**H-4 (High) — Shared worktree carries uncommitted cross-campaign implementations; build inputs ≠ any commit.** Verified: 47 M + 169 ?? + 19 D files; includes backendquality's manifest-aware `check-backend-quality.py` (+125 lines, uncommitted — now violating its own quality budget: `main` 57 lines/complexity 18 → 2 violations in worktree `quality.py .`), +148 lines in `tests/test_pi_batch.py`, iris-ui spec fixes, and **19 deleted files from two other campaigns' run archives** (`execute-expect-only-eval-cases…`, `package-pbatch-data-files…`; recoverable via git, but the worktree silently diverges). *Impact*: reproducibility/provenance broken; this campaign's implement stage will inherit failures it did not cause; concurrent implementations of the *same* file (`check-backend-quality.py` is touched by both the backendquality design and prior fixes) create merge risk. *Remediation*: commit or revert per-campaign changes before implement; the implement stage must snapshot the baseline failure set and prove its own diff doesn't add failures. *Validation*: `git status --porcelain` clean at gate time; per-file `git diff` attributed.

**M-5 (Medium) — CI/`make ci` gate drift, both directions.** Workflow never runs `quality`/`org`; `make ci` never runs `eval`/`coverage` (verified: `Makefile:47` vs `ci.yml`). Two different "green" definitions; org-spec and god-file regressions can pass CI (org check currently valid, but unguarded). *Remediation*: CI = `make ci` + `make eval` + `make coverage`. *Validation*: diff the target lists.

**M-6 (Medium) — No build/release pipeline.** No tags, no CI build step, no artifact retention, no publish target, version hardcoded. *Impact*: no reproducible/auditable artifact; rollback to a known-good build impossible; provenance cannot be demonstrated. This design's scope guardrails explicitly defer it — acceptable for the campaign, blocking for any production claim. *Validation*: CI job produces an uploaded wheel with provenance attestation.

**M-7 (Medium) — No dependency lockfile.** Floors only (`PyYAML>=6.0`, `pytest>=8.0`); CI resolves latest → nondeterministic build inputs. *Remediation*: `uv.lock`/pip-tools constraints for CI/release. *Validation*: CI installs from lock.

**M-8 (Medium) — Single Python 3.11 in CI vs `requires-python >=3.9`.** 3.9/3.10/3.12/3.13 users install untested code. *Remediation*: matrix `[3.9, 3.11, 3.12]`. *Validation*: matrix run.

**M-9 (Medium) — Runtime ops is docs-only with stale assets.** `ops/deploy/` absent; systemd template path stale (`/home/u1/workspace/demo/snaplink`); no unit/timer files, no state backup/restore runbook for append-only state (memory index, `state.jsonl`, archive) — disk loss silently destroys decision history and campaign resumability. *Remediation*: ship corrected unit template; periodic `pi-batch health` probe; backup + restore drill. *Validation*: restore drill from backup.

**L-10 (Low) — No dependency security scanning** (pip-audit/OSV) and no workflow `timeout-minutes`/`concurrency`. Acceptable non-goals for an internal CLI; cheap wins. *Validation*: workflow YAML contains `timeout-minutes` and a pip-audit step.

## 4. Release and rollback procedure

**Preflight (per release candidate):**
1. **Green committed revision first (C-1)**: clean-worktree `make ci` + `make eval` + `make coverage` exit 0 on the exact commit; packaging `7 passed, 0 skipped` (this design's R1) — including the wheel test.
2. Clean tree (`git status --porcelain` empty; no `build/`, no `egg-info` per R2), `python -m build` → wheel + sdist.
3. Fresh-venv smoke: `pip install dist/*.whl` → `pi-batch check`, `pi-batch health`, `pi-batch classify --json "写一个后端接口"` (AC-4); verify `pbatch/role_keywords.yaml` + `task_keywords.yaml` in the wheel (`unzip -l`).
4. Version consistency: tag `vX.Y.Z` matches `pyproject.toml` version; signed tag.
5. Credential check: `git config --get-regexp '^remote\..*\.url$'` contains no tokens (H-2).

**Migration:** none for the package (stateless CLI). State is append-only JSONL; after upgrade run `pi-batch memory ingest` (idempotent, incremental) to re-index sessions. No DB migrations; no schema-version gate on state files (backward-compatible read paths verified).

**Probe (post-install, pre-cutover):** `pi-batch check` + `pi-batch health`, then one real task with `--session-mode shared --reuse` against existing state to confirm session continuity and validator revalidation under the new version.

**Post-deploy:** monitor `--log-file` for validation rejections; confirm `docs/DECISIONS.md` records the release; verify `sessions.index.jsonl`/`state.jsonl` still parse; record artifact hash + signed-tag verification.

**Rollback:** (a) reinstall previous version from retained artifact or `git checkout <prev-tag> && pip install .`; (b) re-run `pi-batch memory ingest` (idempotent index rebuild); (c) verify `pi-batch health` + one `--reuse` run; (d) note: downgrade does **not** replay decision history written by the newer build (append-only state is forward-only) — operators must treat rollback as code-only.

## 5. Missing assets / operator decisions blocking a production claim

1. **A green committed revision** — currently impossible: HEAD fails quality (iris-ui, C-1); the worktree additionally fails 21 tests under the live campaign lock (H-3) and carries uncommitted cross-campaign work (H-4). This blocks this campaign's own migration step "end in `make ci` green" and must be surfaced to the implement stage as a baseline constraint, not a design defect.
2. **Credential remediation decision** — revoke the PAT in `remote.github.url` (H-2) before any release or further GitHub delivery.
3. **Build/release pipeline** — versioning (from tags), CI wheel build + artifact retention + provenance, publish path, and install-of-artifact smoke (M-6, rows 5–7).
4. **`ops/deploy/` assets** — the prompt-mandated location does not exist: unit/timer templates, health-probe wiring, logrotate, and a backup/restore runbook for append-only state (M-9). Without these, "7×24 unattended" remains a doc claim.
5. **Environment parity decisions** — Python version matrix and a dependency lockfile (M-7, M-8); CI/`make ci` gate unification (M-5).
6. **Design-scope caveat**: the close-wheel design is sound and minimal for its direction (R1/R2 validated empirically), but its AC-1 guard against silent-skip recurrence is manual-only — recommend a fail-closed test assertion (fail, not skip, when `wheel`/`build` are absent from dev extras) so the fix cannot silently regress.

---

**Verdict on the design (advisory):** implementable as scoped; the two manifest edits are validated (7 passed / 0 skipped reproduced with `wheel 0.47.0` + `build 1.5.0`; `git check-ignore` behavior verified). The blocking conditions are **pre-existing and out of the design's scope** (C-1, H-2, H-3, H-4) — the implement stage must not be judged against a `make ci` green that the baseline cannot produce, and the gate must distinguish campaign-attributable failures from baseline noise.
