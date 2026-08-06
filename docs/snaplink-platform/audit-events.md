Draft written to `docs/snaplink-platform/audit-event-registry-draft.md` (30KB, 121 event types across 6 services).

**Structure** (per request: `##` per service, `###` per category):

- **§0 Provenance** — separates verified anchors (from `SCENARIO_ANALYSIS.md` / `decisions/03-capabilities.md`: aero-id & aero-vault connectors exist, console audit page exists, snaplink & aero-im connector gaps, `file.delete.admin` naming) from `[PROPOSED]` content
- **§1 Conventions** — event id naming (`<domain>.<verb>[.<scope>]`), common envelope, actor model (`user`/`admin@console`/`service:client_id`/`system`), tenant context legend, retention classes **C1** (≥7y, always hold-eligible) / **C2** (1y) / **C3** (90d) / C0 transient, legal-hold sensitivity High/Medium/Low/None, metadata-only payload policy, ‡ high-volume marker
- **Per-service registries**:
  - `## snaplink` (46): auth (14), policy (14), client/OAuth apps (7), tenant (5), admin (6)
  - `## snaplink-console` (7): sessions, audit/export view events (privacy-sensitive)
  - `## snaplink-audit-governance` (12): ledger/checkpoint/integrity, legal hold, export, relay/DLQ self-audit
  - `## aero-id` (14): account lifecycle, consent, email/phone change, ownership transfer
  - `## aero-vault` (20): file lifecycle, shares/ACL/WebDAV locks, RAG, admin ops
  - `## aero-im` (22): message, room, live/recording, notification, moderation
- **§2 Coverage gate checklist** + **§3 seven open decisions** (IdP ledgering, content-in-payload, RAG/message volume aggregation, compaction-vs-immutability ADR, tenant propagation, console data-source boundary, ADR-0002 alignment)

Per the repo's evidence standard this is a **proposal** — it must be validated against each of the 6 codebases and human-approved before promotion to contract (as required by `MULTI_REPO_OPERATIONS.md`). The two verified gaps (snaplink, aero-im connectors) are flagged in each section's ingestion-status note.
