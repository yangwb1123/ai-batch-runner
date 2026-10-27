"""Snaplink SDK transport seam with existing Hub OAuth byte/deadline guards."""

from __future__ import annotations

import http.client
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .hub_errors import HubAccessError
from .hub_jwks import _https_url
from .hub_oauth_config import strict_json
from .hub_oauth_http import NoRedirect, TokenExchangeError, _read_response


class SDKTransport:
    """No redirects, ambient proxies, retries, or SDK-default unbounded reads.

    Only the hosted-login token exchange is enabled. Session expiry requires
    explicit login; this adapter never refreshes or retries a consumed code.
    """

    def __init__(self, issuer):
        self.endpoint = _https_url(issuer, "web issuer").rstrip("/") + "/token"
        self._opener = build_opener(ProxyHandler({}), NoRedirect())

    def send(self, request):
        if request.get("url") != self.endpoint or request.get("method") != "POST":
            raise HubAccessError("login_unavailable", "unsupported SDK request", 503)
        body = request.get("body")
        if not isinstance(body, bytes) or len(body) > 16384:
            raise HubAccessError("login_unavailable", "invalid SDK request", 503)
        call = Request(self.endpoint, data=body, method="POST", headers=request.get("headers", {}))
        deadline = time.monotonic() + 5
        try:
            try:
                response = self._opener.open(call, timeout=5)
            except HTTPError as exc:
                response = exc
            with response:
                raw = _read_response(response, deadline)
                strict_json(raw)  # reject duplicate keys/non-finite JSON before the SDK decodes
                return {"status": response.status, "body": raw}
        except (OSError, URLError, http.client.HTTPException, ValueError,
                RecursionError, TokenExchangeError):
            raise HubAccessError("login_unavailable", "Snaplink token service unavailable", 503) from None
