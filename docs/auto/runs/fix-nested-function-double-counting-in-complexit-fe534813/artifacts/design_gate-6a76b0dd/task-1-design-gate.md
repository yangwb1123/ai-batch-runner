# Design Gate — nested-function double-counting fix (cycle: requirements a050a42 → design 146efe7 → adversarial_review 770adbc)

I independently re-verified the state on disk rather than trusting the transcripts. This is the **first gate on this cycle's design revision** — the only design_gate commit in git history (`39a6021`) predates the current requirements/design/review chain, and the run directory contains no `design_gate-*` artifact for this cycle.

## Live verification performed

| Check | Result |
|---|---|
| `wc -l task-1-design.md` | **19 lines** — artifact is a summary, unchanged since adversarial review |
| `wc -l requirements.md` | **15 lines** — claims "7 functional requirements" without enumerating them |
| Read both artifacts in full | They *name* a 9-case matrix, 6 failure modes, step sequence, and FR re-enumeration — **none of that content is on disk**. The three cognitive return-path swaps (BoolOp/Match/generic) are asserted, not specified. The M1 parent-filter pin (vs. root-skip / root-only variants), flat if-chain (cyc==3) assertion, class-in-function and decorator-attribution subTests are absent |
| `git log --oneline -5 -- <run dir>` | Last commit `770adbc adversarial_review`; **no design_gate commit or artifact for this cycle** |
| Bug reproduction (my own fixture) | outer cyc=5/cog=4 (my inner has 2 branches), inner 4/3, `_functions` collects `['outer','inner']` — double counting confirmed live |
| `pytest checks/ -q` | **49 passed** (0.18s) — matches all reviewers |
| `git status -- checks/` | `complexity.py` / `test_complexity.py` unmodified (only untracked `test_knowledge_freshness.py` from another campaign) — implementation does not exist yet |
| Implement prompt (`examples/repository-campaign-pipeline.yaml:52-77`) | Says "Add focused regression tests" — **no reproduce-first mandate** (write test → confirm FAIL → fix), no requirement to quote pre-fix failing output (M3 unaddressed) |

## Disposition of the blocking finding

The consolidated blocking finding **H1** (qa_lead High; principal_reviewer High; testing_reviewer F1 Medium; algorithm_reviewer §4) — *design artifact does not contain the design it claims* — is **neither resolved nor explicitly rejected**:

- No revised design artifact exists (still 19 lines, last commit is adversarial_review).
- No design_gate ruling has occurred on this revision, and no reviewer or maintainer rejected H1 with evidence; principal_reviewer's trade-off ledger explicitly chose **(b) hold at design_gate** — "the gate exists precisely for this; transcripts are not consumable by the implement task."
- The implement stage consumes this artifact (`from_outputs: requirements, design, adversarial_review, design_gate`); with the matrix and failure-mode details absent, matrix cases (def-in-if/match, class-in-function, decorator attribution, Match/BoolOp prune paths) can be silently dropped with no detection — a fail-open risk all three reviewers independently confirmed.
- The related gate preconditions (M1 formulation pin, M2 AC1 subTest enumeration, M3 reproduce-first in implement prompt, M5 acceptance extended to `cli.py check`/`check-test`, M4 quality.py follow-up record) are likewise all still open — no evidence of any of them on disk.

The technical approach itself has **High-confidence support** (three independent prototype re-derivations agree on expected values; I re-confirmed the bug, the 49-test baseline, and the unmodified `checks/` tree), but the gate contract requires the validated detail to be consumable by the implement stage, and it is not.

VERDICT: FAIL - blocking finding H1 is unresolved and unrejected: the on-disk design artifact (19 lines) still claims a 9-case matrix, 6 failure modes, step sequence, and FR enumeration it does not contain, no design_gate ruling or artifact revision exists for this cycle, and M1/M2/M3/M4/M5 preconditions remain unaddressed — the gate must fail so the fix loop materializes (or deletes) the claimed design content with the 12 verified expected values, pins the parent-filter formulation and AC1 subTests, mandates reproduce-first evidence in the implement prompt, and extends acceptance to the aggregate gates before implement consumes this artifact.
