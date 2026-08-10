Requirements specification complete and verified. Summary of the work:

**Deliverable**: `docs/auto/runs/unify-ui-token-single-source-of-truth-radius-4-8-a866d728/artifacts/requirements-10762e10/requirements.md` (201 lines, at the pipeline's expected artifact path for the `design` stage).

**Evidence verification** — every cited file/symbol checked against the repo:
- ✅ `tokens.json:15` — radius {sm:4, md:8, lg:12} (exact)
- ✅ `geometry.md` §4 (`:42`) — 6/10/16 family (analysis cited :38-43, minor drift)
- ✅ `check-ui-geometry.py:37` — `RADIUS_FAMILY = {6,10,16}` (**analysis cited :31 — corrected**)
- ✅ `check-ui-spec.py:42` — `SPACING_TOKENS` (**analysis cited :37 — corrected**)
- ✅ `spacing.md:12-22` — 8pt table (rows 13-22)

**5 new evidence items found** that strengthen the case without expanding scope:
- `prompts/ui_designer.md:25` mandates {6,10,16} while `:4` tells agents to load tokens.json — an internally contradictory *fourth* copy that would keep retry churn alive unless fixed
- `component-spec.md:4` already declares tokens.json normative ("不得出现 token 以外的值") with radii 8/12 that the current gate rejects — decisive for the decision
- `tests/test_ui_geometry.py:46,50,91` hardcode the family; gate error message at `:109` hardcodes it too
- `pi-batch.yaml:165` + both example pipelines attach uigeometry (confirmed registered/enforced)

**Decision**: tokens.json wins → radius family resolves to **{4, 8, 12}** (8px accepted). Rationale: component-spec.md already binds to tokens.json, keeping the gate at 6/10/16 would preserve the exact harm the direction describes, and 4/8/12 stays on the 4pt grid of the spacing system.

**Scope discipline**: all 4 supplied acceptance checks preserved verbatim and made testable (A3 resolves to the "8px accepted" branch with concrete exit-code fixtures); directions #2 and #3 from the analysis file, and any new typography/component enforcement gates, are explicitly out of scope.
