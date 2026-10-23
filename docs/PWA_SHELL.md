# PWA shell (installable ops Web UI)

Status: **assets, snippets and global-shell serving are wired.** The closed
static byte routes and their headers live in `pbatch/pwa_shell.py`; the
unauthenticated route policy lives in `pbatch/ops_pwa.py`; `pbatch/ops_serve.py`
serves them. The global task page embeds `page_head()` and `page_script()` and
adds an in-memory token form. The workspace page stays authenticated and does
not embed the shell.

Non-goals: no push notifications, no offline document/API caching, no background
sync, no framework, no build step, no CDN, no third-party host. The shell is
static, same-origin and token-free by construction.

## 1. Module API

```python
from pbatch import pwa_shell

sorted(pwa_shell.ASSETS)          # four URL paths (frozen mapping, see below)
pwa_shell.asset(url)              # -> (bytes, headers dict); ValueError otherwise
pwa_shell.page_head()             # -> str, static <head> markup
pwa_shell.page_script()           # -> str, static IIFE for /sw.js registration
```

`ASSETS` is a `MappingProxyType` of `URL path -> (bytes, Content-Type)`, loaded
once at import from `pbatch/pwa_assets/`. A missing, unreadable, symlinked or
empty asset raises at import (fail closed) — it can never degrade into an empty
`200` response. `asset(url)` re-validates the path, re-checks that the snapshot
is non-empty and returns a fresh headers dict per call.

## 2. Routes

| Route                    | File in `pbatch/pwa_assets/` | Content-Type                    | Cache-Control          |
| ------------------------ | ---------------------------- | ------------------------------- | ---------------------- |
| `/manifest.webmanifest`  | `manifest.webmanifest`       | `application/manifest+json`      | `no-store`             |
| `/sw.js`                 | `sw.js`                      | `text/javascript; charset=utf-8` | `no-store`             |
| `/pwa/icon-192.png`      | `icon-192.png`               | `image/png`                      | `public, max-age=86400`|
| `/pwa/icon-512.png`      | `icon-512.png`               | `image/png`                      | `public, max-age=86400`|

The route table is closed (`_ROUTES`). `asset()` raises `ValueError` for unknown
paths, absolute filesystem paths, `.`/`..` segments, query strings, fragments,
protocol-relative (`//host/...`), absolute URLs, backslashes and non-strings.
There is no path arithmetic anywhere: only fixed file names inside
`pbatch/pwa_assets/` are ever opened, and a symlink or a resolved parent outside
that directory is rejected.

## 3. Authentication boundary at the serving layer

`ops_pwa.public_get(path, has_suffix, global_tasks)` is the only source of
unauthenticated GET permission:

* in `--global` mode, the exact document path `/` is public even when it carries
  the legacy `?token=...`; the response is the same static bytes with no token,
  task, tenant or runtime value reflected;
* the four exact asset paths are public only when the raw request target has no
  `?` and no `#` — `/manifest.webmanifest?x=1`, `/pwa/../sw.js`, `/pwa/`,
  `/sw.js/extra` and mixed case are not asset routes;
* the workspace `/` and **all** `/api/*` routes keep the existing token check in
  `ops_serve.py`; `--control` POST routes are unchanged;
* there is no directory or prefix serving and no client-supplied filesystem
  path. Unknown routes fall through to the normal authenticated 404/401.

Static assets are public in both modes because they contain no facts. Request
logging in `OpsHandler.log_message` remains disabled.

## 4. Headers

* `X-Content-Type-Options: nosniff` on every route (always).
* `Cache-Control: no-store` for the manifest and the service worker: they are the
  update trigger, so a stale copy must not survive a reload.
* `Cache-Control: public, max-age=86400` for the two icons (immutable in
  practice; a new icon means a new file name or a cache-name bump).
* `Referrer-Policy: no-referrer` and `Content-Length` on the page and assets.

The global page CSP is:

```
default-src 'none'; script-src 'self' 'unsafe-inline';
style-src 'unsafe-inline'; connect-src 'self'; img-src 'self';
manifest-src 'self'; worker-src 'self'
```

`manifest-src` is required because `default-src 'none'` would block the manifest
link; `worker-src` is required because `script-src` does not authorize the
`/sw.js` URL; `img-src` covers the apple-touch-icon. No other origin is allowed.
The workspace page keeps its prior CSP and does not register a worker.

## 5. Page connection contract

* The token is read once from the legacy `?token=` query (or typed into a
  `type="password"` input) and held only in a JavaScript closure. It is never
  written to `localStorage`, `sessionStorage`, cookies, IndexedDB, caches or a
  URL by the new UI; requests use `Authorization: Bearer ...` with
  `cache: 'no-store'` against same-origin `/api/tasks`.
* After reading the legacy token the page attempts `history.replaceState` to
  strip it from the visible URL. This is best effort; browsers without it keep
  the original URL, and no new UI path ever appends a token.
* Reopening `/` without a token shows the password form. One bounded
  unauthenticated probe may run so a real server can deny it; a 401/403 clears
  all rendered rows, aborts in-flight requests, increments the request epoch so
  late responses cannot repopulate the view, stops the 5 s poll, and waits for
  an explicit reconnect. A successful read records a last-refresh timestamp.
* The poll interval remains 5 s and is non-overlapping (a busy tick is skipped).
  Each request has a bounded 15 s abort timeout. Network errors show a
  disconnected/stale status plus the last successful refresh; they never render
  zero, empty tables or a success summary. Foreground/visibility change triggers
  a refresh only while authenticated, and the existing manual refresh button
  remains.
* Optional `task.scheduling.queue_wait_seconds` / `execution_wall_seconds` are
  displayed when present and `未追踪` when absent or null. Runtime timing is not
  model thinking, tool duration, goal acceptance or liveness.

## 6. Cache ownership

* Exactly one cache name: **`pi-batch-pwa-v1`**, with the prefix
  **`pi-batch-pwa-`**. No other cache is read, written or deleted.
* `install` precaches only the manifest and the two icons, requested with
  `{cache: 'no-store', credentials: 'omit'}`. Documents and API responses are
  never precached.
* `activate` deletes only cache names that start with `pi-batch-pwa-` and differ
  from the current name. Foreign caches are left alone.
* No `skipWaiting`, no `clientsClaim`: an updated worker waits until the page is
  reloaded/open clients close, so a running session is never silently swapped
  under the user. Shipping a new shell means bumping the cache name (and the
  allowlist if the URL set changes); the previous `pi-batch-pwa-*` cache is
  pruned on the next activate.
* `fetch` is cache-first **only** for the three allowlisted static paths, with a
  network fallback whose successful response is written back into the cache. For
  everything else — any `/api` path, documents, navigations (`request.mode ===
  'navigate'`), cross-origin, non-GET, query/fragment, or any request carrying an
  `Authorization` header — the handler returns before `respondWith`, so the
  network behaviour is byte-for-byte what it is today. Nothing is ever cached
  for those requests.

## 7. Secure context

`page_script()` registers `/sw.js` with `scope: '/'` only when
`'serviceWorker' in navigator` **and** `window.isSecureContext`. Service workers
exist only in a secure context: HTTPS, or `http://localhost` /
`http://127.0.0.1`. A phone opening `http://<host>:8731/?token=...` over plain
HTTP is **not** a secure context, so no worker is registered there — remote
`ops serve` already requires `--tls-cert/--tls-key`, which is also the PWA
prerequisite.

Unsupported context or a rejected registration never throws and never logs: the
only signal is `document.documentElement.dataset.pwa = "unavailable"`. Errors,
URLs and anything error-shaped stay out of the console deliberately (a URL can
carry a query token).

## 8. Token and secret exclusions

* No asset or snippet reads or embeds a token, cookie, `localStorage`,
  `sessionStorage` or `Authorization` value. The worker's
  `request.headers.has('Authorization')` is a **presence-only** bypass — it never
  reads the value.
* The four routes expose only public, static bytes: an app manifest with name and
  icon paths, a worker script with a three-entry allowlist, and two PNGs. Nothing
  there identifies a session, workspace, task or user.
* The global document is static markup plus generic script; it never interpolates
  a token, task row, source name or diagnostic. The token form field is the only
  credential-shaped input and it is cleared immediately after use.
* Do not add token interpolation, per-request query strings or storage reads to
  `page_head()`, `page_script()`, the manifest or the worker. If a future change
  demands authorization on these paths, that is a design change (signed
  capability on a scoped worker URL), not a config toggle — the current
  `scope: '/'` registration assumes credential-free delivery.

## 9. Packaging declarations

`pyproject.toml` → `[tool.setuptools.package-data].pbatch` lists the four files
explicitly:

```toml
pbatch = ["README.md", "role_keywords.yaml", "task_keywords.yaml",
          "pwa_assets/manifest.webmanifest", "pwa_assets/sw.js",
          "pwa_assets/icon-192.png", "pwa_assets/icon-512.png"]
```

Explicit names, not globs, on purpose: `tests/test_packaging.py`
(`test_package_data_declaration_covers_loader_refs`) enforces that this block is
an explicit list — a glob silently ships files nobody reviewed or silently misses
the one a loader needs. Adding a PWA asset means adding its filename here **and**
extending the closed-set expectations in `tests/test_pwa_shell.py` and
`pbatch/ops_pwa.py` (through the frozen `ASSETS` mapping).

`MANIFEST.in` mirrors the same surface for sdist builds:

```
recursive-include pbatch/pwa_assets *.webmanifest *.js *.png
```

`[tool.setuptools] include-package-data = true` carries those sdist members into
the wheel. The icons were generated once during implementation with stdlib
`zlib`/`struct` (deterministic, no metadata chunks) and are committed as bytes:
nothing is generated at runtime and there is no build step. `pi-batch health`
audits tool-root `data-files`, not `package-data`; the packaging regression for
this surface is `tests/test_packaging.py`.

## 10. What these checks do not prove

`python -m pytest tests/test_pwa_shell.py tests/test_ops_pwa.py
tests/test_task_center_connection.py -q` is **static/HTTP/VM**: it parses the
manifest JSON, reads PNG signature/IHDR bytes, compares header strings, greps the
snippets and the worker source text, exercises the Python HTTP handler, and runs
the first inline script in a bounded Node VM with a fake DOM. It does not prove
any of the following, and static green must not be reported as such:

* that a **real browser** installs the app (installability criteria, "Add to Home
  Screen", `display: standalone`, icon rendering);
* that the service worker registers, installs, activates, precaches the three
  entries, prunes old `pi-batch-pwa-*` caches, or that cache-first + network fill
  behaves as written;
* that bypassed requests (documents, `/api/*`, non-GET, cross-origin,
  Authorization-bearing) really reach the network unchanged;
* that the served page's CSP (`manifest-src`/`worker-src`/`img-src`) and MIME
  handling allow the shell in Chrome, Safari, Firefox or an installed app;
* that TLS/secure-context behaviour holds on a real phone, or that the app
  survives an update (cache-name bump) without stale assets;
* that the token form, denial stop, timeout or reconnect behave correctly under a
  real browser's fetch/cookie/cache implementation.

Acceptance therefore still needs, on the target browser/device, at minimum:
serve over HTTPS (or localhost), load the page with DevTools open, confirm an
activated worker at scope `/`, confirm cache `pi-batch-pwa-v1` holds exactly the
three allowlisted entries, confirm `/api/*` responses are absent from the cache,
`curl -I` each route to check the header table, and exercise install plus token
entry on a real phone. Record browser, version, device and who ran it — that
evidence, not this test file, is the PWA acceptance. See
[`WEB_MOBILE_DOGFOOD.md`](WEB_MOBILE_DOGFOOD.md) for the concrete checklist.
