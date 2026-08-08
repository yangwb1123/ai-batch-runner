# Architecture Review — pbatch Data-Files Packaging (campaign direction)

**Revision reviewed:** `01caa2c` + dirty working tree (parallel campaign directions hold uncommitted edits — declared per evidence standard). **Checks run live:** sdist builds (clean `git archive` checkout + stripped copies), loader code paths, `classify` discriminators, `quality.py --strict`, CI workflow, deploy-asset scan, `wheel` presence. `make ci` not run (dirty tree, same reason as QA lead).

---

## 1. Scope, Assumptions, Verified Architecture Summary

**Scope.** Evaluate the design for shipping `pbatch/role_keywords.yaml` + `pbatch/task_keywords.yaml` (and `*.md`) in wheel/sdist, plus the surrounding distribution-boundary architecture. No code changes made by me.

**Assumptions.** Canonical refs from `prompts/README.md` (`docs/architecture/DIRECTORY_MAP.md`, `docs/feature-matrix.md`, `docs/config-reference.md`, `docs/error-codes.md`, `docs/openapi.yaml`) **do not exist in this repo** — navigation base is the live tree + `AGENTS.md`. Installed-distribution behavior is judged against the README/pyproject promise "install as a first-class CLI (`pip install .` → `pi-batch` anywhere)".

**Verified architecture summary** (all live, this revision):

| Layer | Verified state |
|---|---|
| Distribution boundary | `pyproject.toml`: `packages.find include=["pbatch*"]`, no `package-data`, no `MANIFEST.in`/`setup.cfg`/`setup.py`; dist name `ai-batch-runner`, **import name `pbatch`** (egg-info dir `ai_batch_runner.egg-info` — underscores, confirming the QA-F2 wrong-key hazard is plausible) |
| In-package resources | `pbatch/role_keywords.yaml` (34 roles), `pbatch/task_keywords.yaml`, `pbatch/README.md` — shipped by nothing today; **sdist built from clean `git archive` checkout: 61 members, 0 YAML, 0 data MD, 0 scripts/examples/tests** (design's "169 members" is tree-state dependent — stale in-tree `ai_batch_runner.egg-info/SOURCES.txt` inflates member lists; invariant "0 data files" holds both ways) |
| Wheel | Not buildable locally (`wheel` absent — design constraint confirmed); design's `pip wheel` inspection (102 `.py`, 0 data) accepted **Partial** (mechanism consistent with sdist) |
| Loader semantics | `relevance.py:51` silent `{}` on missing YAML (reproduced: YAML-less pbatch → `{}`); `classifier.py` additive built-in defaults (reproduced: `classify -p '生成登录页 tsx'` **byte-identical** `route to frontend pipeline: True` with and without YAML); `rule_matcher.py:91` resolves repo-root + cwd (both miss on install); `config.py:48` `TOOL_ROOT = Path(__file__).parent.parent` → site-packages root on install |
| Parity | `task_keywords.yaml` is a strict subset of built-in defaults; 3 default-only terms (`refactoring`/`重构代码`/`admin`) confirmed at `classifier.py:66,78-79` — naive equality fails, merged==expected is correct |
| CI | `.github/workflows/ci.yml` installs `pip install -e ".[dev]"` → packaging regressions structurally invisible; no build/publish job; **0 tags**; version frozen `0.1.0` |
| Deployability | No `ops/`, Dockerfile, systemd, terraform, k8s anywhere (devops F2 confirmed); `pi-batch.yaml` validators are `{cwd}/scripts/...` or one hardcoded absolute path (`completion`) |
| Gates | `quality.py --strict .` → **OK**; `tests/test_packaging.py` does not exist |

**The fix is architecturally sound and minimal:** package-data keyed to the import name ships the two in-package YAMLs through the same manifest machinery into both wheel and sdist, with zero loader/API changes. The architecture gap it *doesn't* close is precisely bounded: **four out-of-package runtime resource classes keep degrading silently on any pip/pipx/uv install** (see findings).

---

## 2. Findings Table

| # | Sev | Finding | Evidence (live) | Impact | Recommendation |
|---|---|---|---|---|---|
| F1 | **Critical** | Data files never reach wheel/sdist | sdist clean-build: 61 members, 0 YAML; wheel blocked locally but mechanism identical | Documented `pip install .` path silently loses meta role relevance (34 roles → `{}`) and task-keyword enrichment; release blocker for the distribution promise | Apply the design's one-block `[tool.setuptools.package-data]` (`pbatch = ["*.yaml", "*.md"]`) |
| F2 | **Critical** | No deployable production footprint exists | `ls ops` fails; zero Dockerfile/systemd/tf/k8s assets | No supported path to run the tool as a service (7×24, fabric control plane) | Owner decision (U1/U2); not solvable by this direction — defer with explicit sign-off |
| F3 | **High** | Editable-install blind spot | CI: `pip install -e ".[dev]"` only; no build job | Regression invisible to every gate — exactly what happened here | Add a CI job: build wheel+sdist from `tmp_path` copy, assert exact member names (covers QA-F1: never slash-substring predicates) |
| F4 | **High** | Post-fix installs still silently degrade 3 resource classes | `rule_matcher.py:91` (repo-root + cwd paths for `ui-specs/rules.yaml`, `backend-specs/rules.yaml`); `config.py:48` `TOOL_ROOT`; `classifier.py:376-381` default pipeline `examples/frontend-implementation-pipeline.yaml` (not packaged) | `rules` registries degrade to defaults; `{tool_root}` validators break; classify routing points at a nonexistent file — all silent | Narrow the AC wording to "keyword-routing data ships"; declare a support matrix (pip = keyword-routing subset; copy-based = full); classify JSON output gets a repo-mode caveat |
| F5 | **Medium** | No silent-degradation probe | `health.py` checks only `docs/rules/*.yaml` counts, never data-file presence | Broken installs report healthy | Add data-file presence to `health.py` (deterministic, stdlib) |
| F6 | **Medium** | No version identity | 0 tags, version `0.1.0`, no publish job | Rollback target undefined; can't distinguish pre/post-fix installs | Tag + bump on this change (patch bump suffices); document rebuild requirement |
| F7 | **Medium** | Evidence count claim unstable | Design "169 members" vs clean-tree **61** — stale `egg-info/SOURCES.txt` inflates (QA-F3's hazard, reproduced) | Acceptance based on member counts is tree-state dependent | ACs assert exact member *names* (`pbatch/role_keywords.yaml in names`), never counts; build from `tmp_path` copy |
| F8 | **Medium** | In-suite wheel test would silently skip | `wheel` module absent locally | False green on the primary artifact type | Add `wheel` to dev extras (QA-F6); sdist proxy test remains the deterministic offline gate |
| F9 | **Low** | AC-2 venv smoke has undeclared network dependency | PyYAML is a runtime dep; `--no-deps` install false-fails the check | Flaky/confounded acceptance | Document PyYAML pre-install in the AC-2 procedure (QA-F4) |
| F10 | **Low** | `docs/architecture/DIRECTORY_MAP.md` + 4 canonical refs cited by role prompts don't exist | `ls` fails on all five | Role prompts cite nonexistent navigation base | Doc debt: create or re-point; separate from this direction |
| — | Info | Design's two self-corrections (YAML strict-subset parity; assert string nonexistence) are **accurate** | Reproduced both; `merged == expected` is the right parity form | — | Keep them in the AC wording |

**Design-claim corrections from my run:** (1) member count 169 is SOURCES.txt-dependent — clean tree gives 61; (2) everything else in the design's verification table reproduced as stated (AC-2 `{}` vs 34 roles, AC-3 byte-identical, AC-4 exit-1 path, sdist proxy discrimination).

---

## 3. Decision Options

| Option | Cost | Coverage | Residual risk |
|---|---|---|---|
| **A. Design as-is** (one package-data block + `tests/test_packaging.py` + sdist-proxy ACs) | ~1 line + 1 test file | Keyword-routing data ships; acceptance bounded to that | Silent degradation of rules/TOOL_ROOT/prompts/examples remains **invisible to health and CI**; installs can still report healthy while degraded |
| **B. A + compensating controls** (health probe, CI build job asserting exact members, dev-extras `wheel`, support-matrix note in README/AGENTS.md, patch bump + tag) | ~4 small files, no loader changes | Everything in A, plus silent degradation becomes a visible signal and future regressions fail CI | Out-of-package resources still degrade by design — but now declared and monitored |
| **C. Full distribution-boundary repair** (package prompts/, examples/, scripts/, ui-specs/, backend-specs/ as data; move loaders to `importlib.resources`) | High; touches `rule_matcher.py`/`config.py`/`meta.py` loaders + data layout | Complete `pip install` parity | Contradicts the campaign's minimal-scope discipline; loader API churn for a feature nobody has requested end-to-end yet |

**Build-vs-buy:** no off-the-shelf component addresses this — it is a packaging/loader contract, and setuptools' built-in `package-data` machinery (already required by setuptools≥61) is the correct tool. Nothing to buy.

**Preferred: Option B.** The one-block fix is right and validated. The three extra controls (F5/F6/F3) are cheap, deterministic, stdlib-only, and convert the entire defect class from *silent* to *visible* — which is the property that actually protects the next release. Option C stays a follow-up direction with an owner, not this milestone.

---

## 4. Prioritized Implementation Sequence

**M0 — Red-first (before the fix):** add `tests/test_packaging.py` (build_sdist from `tmp_path` copy; expected data files globbed from repo; **exact-name assertions** `'pbatch/role_keywords.yaml' in names`); capture the red run in the record. *Check:* test fails with "0 data files".

**M1 — The fix + AC closure (the design's scope):**
1. Add `[tool.setuptools.package-data] pbatch = ["*.yaml", "*.md"]` to `pyproject.toml`. Single-block rollback; no API change; editable installs unaffected.
2. AC-1: wheel+sdist members include the 3 files (wheel in-process `importorskip`; sdist proxy mandatory).
3. AC-2: neutral-dir venv (`cd /tmp` mandated), PyYAML pre-installed, `load_role_keywords()` non-empty (34 roles); also `python -I` self-defense on 3.10 (QA-F5).
4. AC-3 restated: installed `role_keywords.yaml` byte-identical to repo; `load_keywords()` merged == expected (3 default-only terms accounted); `classify -p '生成登录页 tsx'` output byte-identical pre/post.
5. AC-4: exact sdist script, exit-1 pre-fix proven.
*Checks:* `pytest tests/test_packaging.py` green; `quality.py --strict .` green.

**M2 — Regression visibility (F3, F8, F6):** CI job building sdist (+wheel when available) from a `tmp_path` copy with exact member assertions, so any future packaging break fails PRs (editable-install blind spot closed); add `wheel` to dev extras; patch version bump `0.1.0 → 0.1.1` + tag (rollback identity).

**M3 — Declared surface + probe (F4, F5):** `health.py` data-file presence check; README/AGENTS.md support-matrix note ("pip install = keyword-routing subset; repo copy = full, incl. rules registries, prompts, examples, `{tool_root}` validators"); classify JSON `frontend_pipeline` path caveat for installed mode.

**Final gate (QA exit criteria, endorsed):** full `make ci` on an **isolated worktree** (this tree is dirty from parallel directions); diff of the change restricted to `pyproject.toml` + `tests/test_packaging.py` (+ the M2/M3 files if approved).

**Compatibility plan:** config-only, no API/ABI change; pre-fix wheels are stale → rebuild/reinstall via pipx/uv documented; rollback = remove one block.

**Top risks:** (a) wrong-key (`ai_batch_runner`) silently empty — covered by the wrong-key sdist test (also empty, verified by QA); (b) in-tree `egg-info/SOURCES.txt` pollution — covered by `tmp_path` builds; (c) `*.yaml` glob over-match if fixtures are added to `pbatch/` later — minor, note in test comment.

---

## 5. Unknowns Needing Owner / Product Decisions

- **U1 — Supported deployment surface.** Is pip/pipx/uv an officially supported *complete* install, or only a keyword-routing subset (repo copy = full)? This single decision determines F4 scope, README wording, and whether Option C is ever funded.
- **U2 — Follow-up distribution scope.** Should `ui-specs/`+`backend-specs/` YAMLs, `prompts/`, `examples/`, and `scripts/` validators ship as data in a later release (Option C), and who owns it? Currently deferred with no owner (devops F4).
- **U3 — Production footprint.** F2 (no deploy assets) blocks any "production" claim — is 7×24 service deployment in scope for this tool at all, or is it a library/CLI only?
- **U4 — Runtime silence policy.** Missing-data silence at `relevance.py:51`/`rule_matcher.py:109` stays deferred (F8 in design): acceptable given the M3 health probe, or should loaders emit warn-once logs? (I lean: probe is enough; logging touches every loader.)
- **U5 — Version identity cadence.** Tags/semver adoption before any production use (F6); also Python env matrix (3.9/3.12) — CI only exercises 3.11.
- **U6 — Doc debt.** `docs/architecture/DIRECTORY_MAP.md` and four other canonical refs cited by role prompts don't exist; create or re-point (F10).
