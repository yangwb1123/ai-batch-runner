QA lead review complete — written to `docs/auto/runs/package-pbatch-data-files-role-keywords-yaml-tas-8807d80f/artifacts/adversarial_review-9c87f3a7/meta/qa_lead.md`.

## Summary

**What I ran (12 live checks, Python 3.10.7 / setuptools 63.2.0):** reproduced the wheel gap (102 `.py`, 0 data), sdist gap (169 members, 0 YAML), AC-2 discriminator (`{}` from `/tmp` vs 34 roles from repo root), AC-3 non-discrimination (byte-identical output on broken wheel), the 3-term YAML-subset parity issue, and measured loader coverage (relevance 93%, classifier 78% — but **0%** on the manifest machinery; `tests/test_packaging.py` doesn't exist).

**Design claims verified:** the one-block `pbatch = ["*.yaml", "*.md"]` fix ships all 3 data files via pip-isolated wheel, in-process `build_wheel`, and `build_sdist`; installed YAML is byte-identical to repo; the sdist-based offline regression is a **faithful proxy** (wrong-key sdist is silently empty too). `make ci` not run — parallel campaign directions hold uncommitted edits in this worktree (declared per evidence standard).

**Key findings:**
- **F-QA1 (Medium)** — my own inspection first false-passed with a `'/pbatch/' in name` predicate; AC-1 needs exact member-name assertions (`'pbatch/role_keywords.yaml' in names`), never slash-substrings.
- **F-QA2 (Medium)** — F1 is silent only for the underscore typo (`ai_batch_runner` → builds successfully, empty wheel+sdist, verified); the hyphen form fails loudly. The sdist test discriminates both.
- **F-QA3 (Medium)** — in-suite builds regenerate `egg-info/SOURCES.txt` in the tree (stale SOURCES.txt already exists here); build from a `tmp_path` copy with expected files globbed from the repo.
- **F-QA4 (Medium)** — AC-2 venv smoke has an undeclared network dependency (PyYAML); `--no-deps` would false-fail it.
- **F-QA5 (Low)** — `python -I` removes cwd from `sys.path` on 3.10 (verified), making the neutral-dir check self-defending beyond `cd /tmp`.
- **F-QA6 (Low)** — in-suite wheel test silently skips (no `wheel` pkg); add `wheel` to dev extras.
- **F-QA8 (Info)** — runtime silence when data is missing stays deferred; regression suite is the compensating control.

**Exit criteria:** capture a red `tests/test_packaging.py` run pre-fix, then AC-1–AC-4 + parity + classify guards green, full `make ci` on an isolated worktree, and no files changed beyond `pyproject.toml` + the new test file.
