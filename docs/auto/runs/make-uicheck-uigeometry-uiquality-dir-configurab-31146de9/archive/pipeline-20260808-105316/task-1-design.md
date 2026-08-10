All evidence verified and the design is complete. Summary:

## Verification results (all claims checked against live tree)

| Claim | Result |
|---|---|
| `pi-batch.yaml:163/165/171` hardcode `--dir {cwd}/src` | ✅ exact |
| `backendquality` already `--dir {cwd}` (stale-claim flag) | ✅ confirmed, correctly out of scope |
| `check-ui-spec.py:24/26` "No frontend files found -> exit 0" | ✅ exact |
| `report.json` / `geometry.json` / `quality.json` zero-signal, no `src/` in repo | ✅ exact (geometry uses `files`, others `files_scanned` — all zero) |
| Only expansion sites `runner.py:487/:698` | ⚠️ **correct for these 3 validators** (scope=file), but globally there are 2 more validator-command sites: `repo_gates.py:31-32`, `cli_batch.py:408` |
| `config.py:26-55,419-430`, test anchors `:57-61/:102-112/:138-149` (12 tests), requirements doc | ✅ exact |

**New findings beyond citations** (material to the design):
- **F1**: `{ui_scan_dir}` must be expanded via a shared helper at all **4** validator expansion sites, else a future `scope: repo` gate leaks the literal placeholder to the shell (vacuous-pass resurrection). `pipeline.py:633` is a stage-command surface (out of scope).
- **F2**: `cli_batch.py:408` today expands only `{cwd}`; `repo_gates.py` lacks `{tool_root}` — helper must preserve these semantics (`output=None` skip), incidentally fixing the tool_root gap at zero behavior cost.
- **F3**: warning placement verified — `--json` summary already goes to stderr; non-json OK summary is stdout (anchor-dependent); warning goes to stderr in both.

## Design deliverable

**`docs/auto/designs/ui-specs-672fd14a-design.md`** (follows the `designs/<module>-<id>-design.md` convention):

1. **API changes**: optional top-level `ui_scan_dir:` key; `{ui_scan_dir}` placeholder replacing `--dir {cwd}/src` in the 3 validator commands; new `pbatch.config.ui_scan_dir()` + `expand_cmd()` (single expansion entry point, 4 call sites); stderr zero-file warning in `check-ui-spec.py main()`.
2. **Compatibility**: unset → byte-identical `--dir <cwd>/src` (zero migration); all 3 regression anchors preserved; `--json` stdout purity untouched.
3. **Failure modes** (8, with mitigations): typo'd dir (now visible via R2 warning), empty/whitespace value → fall back to default, shell metachars → `shlex.quote`, placeholder leakage → defensive check, etc.
4. **Migration**: 9-step sequence (config → 4 call sites → yaml → warning → 6 tests → docs → `pytest`/`make ci`/`quality.py --strict`).
5. **Acceptance mapping**: AC1–AC4 → R1–R4 → T1.1–T4.2 → 6 concrete named tests with exact assertions (including the T2.1 required `warning` + `0 file(s) scanned` substrings).

Note: the "12 existing tests" and 6-new-test count from the evidence check out against the actual file.
