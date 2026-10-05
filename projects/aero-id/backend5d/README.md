# aero-id backend-only campaign (5 days)

Backend-only counterpart of the web campaign. Same engine, disjoint scope.

## Shape

| Property | Value |
|---|---|
| Target repository | `/home/u1/aero-id-backend-5d` (isolated worktree, branch `aero-id-backend-5d-20260930`) |
| Main repository | never modified; the campaign merges only into its own branch |
| Model | `--provider opencode --model space-bunny-free --thinking max` |
| Window | 5 days (`timeout 432000` around `run-5d.sh`) |
| Discovery | 40 backend roots (`internal/*`, `cmd`, `configs`, `scripts`, `api/proto`, `migrations`); `web` excluded |
| Direction quota | 2 per module, 4 per round, one round per supervisor iteration |
| Isolation | `--parallel-pipelines 2` gives each direction its own worktree under `.pi-batch/worktrees/` |

## Backend-only enforcement

Four independent refusals, all fail-closed:

1. `campaign.yaml` discovery excludes `web`, so no frontend module is ever analyzed.
2. Every stage prompt forbids inspecting, reading, modifying or testing `web/` or
   any frontend asset, manifest or build file, and forbids frontend tooling.
3. `done-gate.sh` (the `repo-gate` validator) refuses a direction whose committed
   diff, uncommitted work or untracked files contain a `web/` path, or that
   changes `package.json` / `pnpm-lock.yaml` / `vite.config` / `tsconfig`.
4. The same `done-gate.sh` is the `--merge-gate`, so a direction that reached
   `web/` could not merge even if an earlier check missed it.

`repo-gate` is declared with `scope: repo`, so the campaign preflight runs the
full done-gate at baseline before any model call.

## The gate is the repository's own

`done-gate.sh` only calls the target repository's Makefile targets — the fifteen
documented ones from `AGENTS.md`:

```
test lint lint-strict check-file-sizes check-audit-swallow check-anchored-event-ids
check-audit-manifest check-load-scenarios check-migration-set
check-db-startup-hardening check-composition-root check-proto-sync
check-architecture check-integration-compile build
```

No copy of these rules lives in the runner.

## No meta stage

`campaign-pipeline.yaml` is `requirements -> design -> design_gate -> implement
-> acceptance`. There is deliberately **no** meta/adversarial-review stage: on
2026-08-12 such a role deleted live source directories in this repository.
Independent verification is the `design_gate` stage.

## Pipeline artifacts stay untracked

Every stage output lives under `.pi-batch/aero-id-backend5d/auto/`. The merge
controller refuses to merge a direction whose worktree is dirty, so a stage
artifact written into the repository would block the merge (this happened during
the interrupted 24h run: `existing campaign worktree is dirty: docs/designs/...`).
Design documents still go to `docs/designs/` and are committed by their stage.

## Operating it

```bash
bash run-5d.sh                                   # start/attach a 5-day window
tail -f /home/u1/aero-id-backend-5d/.pi-batch/aero-id-backend5d/supervisor.log
tail -f /home/u1/aero-id-backend-5d/.pi-batch/aero-id-backend5d/campaign.log
git -C /home/u1/aero-id-backend-5d log --oneline main..HEAD   # landed directions
```

The supervisor survives the death of the session that launched it (`setsid`, HUP
ignored, TERM/INT logged rather than fatal) and relaunches the campaign with
durable state. A round that makes no progress — for example a provider outage —
backs off 5, 10, 20 … 45 minutes instead of consuming the window. `--reuse
--skip-passed --retry-failed` means completed directions are never repeated.

Stop it by killing the recorded supervisor PID; state stays on disk and a later
`run-5d.sh` resumes.

## Provenance

- `59a0376` carries the strict-lint baseline repair (six G115/errorlint/unused
  findings in `internal/connector/auditgovernance/*_test.go`) that
  `make lint-strict` needed on `main`; without it `repo-gate` can never be green.
- The 24h `openai-codex/gpt-6-luna` run of this campaign was interrupted on
  2026-09-28: the provider started returning `fetch failed` for every call, 64
  pipelines failed and no direction landed. The five-day window moved to
  `opencode/space-bunny-free` for provider diversity.
