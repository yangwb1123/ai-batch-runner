All design preconditions are now verified against live module state. The design artifact is complete at the pipeline-specified output path:

**`docs/auto/runs/gate-placeholder-validation-unsupported-log-plac-3b7b1b9f/artifacts/design-a77de8a6/task-1-design.md`** (425 lines)

## Evidence verification summary

Every requirements claim was independently re-verified — all confirmed, including the requirements' own correction of the `AGENT_ORGANIZATION.md:256` miscite (content at **:246**, contract at **:234`), the dead-code `_summarize` duplicate (org.py:194–202), the 38/38-token placeholder inventory (`{log}` is the sole unsupported token), and the `make ci`/`pi-batch check` → `org check` coupling (Makefile `org:` target; selfcheck.py:38).

## Design highlights

- **D1 shared constant** `SUPPORTED_PLACEHOLDERS = ("{task}", "{code}", "{file}")` + regex scanner `\{[A-Za-z_][A-Za-z0-9_]*\}` — empirically zero false positives on the tree (no `${VAR}`/brace-collision today), plus an anti-drift `assert` in `run_gate`.
- **R1 static**: additive scan in `org_check`'s auto_checks loop → problem `evolution: auto_check 占位符不支持: {log}`, `valid=False`, CLI exit 1 (no `_check_exists` rewrite, minimal blast radius).
- **R2 runtime**: skip branch **before** the 缺参 guard → `skipped` with pinned reason `不支持的占位符 {log}`, never executed, never `passed`; supported placeholders byte-identical (verified: 缺参 guard still fires for `{task}`).
- **R3 self-healing**: `evolution.yaml:31` → `pi-batch retro --actions {code}` (retro accepts dirs via `rglob("*.log")`), mandatory same-change landing due to AC4.
- **Failure modes F1–F7** table (stale-config skip, drift, over-blocking, partial-landing CI break, shell-syntax collisions) with mitigations; **migration** = one commit, ordered verify steps, clean rollback; **acceptance mapping** pins AC1–AC4 to T1a/T1b/T2/T3 with exact assertions.

One real bug caught during verification: my first fixture draft omitted `role`/`role_template` from `REQUIRED_KEYS`, which would have polluted the pinned assertions — fixed to a fully-valid fixture and confirmed `org_check` on it yields *only* the intended problem class.
