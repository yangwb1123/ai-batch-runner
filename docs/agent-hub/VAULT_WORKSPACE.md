# Agent Hub workspace snapshots in Aero Vault

`pbatch.hub_vault_workspace.VaultWorkspaceArchive` stores a validated
`pbatch.workspace.v1` bundle as a versioned Aero Vault object. It is a server
side adapter: it accepts the bundle value from Hub code and never gives a Vault
token or URL to a provider or compute device.

```python
from pbatch.hub_vault import VaultArchiveSettings, VaultEvidenceArchive
from pbatch.hub_vault_workspace import VaultWorkspaceArchive

vault = VaultEvidenceArchive(VaultArchiveSettings(
    base_url="https://vault.example.internal",
    tenant_id="team-a",
    token_file="/run/secrets/agent-hub-vault-token",
    prefix="agent-hub/evidence",
))
snapshots = VaultWorkspaceArchive(vault)
reference = snapshots.put(snapshot_id, validated_bundle)
bundle = snapshots.read(reference)
```

The workspace codec (`pbatch.workspace_bundle`) owns bundle schema and path
validation. The adapter calls `validate_bundle`, then stores the codec's
canonical UTF-8 JSON bytes and SHA-256 unchanged. It does not run the evidence
redactor over file contents. The adapter independently caps the JSON object at
512 KiB; codec limits also cap file count and decoded file bytes.

Keys are generated below the configured Vault prefix as
`{prefix}/workspaces/{snapshot_id}/{sha256}.json`. The object ID must be one
safe path segment. Caller-provided reference keys must match that exact shape
and configured prefix; backend, default bucket, version ID, ETag, digest, and
size are checked before the object is returned.

The adapter requires bucket versioning. After upload it lists the object's
versions and downloads the exact version ID, then checks `X-Version-Id`, ETag,
size, SHA-256, and canonical bundle bytes. A lost PUT acknowledgement is
reconciled by reading that content-addressed key and verifying its newest
version; it does not blindly repeat the write. Reads also list versions and
validate the supplied reference against the exact version metadata before the
pinned GET.

The current Aero Vault REST PUT response omits `version_id`; the version list
and `?version=...` GET are therefore required. Existing presigned URLs cannot
pin a version or scope access to a Hub task/grant, so this adapter never
returns one to a Runner. Hub must keep the immutable reference in its own task
record and deliver snapshot bytes only through a separately authorized,
bounded Fabric task transfer. Result uploads use a distinct task output
authorization. The Vault credential remains server-side and should be
tenant-pinned with only `read` and `write` scopes, not `admin` or wildcard
tenant access.

This adapter does not extract bundles, select workspace files, or mint Runner
capabilities. Hub and the Fabric bridge remain responsible for session/project
authorization, task fencing, safe extraction in the configured workspace, and
rejecting output from stale or cancelled tasks.
