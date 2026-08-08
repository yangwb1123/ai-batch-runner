Spec written: `docs/auto/requirements/evals-85368cf1-requirements.md` (following the campaign convention used by `checks-d4b833b0-requirements.md`).

## Verification summary

**All 9 cited references confirmed** (minor line drift: `asserts` read at eval.py:89, `_suite_domains` at :120, rules.yaml expect blocks at :75/:86/:94/:104; `classify_text` at classifier.py:206 and `match_rules` at rule_matcher.py:202 exact).

**Problem confirmed live:** `python -m pbatch eval --json` → `total: 41, failed: 0` — the 10 expect-only cases (6 classifier + 4 rules) pass with zero assertions; `system_type`/`includes`/`excludes` would raise `ValueError: unknown eval assertion key` if placed in `assert`.

**Three evidence-based corrections to the direction:**

1. **Count: 40 → 41** (6 assess + 18 classifier + 17 rules); 10/41 = 24.4%.
2. **All 6 classifier system-type expectations verified to match actual `classify_text` output** — they'll pass once `system_type` is a dispatchable key. They also lack `id:` keys (report as `classifier/?`, untargetable by `--filter`) — spec requires renaming `name:` → `id:` to make A1 testable.
3. **Critical divergence the direction missed:** 2 of the 4 `rules.yaml` expect blocks **don't match actual machinery behavior** — the workbench input classifies as `unknown`/generic (tier `standard`, 0 rules, because the generic registry has no production scale keywords), and the backend input classifies as `frontend_ui` (the word "流程" flips it), so none of the 6 backend di ids are present. Naively enabling `includes`/`excludes` would fail these cases and break CI (`make eval`, `test_entrypoints.py:52`). The spec aligns the two inputs to evidence-verified variants already proven by `tests/test_design_intelligence.py` — an input-text correction, keeping the original `includes`/`excludes` expectations and **explicitly scoping out** classifier/rule_matcher behavior changes.

**Deliverable contents:** R1–R7 requirements (expect-as-assertions execution, new `system_type`/`includes`/`excludes` dispatch keys, fail-closed exit 2 for vacuous cases, id renames, input corrections, green-suite guarantee, new pytest coverage), the 4 supplied acceptance criteria made testable (A1–A4, with concrete `--filter` commands and exit codes), scope boundaries, and verification commands.
