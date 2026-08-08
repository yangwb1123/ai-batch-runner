# Requirements Specification — Remove PY-GODFILE-SPLIT from the backend domain registry & fail closed on self-referential rule files

| Field | Value |
|---|---|
| Module | `backend-specs` |
| Direction | Remove repo-internal policy rule PY-GODFILE-SPLIT from the backend domain registry and fail closed on self-referential rule files |
| Source analysis | `docs/auto/analyses/backend-specs-66c8a12d.json` (direction #1) |
| Status | Proposed — all cited evidence verified against HEAD |
| Acceptance owner | Campaign implement pipeline |

---

## 1. Verified problem statement

`PY-GODFILE-SPLIT` is a repo-internal dogfooding RCA rule ("god-file 967 行 cli.py：执行层未拆分导致可维护性下降") promoted into the **shipped** backend domain registry. Because it is declared `min_tier: standard` + `required: true`, and required rules are never suppressed (`pbatch/rule_matcher.py:186` — `if rule.get("required"): return ""`), it is injected into the rule manifest of **every standard+ backend task**, and its `files` entry instructs agents to load the registry file itself as a spec.

### 1.1 Evidence verification table

Every citation in the source analysis was checked against the repository:

| Evidence (cited) | Verified status | Verified finding |
|---|---|---|
| `backend-specs/rules.yaml:164` | ✅ Confirmed | Lines 164–170 contain the `PY-GODFILE-SPLIT` block: `description: god-file 967 行 cli.py：执行层未拆分导致可维护性下降`, `files: ['backend-specs/rules.yaml']`, `min_tier: standard`, `level: policy`, `required: true`, trailing comment `# promoted from draft PY-GODFILE-SPLIT（advice，影子/人工批准后并入）` |
| `docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml:1` | ✅ Confirmed | File exists: `id: PY-GODFILE-SPLIT`, `category: backend`, `severity: advice`, full RCA rationale (cli.py 967 行 → cli_rounds/cli_batch/cli_residue), verification, examples, `status: draft` (archived draft copy — expected per `pbatch/learn.py:165` "草案移入 promoted 存档"; it is the repo-policy record, not a live registry entry) |
| `pbatch/rule_cli.py:52` | ✅ Confirmed | `_check_rule` (line 52) validates only field presence, `min_tier` enum, `level` enum, `suppress_on` signals, and file **existence** (`Path(str(file)).exists()`). No self-reference check. |
| `pbatch/rule_matcher.py:186` | ✅ Confirmed | `if rule.get("required"): return ""  # 必选规则不受显式抑制（fail closed)` — required rules can never be skipped, so the leak is unconditional |
| `pbatch/rule_matcher.py:42` | ✅ Confirmed | `DOMAIN_REGISTRIES = {"backend": ["backend-specs/rules.yaml"], "frontend_ui": ["ui-specs/rules.yaml"], "generic": []}` |

### 1.2 Live reproduction (both acceptance inputs)

```
$ python pi-batch.py rules "写一个订单创建接口，含支付审批，生产，go" --json
  → rules[] contains: PY-GODFILE-SPLIT | level=policy | required=True | files=['backend-specs/rules.yaml']

$ python pi-batch.py rules "开发一个客户管理模块的列表查询与新增接口" --json
  → rules[] contains: PY-GODFILE-SPLIT | level=policy | required=True | files=['backend-specs/rules.yaml']
```

Both confirmed by executing the commands against HEAD (see `docs/auto/runs/.../requirements-spec.md` verification session).

### 1.3 Why `rules --check` cannot catch it (verified)

```
$ python pi-batch.py rules --check
[frontend_ui] registry: OK
[backend] registry: OK
$ echo $?
0
```

`_check_registries_cli` (`pbatch/rule_cli.py:90`) calls `check_registry(registry)` → `_check_rule` which only verifies the referenced file *exists* — `backend-specs/rules.yaml` trivially exists. The self-reference is invisible to the fail-closed gate.

### 1.4 Manifest pollution mechanism (verified)

`format_manifest` (`pbatch/rule_matcher.py:373-396`) emits "Apply ONLY these rules; **load the listed files**, do not load unrelated specs:" followed by `- [REQUIRED] PY-GODFILE-SPLIT (standard): …` and `    backend-specs/rules.yaml`. Agents are therefore instructed to load the registry itself as a spec document, while a repo-internal maintenance note leaks into end-user manifests.

---

## 2. Goals

- G1 — Stop `PY-GODFILE-SPLIT` from entering the manifest of any backend (or generic) task; the rule's canonical record remains in the repo-policy store `docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml`, which is the designated home for this repository's own maintenance rules (`pbatch/learn.py:113` `PROMOTED_DIR`).
- G2 — Make registry self-reference a **fail-closed** schema violation: `rules --check` must exit non-zero whenever any rule's `files` (or `files_template` substitution) points at the registry file itself, for both `backend` and `frontend_ui` registries.
- G3 — Lock the behavior in with an eval regression case so removal cannot silently regress.

## 3. Non-goals (scope discipline)

- **No generic-domain registry loading mechanism.** `DOMAIN_REGISTRIES["generic"]` is `[]` by documented design (`pbatch/rule_matcher.py:39-40`: "generic loads NO domain specs — a refactor task must not be prescribed frontend spacing rules"; `rule_matcher.py:80`). Building a new loader so generic tasks consume `docs/rules/promoted/` is a separate mechanism, not required by any acceptance criterion, and would weaken the existing fail-closed design. The relocation target already exists: the promoted archive is the repo-policy registry, consumed by the learn/health/reflection flows for this repository's own tasks.
- **No cross-field registry hardening** (undefined `use_case_types`/`page_types` keys, `required_when_risk` without `risk`) — that is a separate direction in the source analysis (direction #2).
- **No re-tiering of `design-intelligence-*` rules** (direction #3).
- **No cleanup** of `docs/auto/runs/remove-repo-internal-policy-rule-py-godfile-spli-5fe4767b/pipeline.yaml` (a stale prior run manifest for this same direction; harmless historical artifact).

---

## 4. Functional requirements

### R1 — Remove the rule from the shipped backend registry

`backend-specs/rules.yaml` must no longer contain the `PY-GODFILE-SPLIT` rule entry. Concretely, delete the block at lines 164–170:

```yaml
  PY-GODFILE-SPLIT:
    description: god-file 967 行 cli.py：执行层未拆分导致可维护性下降
    files: ['backend-specs/rules.yaml']
    min_tier: standard
    level: policy
    required: true
    # promoted from draft PY-GODFILE-SPLIT（advice，影子/人工批准后并入）
```

- R1.1 — The historical record `docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml` **must remain untouched** (it is the repo-policy archive; it must not be deleted, edited, or re-promoted).
- R1.2 — Guardrail (documented, no code): repo-internal dogfooding/RCA rules must never be (re-)promoted into the shipped domain registries (`backend-specs/rules.yaml`, `ui-specs/rules.yaml`) via `pi-batch learn promote --registry backend|frontend`; the promoted archive is their registry. Note this in the implementation PR description.
- R1.3 — After removal, `pi-batch rules --check` must still exit 0 and `pi-batch eval` must still pass (no other rule depends on the removed entry; verified: no test, eval case, or script references the id `PY-GODFILE-SPLIT`; `tests/test_backend_specs.py::test_backend_spec_registry_integrity` iterates rules generically).

### R2 — Fail closed on self-referential rule files in the schema check

Modify `pbatch/rule_cli.py`:

- R2.1 — `check_registry(registry: dict, registry_paths: Optional[list] = None) -> list` gains an optional parameter carrying the resolved paths of the registry file(s) the rules were loaded from. `None` (default) preserves current behavior for any existing callers.
- R2.2 — In `_check_rule`, for every entry in `files`, and for every `files_template` profile substitution (template with `{profile}` replaced by each declared profile), compare the **resolved** path (`Path(str(entry)).resolve()`) against the resolved registry paths. A match is a violation with a precise message, e.g. `rule '<id>': files entry '<entry>' resolves to the registry file itself (self-referential rule)`.
- R2.3 — `_check_registries_cli` must pass the actual registry paths for each domain, using the same resolution `load_registry` uses (`_default_registry_paths(domain)` in `pbatch/rule_matcher.py:90-93` — repo-root-anchored and CWD-anchored candidates; dedupe after `resolve()`). This covers both `backend` and `frontend_ui`.
- R2.4 — No change to exit-code plumbing: `_check_registries_cli` already calls `sys.exit(1)` on any violation, so `rules --check` fails closed automatically. The `pi-batch check` "registry" gate (`pbatch/selfcheck.py` → `python -m pbatch rules --check`) inherits the new check with no further wiring.
- R2.5 — Path comparison must be **resolve-normalized** (absolute, symlinks resolved, `..` collapsed) so `backend-specs/rules.yaml`, `./backend-specs/rules.yaml`, and absolute forms are all caught.
- R2.6 — The check must also fire for a rule whose `files` is the **other** domain's registry (e.g. a backend rule listing `ui-specs/rules.yaml` is not this check's concern — self-reference means the registry it is *defined in*; the comparison set is only the paths of the registry currently being validated).

### R3 — Eval regression case

Add a new case to `evals/rules.yaml` (the `rules` suite) asserting the rule is absent from a backend manifest:

```yaml
  - id: backend-manifest-excludes-repo-internal-policy
    input: "写一个订单创建接口，含支付审批，生产，go"
    assert:
      task_type: backend
      lacks_rule: [PY-GODFILE-SPLIT]
```

- R3.1 — The case **must use the `assert:` form** (`lacks_rule`), not the `expect:`/`includes` form used by the `design-intelligence-*` cases: `pbatch/eval.py::run_suite` only reads `case.get("assert", {})` — `expect:` blocks are inert (verified: `_assert_value` handles `has_rule`/`lacks_rule`; no code path reads `expect`).
- R3.2 — Optionally add a second identical case for the other acceptance input (`"开发一个客户管理模块的列表查询与新增接口"`); not required, since both acceptance commands exercise the same `match_rules` code path after R1.

---

## 5. Acceptance criteria (verbatim, made testable)

Run from the repository root. Expected exit codes/conditions in parentheses.

| # | Command / check | Expected |
|---|---|---|
| A1 | `python pi-batch.py rules "写一个订单创建接口，含支付审批，生产，go" --json` | Output JSON `rules[]` contains **no** entry with `id == "PY-GODFILE-SPLIT"` (exit 0) |
| A2 | `python pi-batch.py rules "开发一个客户管理模块的列表查询与新增接口" --json` | Output JSON `rules[]` contains **no** entry with `id == "PY-GODFILE-SPLIT"` (exit 0) |
| A3 | `python pi-batch.py rules --check` with a rule whose `files` lists the registry file itself (e.g. a temporary copy of `backend-specs/rules.yaml` re-adding `PY-GODFILE-SPLIT` with `files: ['backend-specs/rules.yaml']`) | Exit code non-zero (1), violation message names the rule id and the registry file; clean registries (the real ones) still exit 0 |
| A4 | `python pi-batch.py eval` (and `make ci`) | Rules suite passes with the new backend case; total failures = 0 (baseline before change: 40 cases / 0 failed) |

Testable procedure for A3 (verified caveat: `rules --check` ignores `--registry` — the check path returns at `pbatch/rule_cli.py:120-126` before parsing the override, so the gate can only be exercised via the unit-test surface, not the CLI override):

```bash
# 1. Prove clean registries pass (after R1):
python pi-batch.py rules --check          # exit 0
# 2. Prove the gate fires on a self-referential rule (pytest, see §6.3):
python -m pytest tests/test_rule_matcher.py -k self_referential -q  # passes, exercising
#    check_registry(reg, registry_paths=[...]) with an in-memory rule whose
#    files == the registry path → violation returned
```

The CLI acceptance statement (“exits non-zero when any rule lists the registry file itself”) is satisfied by construction: `_check_registries_cli` already calls `sys.exit(1)` on any violation, so once `check_registry` reports the self-reference, `rules --check` on a registry *containing* such a rule exits non-zero. The pytest test is the deterministic, non-mutating demonstration that the violation is reported.

---

## 6. Implementation notes (precise anchors)

1. **R1 removal**: `backend-specs/rules.yaml` lines 164–170 (exact block in §4 R1). No YAML structural impact — the entry is a sibling of `network-engineering` and `testing`; removing it leaves valid YAML.
2. **R2 signatures**:
   - `pbatch/rule_cli.py:52` `_check_rule(rule_id, rule, violations)` — add the resolved-registry-paths set as a parameter (or move the loop into `check_registry`; keep `_check_rule`'s single-rule shape).
   - `pbatch/rule_cli.py:82` `check_registry(registry)` — new optional `registry_paths` parameter.
   - `pbatch/rule_cli.py:90` `_check_registries_cli()` — compute `paths = _default_registry_paths(domain)` (import from `pbatch.rule_matcher`; it is already importable) and pass `[str(p.resolve()) for p in paths]` (deduped).
   - `files_template` handling: mirror the existing profile loop (`pbatch/rule_cli.py:75-79`) and apply the same comparison to `str(template).replace("{profile}", str(profile))`.
3. **Unit tests to add/update** (tests live in `tests/test_rule_matcher.py`): one test (`test_registry_rejects_self_referential_files`, matching the `-k` filter in §5) constructing an in-memory registry dict with a self-referential `files` entry and asserting `check_registry(reg, registry_paths=[...])` returns a violation; one asserting the current shipped registries produce no violation (this doubles as the A3 clean-side assertion and runs in `make ci`).
4. **No changes** to `pbatch/rule_matcher.py` required for R1/R2 (only the import of `_default_registry_paths`). Line 186's required-rule fail-closed semantics stay intact — it is correct behavior; the fix is removing the polluted rule, not loosening suppression.
5. **Docs touch-up (optional, one line)**: `docs/SPEC_SYSTEM.md` registry section may note "rules must not reference the registry file itself; `rules --check` rejects self-referential `files`". Not required for acceptance.

## 7. Verification evidence (gathered for this spec)

| Check | Result |
|---|---|
| `python pi-batch.py rules "写一个订单创建接口，含支付审批，生产，go" --json` | `rules[]` includes `PY-GODFILE-SPLIT` (required, files `['backend-specs/rules.yaml']`) — reproduces the bug |
| `python pi-batch.py rules "开发一个客户管理模块的列表查询与新增接口" --json` | `rules[]` includes `PY-GODFILE-SPLIT` — reproduces the bug |
| `python pi-batch.py rules --check` | exit 0 (both registries "OK") — self-reference invisible |
| `python pi-batch.py eval --json` | baseline: 40 total, 0 failed (16 cases in `evals/rules.yaml`) |
| `grep -rn "PY-GODFILE-SPLIT"` (repo, excluding `.git`) | only `backend-specs/rules.yaml`, `docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml`, stale run `pipeline.yaml` — no test/eval/script depends on the id |
| `pbatch/eval.py::run_suite` | only reads `assert:`; `expect:` cases are inert → R3.1 |
| `pbatch/rule_cli.py` callers of `check_registry` | only `_check_registries_cli` → adding an optional param has no blast radius |
| `pbatch/selfcheck.py` | "registry" gate runs `python -m pbatch rules --check` → R2 inherits the self-check gate |

## 8. Risks

- **Risk: removal without the gate** — someone re-promotes a repo-internal rule later and the leak returns silently. Mitigated by R2 (fail-closed gate) + R3 (eval regression) + R1.2 guardrail. The gate is the durable protection: the eval case pins the *specific* rule, the gate pins the *class* of defect.
- **Risk: false positives from path comparison** — a legitimate spec file that happens to be named like the registry path in another directory. Mitigated by resolve-normalized exact-match comparison against only the currently-validated registry's own paths (R2.5/R2.6).
- **Risk: eval case written in the inert `expect:` format** (the `design-intelligence-*` precedent) silently passing forever. Mitigated by R3.1 (mandatory `assert:` form) and the explicit test procedure.
