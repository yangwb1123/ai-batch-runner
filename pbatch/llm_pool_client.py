"""Credential-free OpenCode client configuration shared by export and launcher."""

from __future__ import annotations

from .llm_pool_config import PoolConfigCommandError


PROVIDER = "opencode-go"


def client_config(settings, model: str) -> dict:
    if model not in settings.models:
        raise PoolConfigCommandError("model must be configured in the pool")
    if not settings.token_file.is_absolute():
        raise PoolConfigCommandError("gateway token file reference must be absolute")
    reference = str(settings.token_file)
    if any(character in '{}"\\' or ord(character) < 32 or ord(character) == 127
           for character in reference):
        raise PoolConfigCommandError("gateway token path cannot be represented as a safe file reference")
    try:
        reference.encode("utf-8")
    except UnicodeError:
        raise PoolConfigCommandError("gateway token path must be valid UTF-8") from None
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": PROVIDER + "/" + model,
        "provider": {PROVIDER: {"options": {
            "baseURL": f"http://127.0.0.1:{settings.port}/v1",
            "apiKey": "{file:" + reference + "}",
        }}},
    }
