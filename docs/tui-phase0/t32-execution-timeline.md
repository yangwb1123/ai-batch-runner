# T32 · Typed execution timeline groundwork

The trusted timeline now keeps validator, gate, actor, and review facts as
separate bounded entries instead of flattening them into generic status text.
`validation.*` start/complete pairs are coalesced by `item_id`; gate verdicts
are terminal entries; actor entries are coalesced by actor identity and expose
child actor lineage in their bounded detail. Success/failure markers are derived
from typed fields (`success`, `status`, `verdict`, or `exit_code`), never from
assistant prose.

The controller accepts a child actor event only when its `parent_actor_id`
exactly matches the root turn actor. The root `TUIViewSnapshot.actor_id` remains
stable, while the actor entry records the child. An actor with a missing or
incorrect parent is rejected by the existing fail-closed scope binding. This
preserves session/run/turn binding while making a later Actor Tree possible.

Timeline summaries now include bounded validation, gate, and actor counts. The
same projection is used for live rendering and journal recovery; no new
history store or command authority is introduced. Native pipeline stage/task
production is now provided by the opt-in `event_sink` observer documented in
[`t33-native-pipeline-observer.md`](t33-native-pipeline-observer.md); start,
cancel, and approval controls remain out of scope.
