# Campaign self-repair

Repository Campaigns can opt into a bounded, evidence-driven repair prompt for
clean failures. Recovery is deliberately non-destructive: the coordinator never
runs `git stash`, `git reset`, checkout, or automatic cleanup to make a retry
possible.

```yaml
selection:
  max_directions: 1
  max_total_directions: 1       # optional global cap across modules

recovery:
  enabled: true
  max_attempts: 1               # bounded targeted retry per direction
  restore_on_failure: false     # legacy field; automated restoration is disabled
  narrow_scope: true
  max_roles_per_iteration: 2
  allow_failed_roles: 1         # final Gate still decides PASS/FAIL
  stop_after_stalled_rounds: 2  # 0 disables the stall stop
```

## Contract

1. The runner records the baseline HEAD, branch and clean-tree fact before a
direction starts. Campaign execution refuses a pre-existing dirty worktree.
2. A failed direction writes bounded wreckage and recovery evidence. Changes and
commits are left exactly where the agent produced them; nothing is stashed,
reset, discarded, or automatically restored.
3. A recovery report binds the failure to the original baseline, branch, and
bounded Git status. It contains no prompts, assistant bodies, or command output.
A dirty/changed failed worktree blocks automatic retries so they cannot build on
unreviewed source. Inspect it and create a fresh, clean worktree for further
work.
4. A retry is only available when the failed attempt left the tracked source
worktree clean. It receives bounded failure feedback and a smaller profile:
fewer meta roles, no redundant role retry, and an instruction to keep the
implementation slice small. The final engineering and verdict gates are never
weakened.
5. `max_attempts` and `stop_after_stalled_rounds` prevent `rounds: 0` from
burning provider budget forever. Provider quota failures are circuit-broken
before another agent call; a blocked recovery stops immediately.

Recovery reports live under the configured `data_dir` in `campaign-recovery/`.
They are evidence for human triage, not an automatic merge. Passed isolated
branches still require the normal merge controller and repository gate.

## Read-only phase protection

Campaign materialization marks requirements, design, meta review and verdict
stages as `read_only`. Declared deliverables are allowed, but unexpected Git
source paths fail the stage. This prevents a reviewer timeout from silently
turning review edits into an implementation commit. Non-Git projects retain
legacy compatibility with an explicit warning and cannot provide this guard.
