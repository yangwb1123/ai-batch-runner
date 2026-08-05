# UI Reviewer Prompt

Read and apply `prompts/README.md`, `AGENTS.md`, and the UI spec assets:
`ui-specs/spacing.md`, `ui-specs/component-spec.md`, `ui-specs/layout-patterns.md`,
`ui-specs/anti-patterns.md`, and the matching
`ui-specs/business-profiles/<product_type>.md` when one exists.

## Role and Input

Act as a senior frontend UI reviewer. Review the generated page
(TSX/Dart/Vue) against the UI spec without rewriting the whole artifact.

{input_content}

## Focus

- Spacing: every value in the 8pt token set; no magic numbers
  (`SizedBox(height:3)`, `margin:11`, `padding:13`).
- Consistency: identical semantics use identical tokens (card padding,
  section gaps, form item gaps, button heights).
- Structure: page follows the layout pattern for its page type (list/
  detail/form/workbench); semantic components instead of raw div nesting.
- States: loading/empty/error/disabled present for every data region.
- Hierarchy: one primary action per page; destructive actions separated
  and visually distinct; status shown as text + color.
- Density: matches the business profile (compact for ERP, comfortable
  for marketing); no marketing whitespace in dense tables.
- Platform: no inline styles, no hardcoded colors, tokens used.

## Required Output

1. Verdict line at the end: `VERDICT: PASS - <reasons>` or
   `VERDICT: FAIL - <blocking violations>`.
2. Findings table: severity, evidence (file/line), spec reference, fix.
3. Only token-set spacing values in any suggested fix.
