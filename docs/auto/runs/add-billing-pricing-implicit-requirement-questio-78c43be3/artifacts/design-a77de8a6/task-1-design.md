Design complete. Summary of what was done:

## Evidence verification — all claims confirmed

Every citation in the requirements evidence was re-verified against the live tree:

| Key claim | Verdict |
|---|---|
| `_SCENARIOS` @ product.py:58-96, six entries, no billing | ✅ exact |
| `scenario_questions()` (134-142) sole producer of `product.scenarios` | ✅ exact |
| `rules.yaml` billing use_case_type (evidence said 30-32) | ✅ at **30-31** (minor off-by-one); billing rule @57-64 double-gated confirmed |
| Gap: L3 + `scenarios: []` for "SaaS 平台，订阅收费，按配额计费" | ✅ reproduced live (`level: L3_product_feature`, `evidence: [saas]`, `scenarios: []`) |
| Eval case `l3-prescription-no-billing-without-signal` green | ✅ `pi-batch eval` → all 42 passed |
| `_keyword_hit` CJK-substring / Latin-word-boundary semantics | ✅ |
| 15 existing tests; 181-line product.py | ✅ |
| R1 keyword list ⊆ `use_case_types.billing` | ✅ programmatic subset check |

**Simulated the proposed R1 entry against the live matcher**: A1 input → exactly `['计费/订阅']`; "商业化"/"商业化收费" inputs → only `['上传/文件']` (no billing false positive); `planning`/`tier-1` → no Latin false hits.

**One honest nuance found**: the eval case guards only the *rules/prescription* layer — it would **not** catch a forbidden keyword (`收费`) reintroduced into the *scenario trigger*. The mechanical tripwire is R3 test 3; documented as FM-2.

## Deliverable

`docs/auto/design/product-specs-82a451b3-billing-question-chain-design.md` (sibling of the stale direction-#1 doc, same unique-name convention) containing:

- **§1** verification report (16-row table, all verdicts)
- **§3** concrete design: the exact `_SCENARIOS` entry, doc row, and three test specifications
- **§4** API changes table — purely additive behavioral data; no signature, CLI, schema, or config changes; `product_manifest(text, level=…)` override path untouched
- **§5** compatibility constraints (C1-C4) + 9 failure modes (keyword drift, doc drift, ordering, L0 regression, Latin false positives, multi-scenario collision, fail-closed registry degradation, file-budget gate)
- **§6** migration: 6 ordered steps, single 3-file commit, complete rollback with zero residue
- **§7** acceptance mapping: A1/A2/A3 as exact commands, each pinned to a unit test + integration command, plus the four regression gates
- **§8** scope boundary preserved (directions #1/#3 out of scope)
