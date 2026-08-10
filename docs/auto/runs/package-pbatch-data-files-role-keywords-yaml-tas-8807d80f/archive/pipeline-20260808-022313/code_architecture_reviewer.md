All evidence gathered. Here is my review.

---

# Code Architecture Review — pbatch data-file packaging design (campaign `package-pbatch-data-files-role-keywords-yaml`)

**Reviewed revision:** `01caa2c` (working tree carries parallel-campaign edits; all checks below ran on the current tree, which is what the design targets)

**Checks actually run (evidence standard):**
- `setuptools.build_meta.build_sdist` from repo root → tar inspected: **169 members, 0 YAML** (`pbatch/role_keywords.yaml`, `pbatch/task_keywords.yaml`, `pbatch/README.md` all absent; only root `README.md` ships) — **Verified**, reproduces AC-4 premise
- Source inspection of all three data loaders + `config.py:48 TOOL_ROOT` + its consumers (`runner.py:487,698`) — **Verified**
- `wc -l` + AST pass on `pbatch/config.py` (486 lines, 18 top-level symbols, no function >40 lines) — **Verified**
- `quality.py --strict .` → **OK** — **Verified**
- `git status`: tree pre-dirty from parallel directions; `ai_batch_runner.egg-info` is git-ignored (my build left no tracked changes) — **Verified**
- Design's empirical fix validation (scratch-project wheel+sdist) and parity corrections taken from evidence, not re-run — **Proposed/Verified-by-others** (independent confirmation in QA review)

---

## 1. Findings

| # | Sev | Rule ref | Evidence | Impact | Refactor plan |
|---|---|---|---|---|---|
| F1 | **High** | budgets §1 (单向可解释依赖) | **Data boundary is implicit and split.** Covered by fix: `relevance.py:51`, `classifier.py:136` resolve `Path(__file__).with_name(...)`. **Not covered:** `rule_matcher.py:91` resolves `parent.parent / backend-specs/rules.yaml` + `ui-specs/rules.yaml`; `config.py:48` defines `TOOL_ROOT = Path(__file__).parent.parent` which is a *lie* on pip installs (site-packages' parent ≠ tool root) and feeds `{tool_root}` expansion (`runner.py:487,698`); same pattern in `rule_cli.py:20`, `cli_batch.py:110`, `advance.py:89`, `capsule.py:75`, `reflect.py:153`, `tools.py:118`, `ui_geometry.py:32-35`, `health.py:25`, `export_template.py:19` | Post-fix, a pip-installed `pi-batch` gains role/task keywords but `rules` still silently degrades to defaults/empty, `{tool_root}` validators break, pipeline templates and `scripts/` subprocesses vanish. The fix draws a partial boundary that reads as "pip works now" (AC-2-style checks pass on the covered surface). This is the architectural root of devops F4. Some assets are *deliberately* repo/project-scoped (`pi-batch.yaml` — pyproject comment), others are *accidentally* so (registries) — the split is not stated anywhere | (a) State the invariant: *every `with_name("*.yaml"/"*.md")` reference must be declared in package-data; every TOOL_ROOT-relative asset is repo-deploy-only and enumerated in one doc*. (b) Encode it: in `tests/test_packaging.py`, statically collect `with_name("*.yaml")` refs in `pbatch/` and assert each ships in the built sdist (≈15 lines); assert an explicit allowlist for the two TOOL_ROOT registries. (c) Long-term follow-up (owner + sign-off, per devops F4): move `ui-specs/rules.yaml`/`backend-specs/rules.yaml` into the package, following the `classifier.py` precedent — out of scope for this fix, correctly deferred |
| F2 | Medium | budgets §2 (按业务能力组织) | Sibling capabilities keep data in different homes: `pbatch/task_keywords.yaml` (in-package) vs `ui-specs/rules.yaml`, `backend-specs/rules.yaml` (repo root) — `classifier.py` vs `rule_matcher.py` | Portability contract ("copy `pi-batch.py` + `pbatch/` + `pi-batch.yaml`", AGENTS.md §1) silently carries different data subsets per capability; the fix makes the split visible but not consistent | Document the split in the same invariant doc as F1; registry relocation as a follow-up direction (design already routes it there — keep it) |
| F3 | Medium | budgets §4 (consistent error handling) + AGENTS.md §5 fail-closed ethos | Three sibling loaders, identical try/except-continue skeleton, **three different failure semantics** for "file absent": `relevance.py:56-61` → `{}` (scoring silently disabled, fail-open); `rule_matcher.py:104-109` → frontend defaults (fail-open) / backend empty registry (fail-closed); `classifier.py:146-151` → merged defaults (fail-closed) | The packaging regression stayed invisible for months because every loader has a silent fallback; AC-2's `{}` discriminator works only because relevance fails open. Post-fix, a wrong-key typo (QA F-QA2) degrades non-uniformly, making diagnosis inconsistent | Optional follow-up (design's "no loader changes" stance is correct for *this* fix — YAGNI in scope): the ≥2 real users bar (§5) is met, so a shared `load_yaml_bounded(paths, merge, absent=...)` in `pbatch/text_io.py` (owner of `read_text_bounded`) unifies semantics and logs at warning. Separate refactor, not a merge blocker |
| F4 | Low | budgets §4 + fail-closed ethos | Proposed glob `pbatch = ["*.yaml", "*.md"]`; `pbatch/` currently holds exactly 3 data files (verified: `README.md`, `role_keywords.yaml`, `task_keywords.yaml`) | Glob is fail-open on the future: any yaml/md dropped into `pbatch/` silently ships (or, on a wrong key, silently *doesn't* ship while tests stay green — the F-QA2 trap). An explicit 3-file list makes new data fail closed until consciously added, and the F1 loader-audit test then auto-reminds when a new `with_name("*.yaml")` ref appears | Replace glob with explicit list `pbatch = ["README.md", "role_keywords.yaml", "task_keywords.yaml"]`. Cost: 2 extra lines. Same ship-set, stricter boundary |
| F5 | Info | budgets §3 (god-file) | `pbatch/config.py`: 486 lines, 18 top-level symbols, no function >40 lines (AST-verified); hub imported package-wide; mixes YAML shim, config resolution, `TOOL_ROOT`, validators registry, pipeline defaults | Watch item only. Risk score: 300+ lines +1, 2+ business flows (resolution + registry + defaults) +2, 5+ cross-module deps +1 = **4/13** — below the 5 split threshold and the 8 merge-block. Within repo's own `quality.py` budget (≤1000). This fix adds nothing to it | None for this change. If it grows, the repo already has the dogfooding precedent (cli.py → `cli_rounds.py`/`cli_batch.py`) |

## 2. God-file analysis (required output)

**No god file in the change scope.** The fix is config-only (`pyproject.toml` + one new test file); `quality.py --strict .` passes. Every module in the touch path has a one-sentence SRP description (`relevance.py` = role scoring, `classifier.py` = task-type routing, `rule_matcher.py` = registry matching, `config.py` = config + validators registry): none meets ≥3 god-file criteria from budgets §3.

- `pbatch/config.py` score breakdown: 486 lines (+1) · 2+ business flows (+2) · hub with 5+ dependents (+1) = **4** → below split threshold (5), well below merge-block (8). No split plan required; if it were split, the natural seams are `_validators_registry`/`_pipeline_defaults`/`_config_resolution` per the existing `cli.py` dogfooding pattern.
- F1's loader-audit test must itself respect budgets: keep `tests/test_packaging.py` ≤400 lines / ≤10 exports (test-file budget ×2 applies; the audit loop is ~15 lines).

## 3. Positive findings (credited, so they are not repeated downstream)

- **YAGNI discipline is exemplary**: rejects MANIFEST.in, loader changes, version bump, new deps, and the `wheel`-dependency temptation (sdist-as-proxy is a faithful, cheaper proxy — QA verified wrong-key sdist is empty too).
- **Honest failure-mode table** (F1–F8) and the two evidence corrections (YAML-subset parity → `merged == expected`; nonexistent `AssertionError` string) materially harden AC-2/AC-3.
- **State placement**: loaders are stateless, path-injected pure functions — good. The only "global" (`TOOL_ROOT`) is derived, but its conflation of package location with repo root is the F1 root cause, now isolated.
- **Cross-review alignment**: F1/F2/F3/F4 are consistent with devops F4–F9 and QA F-QA1–F-QA8 (loader 0% manifest coverage, SOURCES.txt tree pollution, PyYAML network dependency, `python -I` self-defense) — no contradictions found between the three inputs.

## 4. Conditions for merge (non-blocking to the fix itself)

1. Apply F4 (explicit file list instead of glob) and add the F1 loader-audit assertion to `tests/test_packaging.py` — the regression suite must encode the boundary it is blessing.
2. Record the deferred surface (`rule_matcher` registries, `{tool_root}` validators, `scripts/` subprocesses) as a documented follow-up direction with an owner — the design already routes it there; the gate should require the owner line, not the implementation.
3. QA's exit criteria stand: red `test_packaging.py` capture pre-fix, then AC-1–AC-4 + parity + classify guards green, full `make ci` on an isolated worktree, diff limited to `pyproject.toml` + the new test.

---

VERDICT: PASS - config-only fix is minimal, empirically validated, and architecturally honest (YAGNI-respecting, no API/dep changes, failure modes documented); the only High finding (F1) is a pre-existing, explicitly deferred boundary gap — not introduced by this change — and is mitigated by two cheap, in-scope strengthenings (explicit package-data list + loader-audit invariant test) plus a documented owner for the deferred pip-coverage surface; no god files in scope, quality gates green at HEAD.
