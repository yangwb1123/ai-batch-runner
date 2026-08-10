All done. Final report:

---

## Implementation complete — 9 new tests, 100/100 green in affected files

### Classification & assumptions
- **Task**: backend/infra CLI-contract fix (pure-stdlib Python) per design `scripts-8f645734`. No UI code; the frontend engineer discipline applies to the checker contract itself: stdout = machine channel, stderr = retry-feedback channel.
- **Assumptions (inferred)**: repo root manifest-free (verified live) so `clean_repo` walk-up is safe; existing `raw_decode` in test files tolerates pure JSON so no test needed modification (verified); health `healthy: True` on this repo (verified live).

### Changes (footprint: 3 sources + 4 test files)
| File | Change |
|---|---|
| `scripts/check-backend-quality.py` | `--skip NAMES` (union with `DEFAULT_EXCLUDES`, segment-exact, `""` no-op, trailing flag → visible missing target); `--json` stdout = exactly one JSON doc (OK/skipped summaries → stderr via `file=sys.stderr if want_json else None`); `parse_argv` refactored to data-driven table (cognitive 23→11) to pass the repo's own complexity gate; file back to exactly 500 lines |
| `scripts/check-frontend-quality.py` | Same `--skip` + `--json` purity; same `parse_argv` refactor (24→11) |
| `pbatch/health.py` | Special case + `raw_decode` removed; all 3 checkers get uniform `--skip SKIP`, parsed with plain `json.loads`; any future stdout regression → `ValueError` → `-1` → `healthy: False` (fail-closed alarm, no silent misread) |
| 4 test files | AC1 contract (ok/skipped/violation fixtures), AC2 backend skip, AC3 frontend skip + replace-implementation negative control, AC4 health plain-JSON + `raw_decode` source guard, AC5 ui-spec regression guard, R2 frontend OK-path purity, C1/C2 non-json/`-o`-only byte-identity guards |

### Verification actually run
- `pytest` (4 affected files): **100 passed** (91 baseline + 9 new)
- `make check` (filesize/complexity/root/style): **green** (style WARNs pre-existing, non-failing)
- `make self-test`: exit 0 · `python quality.py --strict .`: **OK** · `make org`: exit 0, graph valid
- Live reproductions: backend `--dir . --json` and frontend `--dir examples --json` now `json.loads`-clean (were `JSONDecodeError`); `--skip scratch` honored (0 phantom "missing target" entries, was 3); `--skip ""` no-op; `-o`-only and non-json modes byte-identical; `pi-batch health --json` unchanged shape, `healthy: True`
- `make ci`: all steps green **except** 21 pytest failures — **proven environmental**: a concurrent user campaign (pid 1547473, age 3.1h) holds the pi-batch instance lock → subprocess-spawning tests get `Refusing to start (exit 5)` and produce no artifacts. A detached worktree at pristine HEAD passes all 217 tests in the two affected files, confirming zero relation to my changes.

### Could NOT run
- Full `make test` in the live tree (lock-blocked, see above; rerun after the campaign finishes). No lint/typecheck applies — the repo's gates for pure-stdlib Python are `cli.py check` + `quality.py` + pytest, all exercised.

### Self-check vs. defect patterns
Fail-closed on every failure path (F1–F9 from design: byte-identical non-json via `file=... else None`, union-not-replace guarded by AC3's negative control, `DEFAULT_EXCLUDES` never mutated, walk-up fixture traps avoided with `.go`-only manifests, `-1` sentinel alarms on future stdout regressions). No shared mutable state added; `parse_argv` complexity was split (data-driven table) rather than exempted.
