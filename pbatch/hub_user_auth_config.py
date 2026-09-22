"""Private device-login profiles bound to one Hub and authorization origin."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import urllib.parse

from .hub_auth import USER_SCOPES
from .hub_oauth_config import MAX_CONFIG_BYTES, _text, _url, canonical_origin, private_bytes, strict_json

CONFIG_ERROR = "user authorization configuration is invalid or unavailable"
_REQUIRED = {"hub_url", "device_authorization_endpoint", "token_endpoint", "client_id",
             "resource", "scopes", "credential_directory"}
_PROFILE_KEYS = (_REQUIRED - {"credential_directory"}) | {"verification_origin"}


def endpoint_origin(value: str) -> str:
    parsed = _url(value)
    return canonical_origin(f"{parsed.scheme}://{parsed.netloc}")


def normalized_endpoint(value: str) -> str:
    parsed = _url(value)
    if not parsed.path.startswith("/"):
        raise ValueError(CONFIG_ERROR)
    return endpoint_origin(value) + parsed.path


def _user_scopes(value) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= len(USER_SCOPES) + 1:
        raise ValueError(CONFIG_ERROR)
    if any(not isinstance(scope, str) for scope in value):
        raise ValueError(CONFIG_ERROR)
    scopes = set(value)
    if len(scopes) != len(value) or not scopes <= USER_SCOPES | {"openid"}:
        raise ValueError(CONFIG_ERROR)
    if not scopes & USER_SCOPES:
        raise ValueError(CONFIG_ERROR)
    return sorted(scopes)


def validate_user_config(value: dict, base_dir=None, *, hub_url: str = "") -> dict:
    """Normalize a supplied profile, without reading or creating its cache."""
    try:
        if not isinstance(value, dict) or not _REQUIRED <= set(value):
            raise ValueError(CONFIG_ERROR)
        if set(value) - _REQUIRED - {"verification_origin"}:
            raise ValueError(CONFIG_ERROR)
        config = dict(value)
        config["hub_url"] = canonical_origin(config["hub_url"])
        if hub_url and config["hub_url"] != canonical_origin(hub_url):
            raise ValueError(CONFIG_ERROR)
        for key in ("device_authorization_endpoint", "token_endpoint"):
            config[key] = normalized_endpoint(config[key])
        origin = endpoint_origin(config["device_authorization_endpoint"])
        if endpoint_origin(config["token_endpoint"]) != origin:
            raise ValueError(CONFIG_ERROR)
        config["verification_origin"] = canonical_origin(config.get("verification_origin", origin))
        _text(config["client_id"], 1024)
        _text(config["resource"], 2048)
        config["scopes"] = _user_scopes(config["scopes"])
        directory = _text(config["credential_directory"], 4096, whitespace=True)
        base = Path.cwd() if base_dir is None else Path(base_dir)
        config["credential_directory"] = os.path.abspath(base / os.path.expanduser(directory))
    except (OSError, TypeError, ValueError, RecursionError):
        raise ValueError(CONFIG_ERROR) from None
    return config


def load_user_config(path, hub_url: str = "") -> dict:
    try:
        config_path = Path(os.path.abspath(os.path.expanduser(path)))
        value = strict_json(private_bytes(config_path, MAX_CONFIG_BYTES))
        return validate_user_config(value, config_path.parent, hub_url=hub_url)
    except (OSError, TypeError, ValueError, RecursionError):
        raise ValueError(CONFIG_ERROR) from None


def profile_digest(config: dict) -> str:
    value = validate_user_config(config)
    profile = {key: value[key] for key in _PROFILE_KEYS}
    raw = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
