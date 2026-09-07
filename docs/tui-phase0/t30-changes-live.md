# T30 · Changes Inspector live sampling

`/changes` now remains available while a turn is active for the local mutable
scopes (`all`, `unstaged`, `staged`, and `untracked`). It is explicitly labeled
`LIVE (sampled)`: each refresh is an independent read-only Git observation and
is not an atomic view of a workspace being written by an agent.

The initial view keeps the existing bounded snapshot contract. Subsequent
status and diff loads run in a Textual thread worker, with one in-flight sample,
a generation token, and a mounted-screen check. A late result after closing the
overlay, changing the selected file/scope, or replacing the screen is silently
discarded. No worker result is written to the conversation transcript or used as
an authoritative `workspace.snapshot`.

`last-turn`, `base`, and `commit` remain unavailable until the active turn has
settled: the first requires the authoritative after-turn delta, while the latter
are stable reference comparisons rather than live workspace views. After the
turn ends, the final authoritative workspace event remains the source of truth;
the live panel is only a convenience for observing progress. The detail view
also reports bounded patch statistics (files, additions/deletions, bytes) and
labels Git binary patches as metadata-only instead of presenting them as text.

The implementation is in `pbatch/tui_changes.py` and the admission rule is in
`pbatch/tui_inspector.py`. Deterministic coverage is in
`tests/test_tui_changes.py`; it checks that active views open and that repeated
sampling does not run on the UI thread.
