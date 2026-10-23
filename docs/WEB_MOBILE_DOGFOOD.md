# Web/mobile dogfood checklist

Purpose: a repeatable manual pass for the global read-only task shell and the
PWA. Automated tests (`tests/test_ops_pwa.py`,
`tests/test_task_center_connection.py`, `tests/test_task_center_mobile.py`,
`tests/test_pwa_shell.py`) cover HTTP routing, CSP text, token-flow state, the
first inline script in a Node VM and the task-first mobile cards rendered from
the same bounded snapshot. They are **not** real-browser, real-device or
production acceptance. A separate optional actual-Chrome regression is
described in section 6.

## 0. Preconditions (record actual values)

- Host OS/browser + version, device model, and whether the browser is installed
  as an app.
- An existing private ops token file (never commit it, print it or put its value
  in process arguments). Reuse it without truncating or rotating it. If one does
  not exist, create a new random token with exclusive creation and mode `0600`;
  do not overwrite an existing credential to follow this checklist.
- Python environment with the repo installed (`python -m pip install -e ".[dev]"`).
- For a phone on the LAN, a TLS certificate/key for the host name. Plain HTTP is
  not a secure context, so the service worker will not register on the phone.

## 1. Start the global shell

```bash
# localhost only (secure context on the same machine)
python pi-batch.py ops serve --global --token-file ~/.config/pbatch/ops-token \
  --host 127.0.0.1 --port 8731

# LAN/phone: TLS is mandatory for a non-loopback host
python pi-batch.py ops serve --global --token-file ~/.config/pbatch/ops-token \
  --host 0.0.0.0 --port 8731 \
  --tls-cert /path/to/cert.pem --tls-key /path/to/key.pem
```

Confirm the console line reports token `on`, read-only mode, and the expected
data directory. Request logs must stay silent.

## 2. HTTP surface (no browser)

The following bounded check reads the credential into Python memory, not shell
arguments. Set `PBATCH_OPS_BASE` to the trusted HTTPS origin for a remote check;
certificate verification stays enabled. It prints status codes, never bodies or
credentials. A failed request/assertion is a failed check, not an empty success.

```bash
python - <<'PY'
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base = os.environ.get('PBATCH_OPS_BASE', 'http://127.0.0.1:8731').rstrip('/')
with Path('~/.config/pbatch/ops-token').expanduser().open('rb') as handle:
    raw = handle.read(4097)
assert len(raw) <= 4096, 'token file exceeds budget'
token = raw.decode('utf-8').strip()
assert token and token.isascii(), 'token must be nonempty ASCII'

def read(path, authenticated=False):
    headers = {'Authorization': 'Bearer ' + token} if authenticated else {}
    try:
        response = urlopen(Request(base + path, headers=headers), timeout=10)
    except HTTPError as error:
        response = error
    with response:
        body = response.read(2 * 1024 * 1024 + 1)
        assert len(body) <= 2 * 1024 * 1024, 'response exceeds check budget'
        print(path, response.code)
        return response.code, body, response.headers

for path in ('/manifest.webmanifest', '/sw.js', '/pwa/icon-192.png', '/pwa/icon-512.png'):
    code, body, headers = read(path)
    assert code == 200 and headers['X-Content-Type-Options'] == 'nosniff'
    assert headers['Referrer-Policy'] == 'no-referrer'
    assert headers['Cache-Control'] == ('public, max-age=86400' if path.endswith('.png') else 'no-store')
    assert token.encode() not in body
code, body, _ = read('/')
assert code == 200 and token.encode() not in body
assert read('/api/tasks')[0] == 401
assert read('/api/tasks', True)[0] == 200
assert read('/sw.js?x=1')[0] == 401
assert read('/sw.js?x=1', True)[0] == 404
PY
```

The workspace page is a different server mode: without `--global`,
`GET /` and every `/api/*` route must return 401 without the token, while the
four static asset paths stay public and fact-free.

## 3. Browser pass (desktop first)

1. Open `BASE/` without a token. Expect the token form, "未连接" status and no
   task rows. The page must not silently show old data.
2. Submit a wrong token. Expect an authentication-failure status, all rows
   cleared, the form shown again, and **no new request** on the next 5 s tick.
3. Enter the correct token. Expect "已连接", a last-successful-refresh timestamp,
   the section tables, and a new read every 5 s. Click 刷新 and switch the
   project selector; old rows must disappear while the read is pending. Both the
   desktop table and the mobile cards must be empty during that window, and a
   late reply from the old request must not restore either view.
4. Open the page with the legacy `?token=...` URL. Confirm data loads and the
   address bar loses the token where the browser supports
   `history.replaceState`. Do not treat this as a storage mechanism: reloading
   without a token must show the form.
5. DevTools → Application:
   - Manifest: id/start_url/scope are `/`; the two icons render.
   - Service Workers: `/sw.js` activated at scope `/` (HTTPS or localhost only).
   - Cache Storage: exactly `pi-batch-pwa-v1` with the manifest and two icons;
     no `/api/*` or document entries.
6. DevTools → Network, offline mode: trigger 刷新. Expect a disconnected/stale
   status with the last successful refresh time, not zeros or an empty success.
   Go online and confirm the next poll recovers.
7. Install the app (Chrome/Edge "Install", Safari "Add to Home Screen"). Reopen
   it. Expect the same static shell and token form; the token must not be
   prefilled from storage.
8. Narrow the window to about 390 px (or use the device toolbar). The desktop
   task table is replaced by task-first cards, and the first screen shows the
   task title, project/kind, status/phase, execution result versus merge record,
   elapsed/queue/execution timing and the recorded model/thinking configuration
   without horizontal scrolling. `thinking` is the **recorded request
   configuration**, never provider-confirmed effort or thinking duration;
   missing values show 未追踪 rather than 0. Cost, token coverage, artifacts and
   acceptance stay under the collapsed "用量、费用、产物与验收" details; failed or
   blocked work keeps a visible warning line and the attention section stays
   reachable below the cards. Open 详情 to confirm the same structured record the
   desktop row opens, then refresh and confirm the old card disappears together
   with the desktop rows.

## 4. Phone pass (only when TLS and a real device are available)

Repeat steps 3–8 on the phone over HTTPS. Record:

- browser name/version, OS, device model;
- whether "Add to Home Screen"/install appeared and what icon/label was shown;
- whether the worker activated and the cache contents matched;
- whether task cards were readable first (no horizontal scroll), the card width
  fit the viewport, and the essential fields fit above the fold without hiding
  failed or blocked work;
- whether refresh, source switch, timeout and auth denial cleared both the card
  view and any desktop table state, with no stale card left behind;
- one screenshot or note per failure (no token in screenshots).

If no HTTPS certificate or phone is available, **do not** claim mobile/PWA
acceptance. Report "not executed on device" with the reason; localhost and the
Node VM are not substitutes.

## 5. Honest limits

- No push notifications, background sync or offline document caching. An
  already-open page reports disconnected/stale on network loss; a fresh offline
  launch may fail to load the document at all. Offline launch is not guaranteed.
- Runtime timing in the task table is observation only. `queue_wait_seconds` /
  `execution_wall_seconds` are shown when recorded and `未追踪` when absent; they
  are not thinking/tool time, ETA, liveness or goal acceptance. The mobile cards
  show the same values with the same unknowns; “执行结果” is not a merge,
  acceptance or goal verdict.
- `llm[].thinking` is the recorded request configuration only. It does not prove
  the provider applied that effort, how long thinking lasted, or that a model
  call succeeded. Absent/null values stay `未追踪`; older ledger rows are not
  backfilled.
- Mobile cards are a projection of the same bounded snapshot, not a new API or
  a second source of truth. Diagnostics and usage details may be collapsed, but
  recorded flags and failed/blocked states keep a visible warning. Hiding or
  reordering secondary tables must never be used to imply acceptance.
- The shell is read-only. There are no repair, cleanup, merge, budget-reset or
  remote-control buttons, and adding a token does not grant those powers.
- HTTP/static/Node-VM checks prove only their stated scope. The optional Chrome
  regression below adds real-browser evidence, not actual-phone installation,
  remote TLS, background operation or production/seven-day acceptance.
- Costs, acceptance and next steps remain untracked unless the existing
  task-center snapshot records them; missing values are never zero-filled.

## 6. Actual local-browser regression

```bash
python -m pytest tests/test_web_mobile_browser.py -q
```

With local Chrome and Python Playwright installed, this drives a real browser
against a loopback HTTP server with private synthetic ledgers, including a
390×844 viewport. It checks first-screen task visibility, card width/height,
render-error invalidation of both views, login, live completion while a sibling is running,
scheduling display, same-origin worker activation/cache exclusions, token
revocation stopping polling, explicit reconnect, reload without stored tokens,
legacy URL-token removal, and (on the 390 px viewport) that task cards render
first, fit the viewport without horizontal scrolling, keep execution/merge and
recorded model/thinking text visible, and leave failed work discoverable. The
card view and desktop table are cleared together on refresh, source switch,
auth denial and timeout, and a late reply cannot restore an old card.
`tests/test_task_center_mobile.py` additionally executes both inline scripts in
an isolated DOM VM for literal markup, unknown fields, detail selection and
both-view clearing. It does not call a model or start a campaign. Missing
browser/dependency is reported as skipped, not real-browser evidence.

To keep the synthetic screenshot outside the repository, set
`PBATCH_BROWSER_EVIDENCE_DIR` to a private output directory before running.
A simulated viewport is not an Android/iOS device or an installed mobile app.
