Requirements specification produced: `docs/auto/requirements/agent-engineering-36bd39dc-requirements.md`

## Verification findings

Every cited symbol was checked against the current repo:

| Citation | Verdict |
|---|---|
| `evolution.yaml:31` — `pi-batch retro --actions {log}` | ✅ exact |
| `pbatch/org.py:57` — `_check_exists` token-blind (only `parts[1] in commands`) | ✅ exact (57–67) |
| `pbatch/org.py:166` — substitution only for `{task}/{code}/{file}` | ✅ params dict at :149, `cmd.replace` loop at :164–166 |
| `pbatch/org.py:161` — `_execute_check` | ✅ exact |
| `docs/AGENT_ORGANIZATION.md:256` | ⚠️ **miscited** — file is 247 lines; 「缺参不伪装通过」is at **:246**, placeholder contract at **:234** |

**Live reproduction confirmed**: `python pi-batch.py retro --actions '{log}'` → 1 "file" scanned, zero evidence, exit 0 → `_execute_check` marks it `passed`, `gate_passed=True`. And `org check` currently reports `valid: True` (17 nodes).

## Specification highlights

- **Root cause**: two independent layers both blind to placeholder tokens — static `_check_exists` never inspects args; runtime `run_gate`'s missing-param guard only checks *supported* keys, so `{log}` is neither skipped nor substituted and flows verbatim to the shell.
- **R1** static fail-closed: `org check` rejects unsupported `{name}` tokens (shared `SUPPORTED_PLACEHOLDERS` constant — D1, single source of truth to prevent future drift), problem names node + token, exit 1.
- **R2** runtime: unsupported token → `skipped` with reason `不支持的占位符 {log}`, never executed, never `passed` (pinned per the acceptance's "skipped with reason or failed").
- **R3** self-healing: `evolution.yaml:31` → `pi-batch retro --actions {code}` (retro accepts directories; zero new CLI parameters). This is **mandatory**, not optional: `make ci` runs `python pi-batch.py org check` (Makefile:47–50), so AC4 forces the yaml fix in the same change — a coupling made explicit in §6/§9.
- **AC1–AC4 preserved** and pinned to deterministic pytest assertions (fixture style modeled on the existing `test_org_check_detects_bad_command`), with the AC1 nuance that the nonzero-exit test must use a fixture `ORG_DIR` since the shipped tree must be valid post-R3.
- **Scope guard**: explicitly out of scope — new `{log}` parameter threading, `retro` semantics, and the other two directions in the analysis file (including the unreachable `_summarize` duplicate at org.py:194–202, noted as context only).
- **Appendix A**: full placeholder inventory — `{task}`×22, `{code}`×12, `{file}`×3, `{log}`×1 (the sole unsupported token), so R1 introduces no new failures on the shipped tree.
