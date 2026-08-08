All verification complete. Here is the DevOps engineering review.

---

# DevOps Engineer Review — ai-batch-runner build/delivery/deployment

**Scope executed for this revision (`main` @ e623e22):** full-tree scan for deploy assets, `.github/workflows/ci.yml` audit, `pyproject.toml`/`Makefile`/`.gitignore`/`pytest.ini` inspection, git tags/remotes/config audit, packaging-test live repro in a **fresh venv replicating CI's exact install**, `tests/test_packaging.py` source check, ops docs (`RUNNING_247.md`, `AUTOMATION_WORKFLOW_SUMMARY.md`) review, campaign design artifact (`task-1-design.md`) cross-checked against current tree.

## 1. Supported deployment-path and artifact inventory

**Deployment model: pip-installable Python CLI** (`[project.scripts] pi-batch = "pbatch.cli:main"`). There is **no service, daemon, container, or hosted runtime** in this repo.

| Artifact / path | Status | Notes |
|---|---|---|
| `ops/deploy/` | **Missing** (no `ops/` dir at all) | Prompt-claimed location does not exist; nothing to verify beyond absence |
| Wheel/sdist of `ai-batch-runner` | **Missing** | `dist/` contains only `design-intelligence-pack/` (unrelated knowledge pack, gitignored); no `python -m build` anywhere |
| `build/` | **Missing** (deleted during design validation) | `.gitignore` still lacks a `build/` entry (design F4) |
| `ai_batch_runner.egg-info/` | Present, untracked, gitignored | Design F3: delete; live-tree leak from editable installs |
| Git tags / releases | **None** (`git tag -l` empty) | Version hardcoded `0.1.0` (pyproject.toml:7); no `__version__` in `pbatch/` |
| CI | `.github/workflows/ci.yml` (only workflow) | PR + push-to-main; Python 3.11 only; gates + tests + eval + coverage |
| Published package (PyPI/TestPyPI) | **Missing** | No publish workflow, no credentials config, no trusted publishing |
| Delivery remotes | `origin` (Gitea), `github` (GitHub) | `github` remote URL embeds a **live PAT** — see Finding F-2 |
| Runtime deployment assets | Docs only | systemd/nohup template in `docs/RUNNING_247.md`; no unit files, no health-check wiring; template path is stale upstream (`/home/u1/workspace/demo/snaplink`) |
| Deployment manifests (Docker/k8s/helm/terraform) | **None** (searched) | Appropriate for a CLI; `scripts/sverp/*` and `examples/snaplink-platform/*` are **other projects'** example manifests — not deployability evidence for this package |
| State to protect | `.pi-batch/memory/*`, `docs/auto/state.jsonl`, `docs/archive/` | Append-only JSONL; no backup strategy documented |

## 2. Pipeline table (stage → evidence → gap → proposed gate → owner)

| # | Stage | Current evidence (what actually ran) | Gap | Proposed gate | Owner |
|---|---|---|---|---|---|
| 1 | Source control | githooks `pre-commit`/`pre-push`; PR branch protection unknown | No tag/branch policy evidence; no signed releases | Require status checks on main; GPG-signed tags | Maintainer |
| 2 | Engineering gates G1–G4 | CI runs `make check-*` (checks/*, `cli.py`) — Verified | None for these gates | Existing (keep) | Maintainer |
| 3 | Regression suite | CI runs `make test`; **fresh-venv repro: `6 passed, 1 skipped`** (wheel test silently skipped) | **`wheel`/`build` absent from dev extras → the only wheel-content verification never runs in CI** (design's core gap) | Add `wheel>=0.42`, `build>=1.0` to `dev` extras (design fix, empirically validated → 7 passed); optionally fail-closed no-skip assert | Maintainer (campaign implementer) |
| 4 | Eval + coverage | Run in CI (`make eval`, `make coverage`) but **not** in `make ci` | Gate drift both directions (`make ci` omits eval/coverage; CI omits quality/org) | Unify: CI runs `make ci` + `make eval` + `make coverage`; local `make ci` gains eval | Maintainer |
| 5 | Org quality | `make quality`, `make org` exist; **not in CI workflow** | God-file/dup-body and org-spec regressions pass CI | Add `make quality` + `make org` to workflow | Maintainer |
| 6 | Build (wheel/sdist) | None in CI; packaging tests use in-process build only | No reproducible artifact is ever produced or retained | New `build` job: `python -m build` → `actions/upload-artifact` + `attest-build-provenance` | Maintainer |
| 7 | Publish | None | No release mechanism at all (0 tags) | Tag-driven trusted publish (TestPyPI → PyPI); **no PAT-based publishing** | Maintainer |
| 8 | Runtime install smoke | None | `pip install -e ".[dev]"` tested; install-of-built-artifact never tested | Fresh-venv `pip install dist/*.whl` → `pi-batch check` + `pi-batch health` | Maintainer |
| 9 | 7×24 ops | Docs only (nohup/systemd template, log-file) | No health probing, log rotation, alerting, or **backup of append-only state** | Ship unit template + logrotate; periodic `pi-batch health`; backup `.pi-batch/` + `docs/auto/` | Operator |
| 10 | Rollback | No retained artifacts/previous versions | Rollback = "reinstall from git" | Keep tagged artifacts; documented rollback runbook (§4) | Maintainer |

## 3. Findings (severity-sorted)

**F-1 (High) — CI silently skips the only wheel-content verification.** `tests/test_packaging.py:214` `pytest.importorskip("wheel")`; `pyproject.toml` `dev = [pytest, pytest-cov]` only; workflow installs `-e ".[dev]"` then `make test`. **Verified by live repro**: fresh venv with CI's exact install → `6 passed, 1 skipped`, `wheel`/`build` absent; same env with `wheel 0.47.0`/`build 1.5.0` → 7 passed. *Impact*: a wheel missing `pbatch/role_keywords.yaml`/`task_keywords.yaml` (runtime-loaded via `Path(__file__).with_name(...)`) ships and breaks meta-role orchestration and task classification in production installs; regressions invisible to CI. *Remediation*: design AC-1 — add `wheel>=0.42`, `build>=1.0` to `dev` extras (zero workflow changes); optionally an explicit no-skip guard in `test_packaging.py`. *Validation*: fresh venv → `pip install -e ".[dev]"` → `pytest tests/test_packaging.py -q` → `7 passed`.

**F-2 (High) — Live GitHub PAT embedded in `.git/config` remote URL** (`https://yangwb1123:ghp_***@github.com/...`; file mode 664). **Verified** (`git config --get-regexp '^remote\..*\.url$'`); **not committed** (tracked-tree grep clean), so repo consumers aren't exposed — but any process/backup/shell output on this host can exfiltrate write credentials to `github.com/yangwb1123/ai-batch-runner`. *Impact*: credential compromise → malicious pushes to the delivery remote. *Remediation*: revoke immediately, replace with SSH key or `git credential` helper / scoped `GITHUB_TOKEN`. *Validation*: `git config --get-regexp '^remote\..*\.url$'` shows no token; confirm revocation in GitHub settings.

**F-3 (High) — No build/release pipeline.** No tags, no CI build step, no artifact retention, no publish target; version hardcoded `0.1.0`. *Impact*: no reproducible, auditable artifact; "release" is whatever branch someone installs; rollback to a known-good build is impossible; provenance cannot be demonstrated for a production claim. *Remediation*: add `build` job (§2 row 6), tag-driven publish, version-from-tag (`setuptools_scm`) or tag-vs-version consistency gate. *Validation*: CI job produces an uploaded wheel; `pip install` of that wheel passes `pi-batch check`.

**F-4 (Medium) — CI/`make ci` gate drift.** Workflow never runs `quality`/`org`; `make ci` never runs `eval`/`coverage`. *Impact*: two different "green" definitions — local green ≠ CI green and vice versa; org-spec/god-file regressions ship. *Remediation*: CI runs `make ci` plus eval/coverage; add eval to `make ci`. *Validation*: run both locally; diff target lists.

**F-5 (Medium) — Single Python version (3.11) vs `requires-python >=3.9`.** *Impact*: 3.9/3.10/3.12/3.13 users install an untested package; design's "≥3.9 satisfied" claim is floor-declared, not verified. *Remediation*: matrix `[3.9, 3.11, 3.12]` on the test job. *Validation*: workflow matrix run.

**F-6 (Medium) — No dependency lockfile.** Floors only (`PyYAML>=6.0`, `pytest>=8.0`); CI resolves latest. *Impact*: nondeterministic build inputs; a future PyYAML break becomes a surprise CI failure mid-delivery. *Remediation*: commit `uv.lock` (or pip-tools constraints) for CI/release; keep floors for library consumers. *Validation*: CI installs from lock with hash check.

**F-7 (Medium) — 7×24 operation claim lacks monitoring/backup.** `RUNNING_247.md` systemd template only (stale path `/home/u1/workspace/demo/snaplink`); no health probes, logrotate, alerting, or backup of append-only state (`.pi-batch/memory/`, `docs/auto/state.jsonl`, `docs/archive/`). *Impact*: disk loss silently destroys decision history and campaign resumability. *Remediation*: ship corrected unit template; periodic `pi-batch health` (exists, cli.py:307); backup runbook. *Validation*: restore drill from backup.

**F-8 (Low) — Packaging hygiene pending design execution.** `ai_batch_runner.egg-info/` still in tree; `.gitignore` lacks `/build/` (F3/F4 of design). *Remediation*: delete egg-info; add root-anchored `/build/` per design. *Validation*: `git status --porcelain --ignored | grep -E 'egg-info|build'`.

**F-9 (Info) — Minor CI hardening:** no `timeout-minutes`/`concurrency` on the workflow; ubuntu-latest single OS is acceptable for pure-Python; no SBOM/attestation — reasonable non-goals for an internal CLI, note as deliberate.

## 4. Release and rollback procedure

**Preflight (per release candidate):**
1. `make ci` **and** `make eval` **and** `make coverage` all green on the exact commit; packaging tests `7 passed` (no skips) in a fresh venv — this is the F-1 regression net.
2. Clean checkout (`git status` clean incl. ignored tree: no `build/`, no `egg-info`), `python -m build` → wheel + sdist.
3. Fresh-venv smoke: `pip install dist/*.whl` → `pi-batch check`, `pi-batch health`, and a 1-task dry run; verify `pbatch/role_keywords.yaml` + `task_keywords.yaml` present in the wheel (`unzip -l`).
4. Version consistency: tag `vX.Y.Z` must match `pyproject.toml` version.
5. **Pre-merge credential check**: `git config --get-regexp '^remote\..*\.url$'` contains no tokens (F-2 remediation).

**Migration:** none required for the package itself (stateless CLI). State is append-only JSONL; after upgrade run `pi-batch memory ingest` (idempotent, incremental) to re-index sessions under the new build. No DB migrations; no schema-version gate on state files — verified backward-compatible read paths only.

**Probe (post-install, pre-cutover):** run `pi-batch check`, `pi-batch health`, then one real task with `--session-mode shared --reuse` against existing state to confirm session continuity and validator revalidation behavior under the new version.

**Post-deploy checks:** monitor `--log-file` for validation rejections; confirm `docs/DECISIONS.md` records the release decision; verify state files (`sessions.index.jsonl`, `state.jsonl`) still parse; run `git tag -v vX.Y.Z` (signed tag) and record artifact hash.

**Rollback:** (a) reinstall previous version from retained artifact (release store holds prior wheel) or `git checkout <prev-tag> && pip install .`; (b) state compatibility: run `pi-batch memory ingest` again — index rebuild is idempotent; (c) verify `pi-batch health` and one `--reuse` run before declaring recovery; (d) gate: since state is append-only, forward writes from a newer version are **not** replayed into older builds — document that downgrade retains post-downgrade runs as a fresh session (safe, but operators must not expect rollback of decision history, only of code).

## 5. Missing assets / operator decisions blocking a production claim

1. **`ops/deploy/` (and any ops directory) does not exist** — there is nothing deployable-by-default: no runbook, no unit file, no environment configuration. A "production deployment" for this tool is currently undefined.
2. **No release mechanism**: tags, build job, artifact store, and publish target all absent (F-3). Until a tagged, built, retained artifact exists, no deployment can be pinned or rolled back.
3. **CI gate parity** (F-4) and **packaging-test visibility** (F-1) must be closed before CI can be cited as the release gate.
4. **Credential hygiene decision** (F-2): revoke the PAT and choose SSH/credential-helper; also decide who may push to `main` (branch protection not evidenced).
5. **Dependency reproducibility decision** (F-6): accept latest-resolution or commit a lockfile — this is an explicit operator choice, not made today.
6. **Monitoring/backup decision for 7×24 operation** (F-7): no health-check schedule, no log rotation, no backup/restore procedure for the append-only state that makes the tool resumable.
7. **Python-version support matrix decision** (F-5): `>=3.9` is claimed; only 3.11 is tested.

**Bottom line:** the campaign's planned fix (F-1) is the highest-value delivery item — empirically validated, zero-workflow-change, and it converts the only wheel-content check from skipped to enforced in CI. Everything else above is a standing gap between "runs from git" and "releasable/deployable"; none of the findings blocks the campaign's implementation scope itself (packaging manifests only), but F-1's fix should land together with F-8 cleanup so the release path is coherent.
