# ABR Web

**Svelte 5 + Bun + native Iris UI**, using the reviewed local `~/iris-ui` checkout.
A task workbench for the existing Python Agent Hub, not a second auth/execution backend.
Read-only configuration remains the default. Explicit control mode plus exact user grants enables
session creation, instructions, event reading, cancellation and idle-session closure.
The task workspace adds live read synchronization, readable per-turn conversations,
paginated history, task deep links, bounded in-memory drafts and explicit recovery of uncertain writes.
The task catalog searches all authorized sessions on the server and pages in bounded windows.
Latest execution outcomes are distinct from session availability, so failed idle tasks remain actionable.
Paging preserves the open task and draft; invalid cursors and missing task links can recover in the page.
Result inspection separates command exits, reported paths, typed validation/gate events and artifact
metadata. Users can download the currently loaded turn record with explicit completeness flags.
The same authorized selection can also produce a readable UTF-8 text report, preserving reported
event identities, unknown outcomes and coverage limits. Exports do not fetch more records or files.
Historical request receipts identify their original targets separately from the current turn state.
The conversation view can find literal text within the currently displayed instruction and replies,
highlight up to 200 matches and navigate without losing the reading position to live output.
Conversation, recap and result views share terminal reply-completion warnings; none implies goal acceptance.
The currently opened turn's visible instruction can be reviewed, edited and explicitly appended to
the latest in-memory draft. Truncated records are rejected; visible redaction remains intact.
Reuse preserves the existing draft and pending-write boundaries and never submits or replays a task.
Gateways may explicitly opt in to publishing bounded, redacted terminal command output. The results
view can expand, copy and export those snapshots; missing, empty and truncated output remain distinct.
Output sharing is off by default, never enabled by the browser, and does not read artifact paths.
File inspection is a separate opt-in: exact Gateway `--share-file` paths, `web.file_snapshots`, and
the independent `agent.artifacts:read` grant. Immutable snapshots support plain-text preview and
original-byte downloads after SHA-256 verification. Every download reauthenticates; context changes
abort requests and clear previews. These workspace snapshots do not imply accepted deliverables.
Escaped block-format previews provide exact code/reply copying and an original-text view; no raw HTML
or external images are rendered. Execution completion does not imply independent goal acceptance.
See [the task workflow and authorization contract](../docs/agent-hub/WEB_TASKS.md).

```sh
bun install --frozen-lockfile --ignore-scripts
bun run check
bun test tests
bun run build
bun run build:check  # compare a fresh build; never repairs shipping assets
```

`IRIS_UI_DIR` overrides the default Iris directory. Source must match `iris-source.json`.
No Iris files are changed. Build outputs ship in `../pbatch/hub_web_assets/`; Python runtime
requires neither Bun nor the Iris checkout. `bun run dev` is loopback development only,
not a configured login/production service.

From the repository root, `make web-ci` requires the full repository gates, pinned
frontend rebuild and real local Chrome smoke (synthetic issuer). `make ci` also
rejects stale frontend inputs without requiring Bun. Neither command deploys.

[Scope and validation](../docs/agent-hub/WEB_FRONTEND.md) ·
[Release acceptance and remaining human decisions](../docs/agent-hub/WEB_RELEASE.md).
