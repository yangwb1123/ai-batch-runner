# Spike: pi session file format (T6) — Verdict: PASS

Live probe (2026-08-02): one real run with `--session-mode shared --session-name spike-test`.

## 1. Session file location

```
~/.pi/agent/sessions/--<workdir with '/' -> '-'>--/<timestamp>_<session-name>.jsonl
```

Our `--session-id`/`--session-name` value is used **verbatim as the file name
segment** (verified: `.../--home-u1-ai-batch-runner--/2026-08-02T09-37-22-946Z_spike-test.jsonl`).
Multiple runs of the same session name append new timestamped files; the
latest one is the active session.

## 2. JSONL line types (6 lines in the probe session)

| type | fields observed |
|---|---|
| `session` | version=3, id (= our session id), timestamp, cwd |
| `session_info` | id, parentId, name |
| `model_change` | provider ("opencode-go"), modelId ("deepseek-v4-flash") |
| `message` (user) | role, content, timestamp |
| `message` (assistant) | role, content, **api, provider, model, usage, stopReason, responseId, rawStopReason** |

## 3. Usage / cost JSON pointers (assistant messages)

```
message.usage = {input, output, cacheRead, cacheWrite, reasoning, totalTokens}
message.usage.cost = {input, output, cacheRead, cacheWrite, total}
```

Verified live: `{"input":4972, "output":15, "cacheRead":640, "cacheWrite":0,
"reasoning":13, "totalTokens":5627, "cost":{"total":0.000702072...}}`.
Frequency check on a 194-line production session: **52/194 lines carry usage**
(every assistant completion) — per-call usage diffing is feasible.

## 4. Degradation path (used by T7)

Session file missing / unparseable / stale -> `usage: null` event, never a
crash; budget falls back to invocation counts (T8 1.0 semantics).

## 5. Verdicts per dependency

| Dependency | Verdict |
|---|---|
| R6 metering (T7): file glob + 3 verified JSON pointers + usage-diff sample | **PASS** |
| R7 budget (T8): invocation-count 1.0 needs no session parsing | **PASS** (standalone) |
| R9 session rotation (T10): watermark via file size / message count | **PASS** (deferred to M4) |
