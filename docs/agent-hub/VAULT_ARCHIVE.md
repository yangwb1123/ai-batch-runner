# Agent Hub Vault evidence archive

`pbatch.hub_vault.VaultEvidenceArchive` stores terminal task evidence as a
versioned Aero Vault object and returns a small immutable reference. The
adapter does not change task state or claim that an archive succeeded: the
caller should persist the returned reference separately, and record failures
as archive-state failures without rewriting the task's compute result.

```python
from pbatch.hub_vault import VaultArchiveSettings, VaultEvidenceArchive

archive = VaultEvidenceArchive(VaultArchiveSettings(
    base_url="https://vault.example.internal",
    tenant_id="team-a",
    token_file="~/.config/pbatch/vault-token",
    prefix="agent-hub/evidence",
))
reference = archive.archive(task_id, evidence)
```

The token file must be owned by the current user, a regular file, and mode
`0600`. It is opened with `O_NOFOLLOW` and re-read for each HTTP request. The
adapter never takes credentials from the environment, includes them in the
returned reference, or follows redirects. Use HTTPS except for a literal
loopback address during local development. The HTTP timeout and evidence and
response byte limits are configurable through `VaultArchiveSettings` and have
hard upper bounds. The Vault identity must be authorized for object reads and
writes and for the bucket-config management check used to confirm versioning.

The adapter uses only the current Aero Vault REST contract:

1. `GET /v1/buckets/default/config` must report `versioning: true`.
2. `GET /v1/files/{key}/versions` checks for an earlier archive at the
   content-addressed key.
3. If present, the newest exact `version_id` is downloaded with
   `GET /v1/files/{key}?version={id}` and verified against `X-Version-Id`,
   ETag, size, SHA-256, and the canonical request bytes. A mismatch is a
   conflict; the adapter will not overwrite it.
4. If absent, the adapter sends one `PUT /v1/files/{key}` with a deterministic
   `Idempotency-Key`, then discovers and verifies the created version through
   the version list and pinned GET. If the PUT acknowledgement is lost, it
   performs that same read-and-verify reconciliation before returning an
   error. It does not immediately repeat the PUT.

The object key is
`{prefix}/{task_id}/{sha256-of-uploaded-json}.json`. The uploaded canonical
JSON contains the task ID and evidence after the shared content redactor has
removed secret-bearing fields and strings. JSON shape, nesting, node count,
request size, response size, and version-row count are bounded. The returned
reference is exactly `{backend, bucket, key, version_id, etag, sha256, size}`;
`sha256` and `size` describe the uploaded redacted JSON bytes.

Aero Vault's object DTO does not include `version_id` on PUT, so this client
obtains the ID from `ListVersions` and confirms it by reading that exact
version. It intentionally fails when bucket versioning is disabled. Its
tenant bearer credential is held by the trusted Hub/Gateway process and is
not passed to the model provider or device task process. Aero Vault's current
presigned URL is object/path scoped and cannot pin `?version=...`; it is not a
substitute for task-bound Vault capabilities. Workspace distribution now uses a
separate raw bundle adapter and authenticated Fabric task transport, keeping Vault
credentials on the Hub; see [WORKSPACE.md](WORKSPACE.md).
