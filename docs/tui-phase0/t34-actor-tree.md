# T34 · Typed Agents actor tree

`Projection` now retains a bounded actor lineage for the current turn. The
root actor is established by the trusted turn envelope; `actor.spawned`,
`actor.status`, and `actor.completed` must carry an envelope-matching actor id
and a parent that is either the root or a previously spawned actor in the same
turn/run. Unknown parents, duplicate spawns, status-before-spawn, and root
parent claims fail closed.

`TUIViewSnapshot.actors` is an immutable `ActorTreeSnapshot`. It contains only
bounded, sanitized actor labels/provider identities, explicit lifecycle state,
completion outcome, and depth. Actor ids are scoped by turn, terminal nodes are
not marked current, and old turns cannot contaminate the active tree. The
projection remains rebuildable from RuntimeEvents and is independently bounded
from the event journal; omitted nodes are reported as truncated.

The `/agents` overlay renders this trusted active tree above the shared ledger.
Native Pipeline observation contributes the root `pipeline` actor plus typed
stage/task and meta-role children. The App Server adapter additionally projects
provider-declared `actorId`/`parentActorId` items only when the `subagents`
capability is declared; it never derives an actor from an item id. Retained
projection actors are also grouped by historical turn as bounded trees.
Explicit provider `actorAuthority` claims are shown as bounded,
non-authoritative metadata; an inherited claim must name the exact typed parent
and, when that parent's authority is known, may contain only its capabilities
within the same scope. Unknown or over-broad inheritance remains `UNKNOWN`.
`h` cycles those trees without treating the ledger as
actor evidence. The shared ledger remains run history only and does not become
a provider actor tree or an execution grant.
The TUI now exposes `/actor-stop <actor-id>` and
`/actor-steer <actor-id> <text>`, but only as capability-gated requests. The
command resolves its target from the immutable current typed tree, binds the
exact current turn and actor identity, and dispatches through the runtime's
`ActorControlRuntime`; stale, missing, or unsupported targets fail closed.
Provider authority metadata remains display-only and never authorizes the
request. A runtime that does not declare `actor_control` receives no provider
call.
