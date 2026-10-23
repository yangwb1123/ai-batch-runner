# progress.v1 — pure snapshot contract

## Scope and authority

This is a deterministic, read-only projection for consumers that need explicit
progress facts without guessing ledger layouts. It does not discover sources,
read files, access the network, read a clock, log, persist artifacts, execute work,
or grant authorization. Execution state, goal acceptance, validation, liveness,
and authorization are separate facts. Neither counters nor usage imply success.

Authoritative Python definitions (stdlib-only, Python 3.9+):

- `pbatch.progress_contract_v1`: `SCHEMA_VERSION` (1), `SCHEMA`, `ENVELOPE_FIELDS`, `RECORD_FIELDS`,
  `IDENTITY_FIELDS`, `USAGE_FIELDS`, `STATES`, `FIELD_MAP_KEYS`,
  `REQUIRED_FIELD_MAP_KEYS`, `STATE_MAP_KEYS`, and the `MAX_*` bounds below.
- `validate_progress(payload) -> None`: validate the complete wire object, or
  raise `ProgressContractError` (a `ValueError`) with a sanitized message.
- `pbatch.progress_projector_v1.project_progress(rows, *, now, field_map,
  state_map, limit=100, stale_after_seconds=300) -> dict`: validate inputs,
  project every snapshot, reject conflicts, select latest snapshots, sort, limit,
  and return a validated envelope. Arguments have no implicit ledger defaults.

Both APIs leave their inputs unchanged. Returned objects do not share mutable
containers with inputs. Calls with the same facts and arguments return identical
results. Errors return no partial payload and never include source values, source
keys/paths, identifiers, or raw parser diagnostics. Python objects with arbitrary
behavior are not supported: containers and scalar types must be plain built-ins,
not subclasses. Module execution has no I/O; normal Python module import/loading
and the existing package initializer remain the host application's responsibility.

## Exact wire shape

Every listed key is required, even when its value is null. No other keys are
accepted at any contract level (envelope, record, usage). JSON null is Python
`None`; JSON booleans must be actual Python `bool`. Numeric strings, booleans as
numbers, NaN, infinity, missing evidence for required fields, and violated bounds
are rejected. The payload itself must be a plain `dict`.

### Envelope

| Key | Required value |
| --- | --- |
| `schema` | Exactly the string `progress.v1`; no other version is accepted |
| `generated_at` | Canonical UTC timestamp representing caller-supplied `now` |
| `stale_after_seconds` | Integer in 1..86400, default 300 |
| `records` | Plain JSON array (`list`), length 0..200, of exact records below |
| `truncated` | Boolean; true exactly when unique latest records exceed `limit` |

Legitimate empty rows produce `records: []`, `truncated: false`, and the supplied
generation facts, not invented records, counts, costs, or measurements.

### Record

| Key | Required value / meaning |
| --- | --- |
| `tenant_id`, `workspace_id`, `task_id`, `run_id` | Nonblank strings, each at most 128 UTF-8 bytes; complete tuple is the identity |
| `sequence` | Integer in 0..9007199254740991, scoped to that complete identity |
| `observed_at` | Canonical UTC fact, no later than `generated_at` |
| `state` | Exactly `queued`, `running`, `blocked`, `succeeded`, `failed`, `cancelled`, or `unknown` |
| `completed_units`, `total_units` | Independently nullable integers in 0..9007199254740991 |
| `percent` | Null, or the derived percentage in 0..100, rounded to two decimal places |
| `usage` | Exact object below, never null |
| `eta_seconds` | Always null throughout v1, even for succeeded or zero-total work |
| `stale` | Boolean, exactly `generated_at - observed_at >= stale_after_seconds` |

Identities are not trimmed, case-folded, Unicode-normalized, or synthesized.
Whitespace-only identities are rejected. Tenant/workspace boundaries are part of
both dedupe and sorting; matching task/run strings do not cross those boundaries.

Counter units are the producer's explicitly mapped work units, with the same unit
for completed and total. They do not measure goal acceptance. When both counters
are known, completed greater than total is invalid (including positive completed
with total zero). Percent exists only when both are known and total is positive:
`100 * completed_units / total_units`, rounded **half up** to hundredths using exact
integer arithmetic. For example, 1/3 gives 33.33, 1/32 gives 3.13, and 0/positive
gives 0.0. Unknown counters and known zero totals give null, never a guessed 0%
or 100%. JSON numbers need not display trailing zeroes. The validator recomputes
percent and rejects inconsistent representations. State is never inferred or
rewritten from counters, age, usage, or percent.

### Usage: cumulative snapshot, not deltas

| Key | Required value / units |
| --- | --- |
| `input_tokens`, `output_tokens` | Independently nullable safe nonnegative integers, maximum 9007199254740991 tokens |
| `cost_amount` | Null or finite nonnegative JSON number, maximum 1000000000000, in the reported currency's major units (not cents) |
| `cost_currency` | Null or exactly three uppercase ASCII letters (`[A-Z]{3}`) |

Unknowns remain null; explicit zero survives. A known amount with unknown currency
keeps that currency null; a known currency with unknown amount is also valid.
Currency labels are not independently authenticated ISO membership claims. There
is no implicit USD, currency conversion, addition across currencies, token total,
financial reconciliation, or inference of missing values.

The projector normalizes known cost amounts to Python floats (including -0.0 to
0.0) to make equivalent integer/float inputs deterministic; it does not round or
convert currencies. The accepted bound makes integer-to-float conversion exact.
Decimal strings, `Decimal`, and arbitrary numeric classes are not wire numbers.
These observed JSON amounts are not an exact-decimal billing system. Snapshot
sequence 2 reporting eight tokens replaces sequence 1 reporting five tokens;
it does not report thirteen tokens. Snapshots need not have monotonic counters
or observation times. There is no inference of units across different tasks.

## UTC and freshness

`now` is required; it accepts either a plain `datetime` with a built-in
`datetime.timezone` whose UTC offset is zero, or a UTC timestamp string. Naive
datetimes, nonzero offsets, arbitrary tzinfo implementations, numeric epoch values,
and absent values are rejected. There is no local-time conversion or clock read.

Mapped row `observed_at` must be a string because rows are JSON evidence. Accepted
input grammar is `YYYY-MM-DDTHH:MM:SS[.ffffff](Z|+00:00)`, with 1..6 fractional
digits when present and a valid Gregorian datetime in years 0001..9999. Lowercase
markers, leap seconds, offset timestamps other than `+00:00`, `-00:00` (unknown
offset), and more than six fractional digits are rejected, not rounded or clipped.

The projector serializes both timestamps as `YYYY-MM-DDTHH:MM:SS.ffffffZ`, always
six fractional digits. `validate_progress` requires that canonical representation;
it does not silently normalize received wire data. Any future observation fails,
including one microsecond into the future. Staleness uses exact datetime/timedelta
arithmetic, including equality at the threshold, and **does not change terminal
or other states**. Stale is age of evidence, not proof of a stalled process.

## Explicit mapping interface

### `field_map`: canonical scalar target → source key/path

A plain dict, with this finite key set (at most 13 entries):

- Required mappings: `tenant_id`, `workspace_id`, `task_id`, `run_id`, `sequence`,
  `observed_at` (`REQUIRED_FIELD_MAP_KEYS`). Required evidence must also exist and
  be valid in **every** row; mappings do not supply constant or fallback values.
- Optional mappings: `state`, `completed_units`, `total_units`,
  `usage.input_tokens`, `usage.output_tokens`, `usage.cost_amount`,
  `usage.cost_currency`.
- Derived/output-container targets `percent`, `eta_seconds`, `stale`, and `usage`
  itself are not mappable. Unknown targets and missing required mappings fail,
  including on empty input.

A mapping value is either one nonblank string for a literal top-level source key,
or a `list`/`tuple` of 1..4 nonblank string keys for an object path. Each key is at
most 4096 UTF-8 bytes. There are no dotted-path parsing, array indexes, selectors,
callables, attribute access, wildcards, transforms, or implicit source names.
`"literal.key"` selects exactly that key, while `("facts", "state")` traverses two
objects. Paths may explicitly reuse source keys; the caller owns their meanings.

An absent path or null intermediate object supplies absent evidence. Optional
absent, unmapped, or explicitly null numeric/usage evidence becomes null; absent
or null state evidence becomes `unknown`. A path crossing a non-null non-object
is malformed, not absent. Evidence in unmapped source columns is ignored for
projection but still subject to all raw row bounds and JSON validity checks.

### `state_map`: canonical state → source aliases

A plain dict whose only allowed keys are `STATE_MAP_KEYS` (the seven `STATES`).
Each value is a plain list/tuple of source-state strings. Each alias is nonblank,
at most 128 UTF-8 bytes; at most **64 aliases total** are accepted across at most
seven keys. Aliases must be unique, even within one list or between states.
Empty alias lists and an empty map are valid, not implicit identity mappings.

For example, `{"running": ["ACTIVE"], "succeeded": ["DONE", "ok"]}` explicitly
maps those aliases. A source value of `"running"` is **not** recognized unless
explicitly declared, for example `{"running": ["running"]}`. Any non-null mapped
state value that is not a valid declared alias fails closed. Missing/null state
always becomes `unknown`; aliases cannot remap absence. Explicit `unknown`
aliases are allowed. Aliases are case-sensitive and never stripped or guessed.

## Bounded input admission

Rows must be a plain `list` or `tuple`, with at most 1000 elements, not a generator,
iterator, arbitrary sequence subclass, string, or mapping. Sequence type and
length are checked before row traversal. Each row must be a plain JSON dict:

| Bound | Rule |
| --- | --- |
| Canonical JSON bytes | At most 16 KiB (16384 bytes) per entire row |
| Container depth | At most 4 container levels, counting the root row as level 1; scalars do not add a container level |
| Container width | At most 64 entries in each dict/list, including unmapped metadata |
| Strings and object keys | Strict UTF-8, at most 4096 bytes each; unpaired surrogates fail |
| Scalars | Only null, bool, string, safe integer in -9007199254740991..9007199254740991, or finite float |

Row nested containers are plain dict/list only; tuples are accepted only for the
outer row sequence and mapping paths/alias collections. Metadata booleans and
negative JSON numbers are allowed, but never accepted as numeric metrics. Mapped
metrics impose the stricter nonnegative/type/bound rules above.

Canonical byte accounting is equivalent to UTF-8 encoding
`json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
allow_nan=False)`. Key order does not affect byte length. Budgeted traversal stops
with an exception on exceeded depth, width, scalar size, or accumulated byte size;
it never serializes an unchecked unbounded tree or consumes arbitrary iterators.
All admitted rows, including superseded/duplicate/ultimately omitted rows, receive
full semantic validation before selection. Oversized or malformed input is
rejected rather than partially projected.

`limit` must be a real integer in 1..200. `stale_after_seconds` must be a real integer
in 1..86400. Booleans are not configuration integers. Mappings and configuration
are validated even for empty input. No input values are clipped to fit budgets.

## Dedupe, ordering, limiting

1. Form the full identity `(tenant_id, workspace_id, task_id, run_id)` for each
   validated canonical projected snapshot. Dedupe is not based on display labels.
2. At each `(identity, sequence)`, collapse identical canonical snapshots. Raw
   unmapped metadata, source aliases with the same declared state, alternate valid
   UTC input spellings, and integer/float representations of the same cost amount
   do not create conflicts after normalization. Differences in projected evidence
   do. Reject conflicting duplicates **even if a greater sequence supersedes them**.
3. Retain the greatest sequence per identity, not the latest timestamp or last
   input position. Never add cumulative snapshots together.
4. Sort retained records by `observed_at` descending, then the complete identity
   tuple ascending (case-sensitive Python Unicode code-point order).
5. Apply `limit` only after all validation, conflict detection, dedupe, and sorting.
   `truncated` is exactly `number_of_unique_latest_records > limit`.

Input permutations produce identical output, including the truncation flag. The
wire validator also rejects duplicate identities and records out of this order.
Without original rows and limit, a validator cannot independently prove omitted
record count, greatest sequence, duplicate-conflict provenance, or truth of a
nonempty envelope's `truncated` flag; it validates their wire invariants only.
An empty envelope with `truncated: true` is impossible and rejected.

## Integration examples (synthetic evidence)

```python
from datetime import datetime, timezone
from pbatch.progress_contract_v1 import validate_progress
from pbatch.progress_projector_v1 import project_progress

fields = {
    "tenant_id": "tenant", "workspace_id": "workspace",
    "task_id": "task", "run_id": "run", "sequence": "seq", "observed_at": "at",
    "state": ("facts", "status"), "completed_units": ("facts", "done"),
    "total_units": ("facts", "total"),
    "usage.input_tokens": ("meter", "input_tokens"),
    "usage.cost_amount": ("meter", "amount"),
    "usage.cost_currency": ("meter", "currency"),
}
states = {"queued": ["WAITING"], "running": ["ACTIVE"], "succeeded": ["DONE"]}
rows = [{
    "tenant": "t", "workspace": "w", "task": "task", "run": "run", "seq": 1,
    "at": "2026-08-01T11:59:00+00:00",
    "facts": {"status": "ACTIVE", "done": 1, "total": 3},
    "meter": {"input_tokens": 0, "amount": 0, "currency": None},
}]
payload = project_progress(
    rows, now=datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
    field_map=fields, state_map=states, limit=100, stale_after_seconds=300,
)
validate_progress(payload)  # None; failure raises ProgressContractError.
```

The complete payload from that example is:

```json
{
  "schema": "progress.v1",
  "generated_at": "2026-08-01T12:00:00.000000Z",
  "stale_after_seconds": 300,
  "records": [{
    "tenant_id": "t", "workspace_id": "w", "task_id": "task", "run_id": "run",
    "sequence": 1, "observed_at": "2026-08-01T11:59:00.000000Z",
    "state": "running", "completed_units": 1, "total_units": 3, "percent": 33.33,
    "usage": {"input_tokens": 0, "output_tokens": null,
              "cost_amount": 0.0, "cost_currency": null},
    "eta_seconds": null, "stale": false
  }],
  "truncated": false
}
```

For an adapter with identity/sequence/time facts but no metrics or state evidence:

```python
fields = {key: key for key in (
    "tenant_id", "workspace_id", "task_id", "run_id", "sequence", "observed_at")}
payload = project_progress([], now="2026-08-01T12:00:00Z",
                           field_map=fields, state_map={})
assert payload["records"] == [] and payload["truncated"] is False
```

The orchestrator must supply authorized, bounded sources, actual field/state
meanings, and a trustworthy UTC `now`. This contract does **not** wire existing
ledgers, reuse history cursors, provide pagination/history, implement SSE, serve
ops routes, persist snapshots, authenticate tenants, or authorize access/control.
A valid projection is neither independent source verification nor release or
production approval. No existing endpoints or ledger semantics change here.

## Regression command

```bash
python -m pytest tests/test_progress_contract_v1.py -q
```

The suite checks closed schemas, explicit mappings, unknown versus zero, numeric
and time bounds, derived values, dedupe conflicts, scoped identities, deterministic
ordering, truncation, input budgets, immutability, and runtime side-effect guards.
These are pure unit/contract checks, not live-ledger, browser, network, or deployment
acceptance.
