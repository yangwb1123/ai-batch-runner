All verification complete. Adjudication record for the gate:

---

# Adjudication — QA F1: `--skip` swallowing a following flag

## Decision: **HARDEN** (reject flag-like values, exit 2)

### Evidence that overturned "accept-and-document"

The QA report justified accept-and-document partly on "argparse 家族同款" — **empirically false**. I ran the family live:

| Parser | `--skip --json` behavior |
|---|---|
| `check-backend-experience.py` (argparse) | **exit 2** "expected one argument" |
| `check-design-intelligence.py` (argparse) | **exit 2** |
| `check-knowledge-freshness.py` (argparse) | **exit 2** |
| `check-backend-quality.py` (hand-rolled, pre-fix) | **exit 0, silent human mode** ← F1 |
| `check-frontend-quality.py` (hand-rolled, pre-fix) | **exit 0, silent human mode** ← F1 |

The `--dir` precedent cited is also not silent: `--dir --strict` fails visibly (scan error, exit 1) because a bogus path errors. `--skip` is the only value flag whose swallowed value is *syntactically valid* — any string is a legal skip segment — which is exactly why it was the family's sole silent false-pass member. The argparse rule (verified: rejects any dash-leading value except bare `-` and negative-number-like) is the true family convention, and the design's own fail-closed philosophy (this whole direction is "machine contract must not silently degrade") forbids keeping exit-0-on-clean-input.

### What was implemented (minimal-change, no argparse migration)

One guard in the **shared value-taking branch** of both `parse_argv` loops (root cause, not just `--skip`):

```python
if nxt.startswith("-") and nxt != "-":
    print(f"error: {arg}: expected one argument (got {nxt})", file=sys.stderr)
    raise SystemExit(2)
```

Verified live: `--skip --json`/`--skip --strict` → **exit 2** on both checkers (was silent exit 0); the same class is closed for `--manifest --json` and `-o --json` (both also silent pre-fix; argparse family rejects these too). `--dir --strict` changes from visible-scan-error exit 1 → usage-error exit 2 (strictly more consistent, nothing pins the old code). Unchanged by design: trailing `--skip` with no value → visible "missing target" (exit 1, design F3 as documented); bare `-` value stays legal per family convention.

### Confirmed contract shapes (unchanged by this adjudication)

1. **Union semantics** — `DEFAULT_EXCLUDES | {s for s in skip.split(",") if s}` (never replace; AC3 negative control pins it; constant never mutated). **Confirmed correct.**
2. **Comma-separated value format** — matches design-intelligence/backend-experience family. **Confirmed correct.**
3. **`--json` stdout purity** — exactly one JSON doc, summaries via `file=sys.stderr if want_json else None`; C1/C2/R2 byte-identity guards hold. **Confirmed correct.**

### Verification

- `tests/test_backend_specs.py tests/test_frontend_quality.py tests/test_tools_health.py tests/test_ui_check.py` → **102 passed** (91 baseline + 9 prior new + 2 new F1 tests)
- New `test_backend_flaglike_value_rejected` (4 flag pairs + bare-dash legal), `test_frontend_flaglike_value_rejected` (2 pairs + bare-dash legal)
- `quality.py` on both checkers: **OK** (complexity budget intact)
- Health consumer unaffected: `pbatch/health --json` renders; if a future config ever passed a dash-leading `--skip` value, exit 2 → `json.loads` fails → `-1` → `healthy: False` (design F4 alarm, exactly as designed)
- Design doc F3 row updated; adjudication appended to `docs/auto/runs/cli-json-stdout-skip-json-0f442e49/DECISIONS.md`

**Gate recommendation**: PASS with F1 resolved by hardening — the remaining QA items (F2/F3/F4/F6 test additions) are coverage suggestions, not blocking; F1 was the only open contract question and is now closed with tests and documentation.
