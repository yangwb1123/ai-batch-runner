QA lead review complete — written to `docs/auto/runs/make-the-backendquality-gate-fail-closed-on-back-02ef859d/artifacts/adversarial_review-9c87f3a7/meta/qa_lead.md`.

## What I ran (all against HEAD `75a7a27`)

- **Live reproductions**: missing dir, empty dir, go.mod-only dir all exit **0** today (AC-1/AC-2 repros confirmed); `--dir .` on this repo = 21 violations/106 files (design's language-restriction claim confirmed); `--dir <regular file>` also exits 0 — a hole the design doesn't cover
- **Baselines**: 65 backend/registry/context/advance tests + 12 health/entrypoint tests + 42 eval cases all green; `pi-batch health` = HEALTHY 100.0 (green only because health runs without `--strict`); `pi-batch check` does not run backendquality; no stray ancestor manifests within 6 levels (walk-up safety claim confirmed)

## Key findings (severity-ordered)

1. **High (F1)** — The design artifact is a 17-line summary that *references* 8 failure modes, 6 migration steps, 10 regression tests, and AC bindings that **don't exist in the artifact** — those claims are untestable as delivered
2. **High (F2)** — `--dir <regular file>` vacuous-passes today; the "missing dir" contract alone leaves a bypass
3. **High (F3)** — Org gates (`agent-engineering/*.yaml` run `--dir {code}`) silently lose backend enforcement on manifest-less repos (Python/SQL/PHP — pyproject.toml is deliberately not in the manifest list); "zero code changes" covers exit codes, not this coverage loss
4. **Medium (F4)** — stdout/JSON channel for skip/scan-error signals is unspecified; advance.py's `json.loads` already breaks on today's exit-0 trailing `OK` line (verified), and any new stdout diagnostic worsens it
5. **Medium (F5)** — `--manifest <unknown>` fail-closed semantics undefined for the 0-files case; `--manifest` last-arg crash risk (parse_argv guard needed)
6. **Medium (F6)** — new report path `docs/backend/` is **not** gitignored (`docs/ui/` is), while `docs/backend/` is simultaneously the intentional deliverable dir of the backend example pipelines
7. **Medium (F7)** — monorepo roots without their own manifest still vacuous-pass via the registry (walk-up goes up, not down)

Each finding carries the exact test name, fixture, and acceptance assertion. The report also includes a prioritized P0–P4 scenario list (happy/boundary/error/race/recovery), a requirement-to-test matrix, and 6 implement-stage exit criteria.
