Design complete: `docs/auto/designs/scripts-8f645734-design.md`.

## Verification result — evidence is trustworthy

Every claim in the requirements spec was independently re-verified against the live repo. **All material claims confirmed**, with live reproductions:

- **Pollution paths reproduced**: backend `--dir . --json` (skipped) and `--dir <go.mod fixture> --json` (OK) both fail `json.loads` with `JSONDecodeError: Extra data`; frontend `--dir examples --json` same. Violation paths are already pure (summaries at stderr).
- **Mis-parse reproduced**: `--skip scratch` on frontend → `files_scanned: 3` with `'--skip'`/`'scratch'` as phantom "missing target" violations; backend → 0→2 (mechanism identical, differing only in the spec's fixture numbers — mine used `.py` under a `go.mod` manifest which isn't a backend source).
- **Consumers verified**: `health.py:48-50/55-57` special-case + `raw_decode`; `advance.py:97-108` silently degrades to `{}`; `pi-batch.yaml:131` family precedent — plus a stronger one the spec didn't cite (`backendexperience` at :132 also uses `--skip`).
- **Migration precondition verified**: the other two checkers health.py parses (design-intelligence, knowledge-freshness) are stdout-pure on OK/empty/modules paths, so `raw_decode` removal is safe; no existing test pins the polluted behavior or imports the changed internals; 91 tests green on baseline.

## Design highlights

- **API-1** `--skip NAMES` on both checkers: union (never replace) with `DEFAULT_EXCLUDES`, segment-exact matching inherited from `scan_directory`'s existing predicate, `--skip ""` no-op, explicit file targets unaffected.
- **API-2** `--json` stdout purity: `file=sys.stderr if want_json else None` on the two fall-through summary lines — byte-identical human mode by construction, `-o`-only untouched (condition on `want_json`, matching ui-spec exactly).
- **API-3** health.py: uniform `--skip SKIP` + plain `json.loads`; `-1` → `healthy: False` is the documented fail-closed alarm for any future regression.
- **Failure modes F1-F9** including the replace-not-union trap (caught by AC3's negative control), trailing-`--skip` edge (consistent with the file's `--dir` convention, visible not silent), and the `/tmp/go.mod` test-fixture trap.
- **7-step migration** with file/line anchors, no rollout concerns (additive flag, no existing invocation passes `--skip`).
- **Acceptance mapping**: all 5 ACs pinned to 8 concrete tests with fixtures and exact assertions, plus C1/C2 constraint guards; no existing test needs modification.
