# T35 · Review finding navigation

Structured review findings now have a separate bounded picker at `/findings`.
The picker consumes either the completed review runner's structured `findings`
payload or canonical `review.finding` RuntimeEvents; it never scans Assistant
text, Markdown, stderr, or report paths for locations.

Before a finding can become a Changes request, its file is normalized as a
workspace-relative path: absolute paths, drive paths, NUL/control characters,
parent traversal, and oversized values are rejected. Line values are explicit
non-negative integers (or absent). Enter opens the existing read-only Changes
Inspector with the validated path and line; the closest unified-diff hunk is
selected from the typed hunk header when available. A missing file/diff remains
an honest unavailable Changes view.

The jump is advisory navigation only. It does not edit files, execute a fix,
turn an AI finding into a validator/gate verdict, or grant additional access.
The picker also exposes `f` when opened by the TUI: after the user explicitly
selects it, the finding is converted into a bounded ordinary agent-turn prompt.
The prompt labels review fields as untrusted data, requires inspecting current
code, and asks for a minimal no-commit fix plus checks actually run. It uses the
normal turn/runtime permissions and validation path; it is not an automatic
fixer and never marks the finding resolved by itself. When that ordinary turn
ends, TUI writes a bounded `docs/reviews/review-fix-<id>.md` report. It includes
source-review/fix-turn association only when the terminal RuntimeEvent identity
matches exactly; validator, gate, and authoritative workspace facts are otherwise
reported unavailable. A new `/review` is required to reassess the finding.
`/findings` is refreshable while a trusted runtime projection is active, and
completed local review results take precedence over older runtime findings so
stale evidence cannot leak across a new review.
