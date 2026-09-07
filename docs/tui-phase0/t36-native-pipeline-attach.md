# T36 · Native Pipeline TUI attach

The TUI can now explicitly start a workspace Pipeline and observe it without
taking execution authority:

```text
/pipeline pipelines/review.yaml [--reuse]
pi-batch tui --cwd . --pipeline pipelines/review.yaml
```

The programmatic attach remains available for an already-running Pipeline:

```python
attachment = app.attach_native_pipeline(workspace_id)
run_pipeline(pipeline, event_sink=attachment)

# Optional exact native session recovery before new events arrive:
attachment = app.attach_native_pipeline(
    workspace_id, session_id="native-session-id")

# Equivalent startup viewer (provider-free, composer disabled):
# pi-batch tui --cwd /workspace --native-session native-session-id
# Or from an idle TUI:
# /native-recover native-session-id
```

The attachment starts with a separate `TUIEventController`, so a native
Pipeline cannot write into an unrelated provider session journal. When the
workspace is an existing directory and journaling is enabled, the first exact
native session lazily binds that controller to its own scoped journal and
checkpoint; otherwise it remains bounded in memory. An explicit `session_id`
requires a real workspace directory and enabled journaling; it never silently
falls back to an unbound memory projection. Recovered events must also carry
that exact workspace identity; a journal with a mismatched historical workspace
is rejected. The status panel reports bounded recovery metadata and checkpoint-cache state. It forwards accepted snapshots to the UI thread and uses a generation guard so delayed
callbacks from a detached Pipeline cannot overwrite the next session view.
Detaching is presentation-only and never calls cancel, interrupt, or stop.

The bridge accepts only `RuntimeEvent` values with the exact attached
workspace, `extensions.runtime == "pbatch-native"`, and
`extensions.read_only is True`. Scope or projection failures close the bridge;
the existing `NativePipelineObserver` then disables observation while the
Pipeline's execution result remains unchanged.

Native events now expose a trusted lineage: the Pipeline turn is the root,
each stage is a child actor, each task is a child of its stage, and meta roles
are stage children. The existing `/agents`, Timeline, Completion, and
read-only Changes guards consume these facts. Explicit Pipeline start remains
available; approval stages use the separate exact-run-scoped
`NativePipelineControlChannel`; Ctrl+C requests a local cooperative stop at
stage boundaries, while it never sends provider stop/cancel.
