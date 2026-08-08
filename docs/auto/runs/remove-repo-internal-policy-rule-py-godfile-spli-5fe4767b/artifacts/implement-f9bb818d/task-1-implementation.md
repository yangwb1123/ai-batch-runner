# Change Report — Remove repo-internal policy rule PY-GODFILE-SPLIT & fail closed on self-referential rule files (task-1)

## 1. Analysis (module boundary / data ownership / change points)

- **Module boundary**: this direction is a *tool-data + tool-code* change, not a
  business capability — no domain module owns it. The rule registry
  (`backend-specs/rules.yaml`) is data owned by the rule-matcher subsystem
  (`pbatch/rule_matcher.py` loading, `pbatch/rule_cli.py` validating). The
  design's ownership decision is honored: the rule's *canonical record* stays
  in `docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml` (repo-policy store,
  untouched), and the *shipped registry* stops carrying it.
- **Dependency direction**: `rule_cli.py` already depends on
  `rule_matcher.py` (load_registry, LEVELS, TIER_ORDER) — the gate adds a use
  of the existing private helper `_default_registry_paths`, so the module
  graph is unchanged (`cli → rule_cli → rule_matcher`), no new edges.
- **Change points**: exactly what the requirements pinned — (R1) data removal,
  (R2) fail-closed schema gate, (R3) eval regression lock. Nothing else
  varies; no abstraction introduced ("no pattern — composition suffices":
  the self-reference check is a resolved-path set membership test composed
  into the existing `_check_rule`/`check_registry` flow).
- **Strong consistency**: the gate must never silently pass on a polluted
  registry — hence fail-closed (`sys.exit(1)` on any violation, inherited by
  `pi-batch check` and `make ci` via `test_rules_check_passes`).

## 2. Implementation (design §3, landed per §6 order)

| File | Change | Design ref |
|---|---|---|
| `backend-specs/rules.yaml` | Deleted the `PY-GODFILE-SPLIT` block (description/files/min_tier/level/required + comment). File now byte-identical to HEAD. | R1 |
| `pbatch/rule_cli.py` | `check_registry(registry, registry_paths=None)` — additive optional param, default `None` preserves all existing callers; `_check_rule(..., registry_paths=None)`; new `_check_self_reference()` compares **resolve-normalized** `files`/`files_template` entries against the registry's own paths, precise violation message; `_check_registries_cli` passes `sorted({str(p.resolve()) for p in _default_registry_paths(domain)})` (repo-root + CWD candidates, deduped). Exit plumbing untouched → `rules --check` / `pi-batch check` / `test_rules_check_passes` inherit fail-closed with zero wiring. | R2.1–R2.6 |
| `evals/rules.yaml` | One new case `backend-manifest-excludes-repo-internal-policy` in **`assert:` form** (`task_type: backend` + `lacks_rule: [PY-GODFILE-SPLIT]`), anchored after `crud-backend-minimal`. | R3/R3.1 |
| `tests/test_rule_matcher.py` | `test_registry_rejects_self_referential_files` (violation side + default-arg backward compatibility) and `test_shipped_registries_have_no_self_reference` (A3 clean side, both domains). | R2.4 |
| `docs/SPEC_SYSTEM.md` | §2 registry note: rules must not reference the registry file itself; `rules --check` rejects self-referential `files`/`files_template` (fail closed). | R1.2 (documented guardrail) |

**Not changed** (per design): `pbatch/rule_matcher.py` matching logic
(required-rule fail-closed semantics at line 186 are correct and stay);
`pbatch/learn.py` (re-pollution protection = R2 gate + R3 eval + R1.2 doc);
`docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml` (archive intact,
verified present).

## 3. Evidence-backed deviations from design

1. **`_check_self_reference` extracted as a helper** — design §3.1 sketched the
   loop inline in `_check_rule`, but that pushed `_check_rule` to cyclomatic
   complexity 16 > 15 (org gate `quality.py --strict`). Extracted into a
   dedicated function; identical behavior and identical violation messages
   (design's exact wording).
2. **`_default_registry_paths` imported at module top** instead of inside
   `_check_registries_cli` — rule_cli.py already imports from rule_matcher at
   top; no circular import; C5 (no new public API surface) holds either way.
   `_default_registry_paths` remains private to the package.
3. **Rollback nuance (design §6.7)**: `pbatch/rule_cli.py` is **untracked**
   (it is part of the parallel cli.py-split workstream — `git log` has no
   commit for it, and HEAD's `rule_cli.py` does not exist). The design's
   `git checkout pbatch/rule_cli.py` rollback cannot restore it. Rollback for
   that file = manually revert the 3 edited blocks (the file's pre-existing
   content is the parallel workstream's, which must be preserved). The other
   three tracked files (`backend-specs/rules.yaml` already clean vs HEAD,
   `evals/rules.yaml`, `tests/test_rule_matcher.py`, `docs/SPEC_SYSTEM.md`)
   roll back with `git checkout`.
4. All other dirty-tree files (`pbatch/*.py`, `Makefile`, `ui-specs/rules.yaml`,
   …) are pre-existing parallel-campaign edits, untouched by this task
   (verified: `ui-specs/rules.yaml` dirty delta is `level`/`suppress_on`
   fields only, no self-references).

## 4. Verification commands actually run

| Gate | Result |
|---|---|
| R1 YAML parse + removal | `yaml.safe_load(backend-specs/rules.yaml)` OK; `grep -c PY-GODFILE-SPLIT backend-specs/rules.yaml` → 0; archive file `docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml` present |
| A1 | `python pi-batch.py rules "写一个订单创建接口，含支付审批，生产，go" --json` → `PY-GODFILE-SPLIT in rules[]: False` (23 rules, exit 0) |
| A2 | `python pi-batch.py rules "开发一个客户管理模块的列表查询与新增接口" --json` → `False` |
| A3 clean side | `python pi-batch.py rules --check` → both registries OK, **exit 0** |
| A3 violation side | Direct: polluted rule re-added in-memory → exact message `rule 'PY-GODFILE-SPLIT': files entry 'backend-specs/rules.yaml' resolves to the registry file itself (self-referential rule)`; all three path forms (relative, `./`-prefixed, absolute) caught; `check_registry(reg)` without paths returns `[]` (backward compatible). Pytest: `tests/test_rule_matcher.py -k self_referential` → **2 passed** |
| A4 | `python pi-batch.py eval --json` → **total 41, failed 0** (baseline 40/0, +1 delta); `eval --filter backend-manifest-excludes-repo-internal-policy` → **PASS** (case actively runs — inert-`expect:` hazard F7 checked) |
| Regression | `python -m pytest tests/test_rule_matcher.py` → 26 passed; `tests/test_agent_engineering.py -k rules` → 2 passed; `python -m pytest checks/` → 37 passed |
| Full suite | `python -m pytest tests/` → **934 passed, 19 failed — all lock contention**: `Another pi-batch instance is running (pid=1684832 … Refusing to start (exit 5))`. Our own live campaign holds the repo lock (verified signature; identical to the previous campaign's documented environmental finding; none of the 19 touch rule/registry code) |
| `make ci` | `check` ✅ 0 / `self-test` ✅ 0 / `quality` ✅ 0 / `org` ✅ 0; `test` target fails only on the same 19 lock-contention tests (environmental) |
| `python quality.py --strict .` | **OK** (after deviation 1) |
| Org gates | `pi-batch org check` / `capabilities check` / `graph extract` (inside `make org`) → exit 0, cycles: none |

**Could not run cleanly**: the 19 lock-dependent tests inside `pytest tests/`
and `make ci`'s `test` target (repo lock held by pid=1684832, our own
7×24 campaign; `--no-lock` override would bypass the very guard those tests
exercise — not used). Everything not lock-gated passes.

## 5. Self-check vs architecture.md §8 / evolution.md §6

- **§8 (module organization / dependency direction)**: no new module edges;
  gate lives in the validator module (`rule_cli`) that already owns registry
  checks; data removed from its owning registry; no domain imports infra.
- **§6 (evolution / ADR / tech debt)**: change is reversible (rollback = 4
  tracked files `git checkout` + manual revert of 3 blocks in the untracked
  `rule_cli.py`); no persisted state, no schema migration; tech debt paid
  (the polluted rule) while the durable protection (class-level gate) is the
  structural fix; decision recorded in the run's DECISIONS.md via pipeline.
- **Over-engineering red lines**: no interface/factory/Base class added; the
  one helper extraction exists to satisfy the org gate, not speculation; no
  speculative extension points (out-of-scope cross-domain union hardening
  explicitly *not* built, per requirements §3).

**Not committed** — per pipeline instructions. The change sits in the working
tree alongside the pre-existing dirty state; the acceptance stage gate will
verify against the live tree.
