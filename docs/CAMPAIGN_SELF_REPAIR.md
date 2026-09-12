# Campaign self-repair

Repository Campaigns can opt into a bounded self-repair loop. It is designed
for execution failures such as a timeout, a provider interruption, or an agent
editing source during a requirements/design/review stage.

```yaml
selection:
  max_directions: 1
  max_total_directions: 1       # optional global cap across modules

recovery:
  enabled: true
  max_attempts: 1               # automatic retries per direction
  restore_on_failure: true
  narrow_scope: true
  max_roles_per_iteration: 2
  allow_failed_roles: 1         # final Gate still decides PASS/FAIL
  stop_after_stalled_rounds: 2  # 0 disables the stall stop
```

## Contract

1. The runner records the baseline HEAD, branch and clean-tree fact before a
   direction starts.
2. A failed direction writes the existing bounded wreckage report, then saves
   all uncommitted and untracked changes in a Git stash. If the agent created a
   commit, the commit is retained under `refs/ai-batch/recovery/...`.
3. Only an attempt that started from a clean, known branch may be restored.
   Pre-existing dirty trees, branch changes, unknown Git state and divergent
   commits fail closed and stop the campaign.
4. The worktree is reset to the original baseline only after the stash/backup
   is verified. A recovery JSON record contains status, hashes, bounded paths,
   stash/ref evidence and the failure reason; it never copies prompts,
   assistant bodies or command output.
5. A retry receives bounded failure feedback and a smaller profile: fewer meta
   roles, no redundant role retry, and an explicit instruction to keep the
   implementation slice small. This changes orchestration only; it never
   weakens the final engineering or verdict gate.
6. `max_attempts` and `stop_after_stalled_rounds` prevent `rounds: 0` from
   burning provider budget forever. A blocked recovery stops immediately.

The recovery stash and records live under the configured `data_dir` in
`campaign-recovery/`. They are evidence for human triage, not an automatic
merge. Passed isolated branches still require the normal merge controller and
repository gate.

## Read-only phase protection

Campaign materialization marks requirements, design, meta review and verdict
stages as `read_only`. Declared deliverables are allowed, but unexpected Git
source paths fail the stage. This prevents a reviewer timeout from silently
turning review edits into an implementation commit. Non-Git projects retain
legacy compatibility with an explicit warning and cannot provide this guard.
