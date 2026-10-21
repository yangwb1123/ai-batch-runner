# Campaign admission fixes — ForgeOS follow-up

These changes address coordinator defects, not the acceptance status of any
ForgeOS candidate. Direction PASS, finalized commits, and project acceptance
remain separate. No campaign is started, stopped, merged, or given more budget
by applying this patch.

## Implemented

- Budget exhaustion supplies the required `evidence` argument to the real
  event writer. The budget event and derived summary can be written instead
  of crashing with `TypeError`.
- A budget/strategy stop (`run_campaign` exit 3) is terminal for both the CLI
  round loop and the historical `pbatch.campaign.run_campaign_loop` facade,
  including `--rounds 0`. A `BUDGET_BLOCKED` direction observed mid-batch also
  propagates exit 3, rather than being reduced to an ordinary failed round.
  Neither path runs a round commit, sleeps, or retries just because an earlier
  round failed.
- Serial direction admission checks the coordinator's configured cutoff before
  Git inspection or materialization. Isolated batches check before worktree
  preparation and again when a queued direction actually starts. A refused
  queued direction retains its prepared branch/baseline identity and analysis
  evidence, with `BUDGET_BLOCKED`, never PASS.
- The isolated batch resolves one cutoff from the coordinator ledger, not each
  worktree's private runtime. Rechecking a missing-ledger fallback does not
  slide that batch's cutoff. Existing absolute-deadline precedence and the
  first-recorded-start budget semantics are unchanged.
- Validator startup references are inspected before analysis: stage, task,
  and gate-fix declarations use the existing registry and command expansion.
  Missing direct executables or recognized interpreter scripts and malformed
  shell quotes stop the campaign with exit 4, even without `--preflight-strict`.
  This distinguishes a missing checker from an executed baseline quality FAIL.

## Validation boundaries

The reference check only runs a bounded shell `command -v` lookup; it does not
execute file/judge checkers or require future output files. Existing
`requires_any` applicability still applies. Repo-scoped validators declared by
individual tasks use the materialized stage/root cwd for both applicability
and script references, as at execution time. Effective file validators follow
inheritance: `from_outputs` tasks use their own cwd and explicit/null/empty
validator override semantics. `from_dir` aggregate tasks and plain per-file
suffixes use the resolved input directory; nested non-aggregate input globs
have unresolved per-file parent cwds and are explicitly unverified (no new
input walker). Repo references are still checked at root. Prompt/meta tasks
and gate-fix use the stage cwd.
Task `validate` wins over its legacy `validate_cmd` alias, as in actual task
construction. An unused stage file validator is not checked at an invented cwd.
Filesystem cwd resolution is separate from the execution-time `{cwd}` spelling:
a relative task cwd stays relative during expansion, including its existing
nested-path behavior; readiness does not silently repair runtime argv.

Compound/dynamic shell commands, including tilde/glob paths, heredocs, and
commands containing `#` comments/literals, are detected before direct-command
quote parsing and explicitly logged as **not statically checked**. An unavailable future task/input cwd is also unverified, not a false
missing-executable claim; readiness does not create directories or authorize
future writes. Output-derived executable names are unverified before lookup;
the future-output sentinel is never treated as a real executable path.
Executable lookup does not prove module imports, interpreter option
semantics, script dependencies, or eventual checker success. Literal Python regular-file
scripts after supported simple flags are checked. Python directory/special-file
startup, `-m`/`-c` modes, and shell/Node/Ruby/Perl input resolution are explicitly
unverified, not rejected as literal missing files. Arbitrary wrappers are not
interpreted. Execution-time
validators and existing repo-baseline gates remain mandatory. A template with
no repo-scoped validators still warns unless its existing preflight policy
requires one; this patch does not manufacture a target-project validator.

Regression coverage includes the real event signature/ledger, terminal round
handling, cutoff equality, serial and queued admission, original-ledger reuse,
missing-ledger anchoring, disabled budgets, missing checkers, task aliases/cwd,
non-applicability, and no premature file-validator execution. Controlled tests
are not provider-side model authentication or an unattended campaign rehearsal.

## Still separate work

This is **admission**, not a hard campaign-wide execution deadline: already
running analysis/directions/stages/roles/retries are not interrupted or capped
by remaining campaign time. Preparation I/O also has no new hard deadline.
Deleting the authority ledger is not a supported budget recovery mechanism.

Default isolation independent of concurrency, unique campaign/run branch
namespaces, effective model/environment normalization, immutable shared
analysis handoff, explicit design write scopes, change budgets, and matched-task
model measurements remain to be implemented and validated. No existing gate or
threshold was weakened to improve the pass rate.
