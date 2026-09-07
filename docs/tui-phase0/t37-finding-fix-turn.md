# T37 · Finding fix as an ordinary turn

The `/findings` picker now offers `f` for an explicitly selected finding. The
key does not run a special fixer or mutate the workspace directly. It dismisses
the picker and starts the normal TUI turn path with a bounded prompt containing
the normalized severity, workspace-relative file, optional line, problem,
evidence, and fix hint.

The prompt tells the agent that the review block is untrusted data, requires
checking the current code first, asks for the smallest safe change, forbids an
automatic commit, and requires reporting only checks actually run. The normal
backend/runtime, workspace, capability, cancellation, and validation gates
therefore remain the authority. If a turn is already active, a native Pipeline
is running, or the backend is unavailable, the fix request is rejected rather
than queued or executed through another path.

A finding is never marked resolved by this action. When the ordinary turn ends,
TUI records a bounded `review-fix-<id>.md` evidence report. For runtimes with an
exact terminal `RuntimeEvent` scope, the report associates the source review and
finding with the fix turn and includes typed validation, gate, and authoritative
workspace facts. Missing or mismatched runtime identity is reported as unavailable;
agent prose and exit text cannot create a pass. A follow-up `/review` is still
required to reassess the finding. The original `Enter` path still only opens the
read-only Changes Inspector.
