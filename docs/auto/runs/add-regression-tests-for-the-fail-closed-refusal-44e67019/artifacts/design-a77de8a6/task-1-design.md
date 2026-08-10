Design written to `docs/requirements/scripts-check-no-refusal-design.md`. Summary:

## Verification results — all evidence claims confirmed

| Claim | Verdict |
|---|---|
| E1 gate contract (exit 0/1/2, 10 markers, `REFUSAL-CHECK`) | ✅ read source directly |
| E2 byte-identical duplicate (1414 B, `diff` IDENTICAL) | ✅ both git-tracked |
| E3 zero test coverage | ✅ grep exit 1 |
| E4 not in 23-entry validator registry | ✅ `yaml.safe_load` count |
| E5 8 `validate_cmd` refs / 4 pipelines | ✅ 5+1+1+1, exact lines |
| E6 `{cwd}` → workdir → canonical copy | ✅ `runner.py:487`, `pipeline.py:604` |
| F1–F6 (selfcheck gaps, VALIDATORS exposure, markers, `python`/`python3`, README, test conventions) | ✅ all |

**Two gaps the evidence missed:** (1) `AGENTS.md:70` already says "6 个验证器" but the registry has 23 — DOC-1 must set the truthful count **24**, not 7; (2) `pi-batch check --steps <unknown>` silently exits 0 with "all 0 gates passed" — a no-op trap the spec's A4.3 demands closing.

## Design highlights

- **API changes**: 1 additive `refusalcheck` validator entry in `pi-batch.yaml`; `pbatch/selfcheck.py` gains an in-process `_run_refusal_step(validators=None, script=None) -> bool` callable step (unit-testable fail-closed), registered in `_default_steps`, plus `--steps` unknown-name rejection (exit 2); new `tests/test_no_refusal.py` (~19 cases); `git rm` of the example copy; one `AGENTS.md:70` line.
- **Compatibility**: gate script gets **zero** edits (NG1); `_run_step` signature untouched (existing `tests/test_org.py` pins it — verified it stays green, including `--steps org,capabilities,tools` → "all 3 gates passed"); `--steps eval --skip-eval` semantics preserved; deletion breaks only the documented-never invocation.
- **Failure modes**: 13-item register with detector mapping (marker purge → A1.6, duplicate return → A2.1, registry removal → A3.1+A4.2+A4.3, step no-op → D2 hardening, etc.).
- **Migration**: 7 ordered, individually verifiable steps (test-first: A2.1/A3.1/A4.2 red → green in sequence), with rollback = revert of independent commit-sized hunks.
- **Acceptance mapping**: every A1.x/A2.x/A3.x/A4.x/A5.1 mapped to a named test or command with exact pass criteria; §6.1 byte-identity fallback branch specified verbatim as an alternative test.
