# Workspace increment verification

Date: 2026-09-13 UTC (2026-09-12 local). This increment adds bounded Vault-backed
workspace input/output to the existing Agent Hub compute flow.

## Runner/Hub evidence

Final integrated source checks:

- `make ci`: **passed**. Regression suite: **3,078 passed in 536.08 seconds**;
  harness self-tests: **56 passed**; rule evaluations: **53 passed**. Release
  metadata, all 8 registered gates, quality, 17 organization nodes, 72 capabilities
  and the dependency graph passed. Log: `/tmp/hub-workspace-runner-ci.log`.
- Integrated Hub/Fabric/Runner, GPU, wire-budget and task-store checks:
  **56 passed in 60.20 seconds**. Log: `/tmp/hub-workspace-runner-e2e.log`.
  The four real workspace scenarios cover fixed-version input/output round trips
  through HTTP and a Runner subprocess, byte-preserving CLI restore, protection of
  the original directory, upload limits, cancellation/resource release, and missing
  output failure. They do not require a pre-seeded input directory on the Runner.
- Fabric/codec/filesystem adapter checks: **97 passed** in the bridge worktree.
  Regressions cover capability refusal, input/output digest binding, content
  idempotency, single-workspace polling and cancellation during failed staging.
- The task-state compaction capacity test verifies that an oversized compacted log
  cannot replace the previous readable state. The signed HTTP envelope boundary
  test combines 256 KiB of files, 128 long Unicode paths and two 32 KiB NUL output
  streams; the resulting request remains below the 1 MiB RPC limit.

Additional targeted checks during implementation (overlap with the suite above):

- Canonical bundle validation: 44 tests, including shared Python/Dart digest,
  Unicode/path collisions, size boundaries and rejection before Base64 decoding.
- Vault version contract plus hardened filesystem helpers: 71 tests and 3 subtests
  passed in the adapter worktree (before the additional Base64 test).
- Hub workspace/CLI/broker/TUI tests: 18 passed, including tenant/session grants,
  shared-session snapshots, idempotency, pending upload quotas, cross-session
  default broker keys, cancellation during input download, output digest mismatch,
  failure states, and restart-safe independent archive retries.
- Existing Hub HTTP, CLI transport and GPU request tests remained green after the
  request-size and receipt-projection changes.
- `make check`: all 8 gates passed; `python quality.py --strict .` and
  `git diff --check` passed during implementation.
- OpenAPI 1.4.0 parses; local references resolve and operation IDs are unique.

Tests use a loopback Vault REST contract server with real version lists, pinned
downloads, response headers and lost-ACK behavior; they do not call a deployed Aero
Vault account. The implementation source passed CI at `53700a4`; subsequent changes
only complete documentation and remove one trailing space in a test source line.

## Console evidence

Source: `9229fb4e0200a4a2ce681cb70bdcc3bea58de2d2`, based on
`5f2bf03796aab825b294a9196b35db1c928e1db1`.

- `flutter analyze --no-pub`: passed.
- Full VM suite: **1,315 passed**.
- Chrome browser suite: **60 passed**, including real DOM file selection size,
  read/cancel boundaries, workspace DTO/API checks and existing browser regressions.
- Guard pins: **83**, exact expected count; Python unit tests: **24 passed**.
- Kubernetes render, Web release build, release-artifact gate and full repository
  harness: passed. No gate thresholds or dependency lock were changed.
- Canonical fixture matches the Python SHA-256; non-BMP path sorting is covered.
  NFC and full Unicode case folding remain authoritative on the Hub.
- Upload retries and stale session/list/task/download responses are covered;
  320-pixel English/Chinese forms and the full task submission flow are covered.

Web release: **94 files, 44,971,025 bytes**; content manifest SHA-256
`b13028adae3f736099788a940d313eeeaf5d0f125c6aa5cf35f9258cb8b8f511`.
The main Console index equals the verified source tree
`122aedfef22298e024042a245e3e06a02808d930`; main Web files match the manifest.
The prior index and unstaged changes were checked before applying the increment.
Detailed local logs: `/tmp/hub-workspace-console-verification.json` and
`/tmp/hub-workspace-console-*-tests.log`.

## Limits and worktree preservation

The Runner increment starts from `689c5ec210c4bf5658e4132a13478cc85fb2b325`.
Implementation uses isolated worktrees. Earlier staged milestones remain staged;
application checks the previous index against that base, applies only the increment
with `git apply --index`, and compares the resulting index with the final isolated
tree. It also checks the unrelated unstaged `pbatch/campaign_runtime.py` patch
byte-for-byte and keeps the main HEAD unchanged. The application receipt is
`/tmp/hub-workspace-runner-applied.json`; the reviewable increment is
`/tmp/agent-hub-workspace-runner.patch`.

Not executed: a deployed six-service environment, real remote network/device
installation, actual CUDA work, Android/iOS/desktop installers, native host file
selection/saving, large-file chunk transfer, source-directory patch merging, or
multi-Hub failover. Native Console supports JSON view/copy; Web supports file
selection/download. POSIX dir-fd/no-follow primitives are required for Runner
workspace materialization. Failure cleanup may retain directories, and the
workspace path boundary is not operating-system isolation from a hostile process
running as the same OS account.
