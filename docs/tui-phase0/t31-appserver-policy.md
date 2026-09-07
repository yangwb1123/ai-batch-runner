# T31 · App Server effective policy status

The App Server adapter now retains a detached `policy_state` snapshot with the
three effective values relevant to the TUI status panel:

- approval policy;
- sandbox policy;
- network access policy.

Values are observed only from explicit App Server initialize, thread-start,
turn-start, and corresponding start notifications. The adapter does not derive
them from local CLI options or approval requests. Missing fields remain
`unknown`; scalar values are bounded and structured values become the safe
`present` marker rather than being rendered as arbitrary JSON.

After a turn starts, explicit known values are emitted as a bounded typed
`policy.updated` RuntimeEvent. The status panel consumes the resulting immutable
projection rather than reading adapter state directly. The snapshot remains
advisory display state, not an authorization decision: typed approval remains
the only path for resolving a server request, and the server remains the
authority for policy enforcement. It is also not persisted as a new history
source. Tests cover explicit extraction, structured-value redaction, and
thread-start readback.
