Review complete and written to `docs/snaplink-platform/v2/reviews/identity-protocol-review-v2.md`. Summary:

## Scope verified
- **Deployed IdP** = `/home/u1/snaplink` (module `github.com/opensso/sso`): `cmd/server/routes.go`, `sso.go:894-960`, `token/jwt/generator.go`
- **SDK** = `/home/u1/workspace/demo/snaplink` (module `yangwb1123/snaplink`): `infrastructure/defaultimpl/issue_payload.go` (`buildAccessPayload`)
- **Sink** = `/home/u1/workspace/demo/snaplink-audit-governance`
- Standards in scope: OAuth 2.0 (cc grant), Bearer, JWT/JWS/JWKS, RFC 7662 introspection, RFC 8628 device, RFC 8414 discovery. SAML/SCIM/WebAuthn/CAEP: no implementation anywhere.

## Key findings (13 total; all v1 defects still open)
| # | Sev | Status |
|---|---|---|
| V2-1 | **Critical** | IdP still mints `{sub,aud,iat,exp,uid}` with no `kid` header — sink's JWKS verifier requires `kid` (`verifier.go:167`); e2e trust path broken (B4-1 pending) |
| V2-2 | **Critical** | `AUDIT_ALLOW_DEV_AUTH` default still `true` (`main.go:45`); unsigned `dev:` tokens grant full read/legal-hold (B1-1 pending) |
| V2-3 | High | Discovery still wrong: `token_endpoint=/authenticate`, `/revoke` 404, `code` flow advertised without `/authorize`, issuer Host-derived with port bug — **codified by two passing tests** (`TestOIDCDiscoveryEndpoint` asserts `8080:0`) |
| V2-4 | High | Introspect = unauthenticated oracle (no credential check) |
| V2-5 | **High (new)** | **Batch plan conflates two codebases**: `buildAccessPayload` lives only in the SDK tree, `sso.go:894-960`/`routes.go` only in the deployed IdP — B4-1 implementers will edit the wrong repo |
| V2-6..10 | Medium | Query-string creds, no Content-Type, scope ignored, no no-store/empty refresh_token, device flow unauth'd + relative URI, readyz probes archive only, migration-001 unwired |
| V2-11..13 | Info | SDK already fixes introspection auth/revocation (migration option); vault `RequiredScope` strict check confirmed (B3-5 "no code change" claim accurate); OpenAPI still omits identity surface |

## Checks that ran
`go test ./cmd/server/ -run TestOIDCDiscovery` (passes — codifies wrong contract), `go test ./internal/auth/` (sink, passes), plus git-diff inspection of the IdP working tree.

**Bottom line**: v2 contract is a faithful pin of v1's findings and the sink/producer side is verified correct — but zero B4 items are implemented; G1 (T-1.1/T-1.2/T-8a) and G5 (T-2/T-8/T-9) cannot pass in the current tree. No OIDF certification claimed or evidenced.
