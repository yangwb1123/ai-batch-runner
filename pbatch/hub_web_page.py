"""Credential-free Svelte/Iris shell; all login and resource authority stays in Hub."""
from __future__ import annotations

import base64

from .hub_web_assets import bundle


def page():
    _, files = bundle()
    return files["index.html"].decode("utf-8", "strict")


def security_policy(login_origin):
    manifest, _ = bundle()
    script = base64.b64encode(bytes.fromhex(manifest["files"]["app.js"]["sha256"])).decode("ascii")
    styles = " ".join("'" + value + "'" for value in manifest["style_hashes"])
    return ("default-src 'none'; script-src 'sha256-" + script + "'; script-src-attr 'none'; "
            "style-src 'self' " + styles + "; style-src-attr 'none'; connect-src 'self'; "
            "img-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; "
            "form-action 'self' " + login_origin)
