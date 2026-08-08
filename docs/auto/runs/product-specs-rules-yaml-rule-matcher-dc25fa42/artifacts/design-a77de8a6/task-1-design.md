Design complete and delivered at `docs/auto/design/product-specs-82a451b3-design.md`. Summary:

## Verification (all evidence claims re-checked live)

| Claim | Result |
|---|---|
| 21 environmental failures, lock exit 5, pid=1684832 | ✅ **Reproduced exactly** — ran full suite: `21 failed, 956 passed, 1 skipped`; error text quotes `pid=1684832, campaign --jobs 4, exit 5`; live campaigns confirmed in `ps` |
| `test_product.py` 13/13, `eval` 41/41, `rules --check` 2 domains | ✅ All live-verified |
| `rules "做一个开源上传产品，商业化"` → generic, 0 rules (A3 fails today) | ✅ Live: `domain: generic, rules: []` |
| `product.py:85` `_SPECS_BY_LEVEL` / `rule_matcher.py:42-43,100` / `rule_cli.py:122` | ✅ Exact line matches |
| Merge-inheritance trap, import-cycle constraint, L0 restraint, empty-`signals` second guard, billing double-gate | ✅ Verified by code path + **live simulation** (monkeypatched, zero repo changes): F1 leak reproduced unfixed / clean fixed; derived spec sets set-equal for all 4 levels; A3/A4 paths behave as specified |

## Discrepancies found (flagged, not silently accepted)

1. **"tests/ 387" is stale** — live suite is 978 cases (AGENTS.md, CHECKS_REGISTRY.md, and the requirements doc all repeat it).
2. **eval.py line cites drift** — `_assert_value` is at :101 (not 104–122); the "set-semantics tuple" is three sites (:101, :147/:153, :173), not one at :138.
3. **Vacuous A1** — `rules --check` passes with a *deleted* registry file (empty rules → zero violations). Design adds a non-empty guard (R4′).
4. **Order drift** is at L2 and L3 (doc claimed L3 only) — still unpinned, set-equality holds.

## Design deltas vs the requirements doc (all testable)

- **D1** non-empty registry guard → A1 becomes meaningful
- **D2** eval case gains `lacks_rule: [visual-core]` → merge-inheritance hazard pinned to CI
- **D3** parity pin (L3 == 4 files, L0 == `[]`) upgraded from optional to **required** → guards future silent spec-set drift
- **D4** eval R5 corrected to three edit sites
- **D5** acceptance numbers use the live 978 count

The design covers all requested sections: per-file API changes with signatures (incl. the lazy-import cycle constraint), compatibility constraints (signature stability, precedence, packaging, fail-open contract), 9 enumerated failure modes with detection/mitigation, 8 ordered migration steps each independently verifiable with rollback notes, and a command-level acceptance mapping A1–A4 plus F1/F6 pins.
