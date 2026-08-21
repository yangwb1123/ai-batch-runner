# Gate decision contract

Gate artifacts may remain Markdown, but a new pipeline can include one
structured JSON decision block:

```json
{
  "schema_version": 1,
  "verdict": "PASS",
  "reason": "All acceptance checks are satisfied.",
  "evidence": [
    "tests/test_order_flow.py",
    "artifacts/quality-report.json"
  ],
  "validators": [
    {"name": "pytest", "status": "PASS", "detail": "24 passed"}
  ],
  "findings": []
}
```

The contract is parsed from a ` ```json ` fence. `schema_version` is `1`.
`verdict` is `PASS`, `FAIL`, or `REJECT`; `reason` is always required.
Structured `PASS` additionally requires at least one evidence reference and
one validator result. A `PASS` containing a `FAIL` or `REJECT` validator is
invalid and fails closed.

Structured findings use the bounded fields `severity`, `file`, `line`,
`problem`, and `fix_hint`. They are available to the Gate-fix loop.

Existing artifacts using `VERDICT: PASS - reason` remain valid for backward
compatibility. If an artifact declares a structured contract but its JSON is
malformed or violates the contract, the runner does not fall back to a text
verdict.
