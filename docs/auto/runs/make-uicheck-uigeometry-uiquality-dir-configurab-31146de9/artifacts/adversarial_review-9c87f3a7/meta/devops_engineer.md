Review complete — written to `docs/auto/runs/make-uicheck-uigeometry-uiquality-dir-configurab-31146de9/artifacts/adversarial_review-9c87f3a7/meta/devops_engineer.md`.

## Summary

**Key premise verified**: `ops/deploy/` does **not exist** in this repo (`git ls-files ops/` empty; zero Dockerfile/systemd/k8s/terraform assets anywhere). This is the standalone `ai-batch-runner` pip CLI, not the Snaplink server tree — `examples/snaplink-platform/suite-template/compose/*.yaml` are examples for a *different product* and were excluded as evidence.

**What I actually ran**: packaging suite 7/7 passed (sdist content, byte-identity, loader-audit, wrong-key discrimination, wheel), `pi-batch check` 9 gates + 42 evals, `pi-batch health`, `pip check`, full inventory. `make ci` not re-run (sibling roles recorded 21 lock-contention failures from the campaign's own singleton lock).

**Top findings**:
- **F-1 High**: `.github/workflows/ci.yml` ≠ `make ci` — omits `quality` and `org` gates (CI file predates the `org` gate added to the Makefile). CI can go green while AGENTS.md's hard gate regresses.
- **F-2 Medium**: wheel-content test is `importorskip("wheel")` with `wheel` not in dev extras — CI could pass green with the artifact `pip install` actually consumes unverified (the repo's own docstring admits the fix is "campaign direction #2", still pending).
- **F-3 Medium**: no dependency verification/security scanning (no `pip check`/`pip-audit`), unpinned deps, CI tests 3.11 only vs `requires-python >=3.9` (dev env is 3.10.7).
- **F-4 Medium**: no release process — zero git tags, no changelog/publish workflow, no `--version` flag, `dist/` gitignored, `VERSION.json` carries local absolute paths and empty provenance arrays.
- **F-5 Medium**: `.pi-batch/` operational state (sessions index, campaign state) gitignored and unbacked-up; systemd/monitoring guidance is docs-only, no `ops/` to version units.

**Production claim blocked by**: missing ops assets, release automation/provenance, CI parity, dependency policy decision, backup runbook. Canary/blue-green and multi-replica topology correctly N/A for a stateless CLI (singleton lock is the right topology); rollback = reinstall, but only after a documented `.pi-batch/` state-format compat probe since no compat contract exists.
