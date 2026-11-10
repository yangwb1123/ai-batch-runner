"""Bounded, digest-checked compiled frontend assets, never a general file server."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .hub_web_provenance import validate_build

ASSET_ROOT = Path(__file__).with_name("hub_web_assets")
LIMITS = {"index.html": 65536, "app.js": 1048576, "app.css": 262144,
          "iris.LICENSE": 8192, "third-party.LICENSE": 32768}
PUBLIC = {"app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8"}


def _read(name, maximum):
    directory = os.open(ASSET_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
                raise ValueError("frontend asset metadata exceeds contract")
            raw = stream.read(maximum + 1)
            if len(raw) != info.st_size or len(raw) > maximum:
                raise ValueError("frontend asset changed or exceeded budget")
            return raw
    finally:
        os.close(directory)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate frontend asset manifest field")
        result[key] = value
    return result


def _validate(manifest):
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "files", "style_hashes", "iris", "build"}:
        raise ValueError("invalid frontend asset manifest")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 2:
        raise ValueError("unsupported frontend asset schema")
    files, hashes = manifest["files"], manifest["style_hashes"]
    if not isinstance(files, dict) or set(files) != set(LIMITS):
        raise ValueError("frontend asset names do not match the shipping contract")
    for name, row in files.items():
        if (not isinstance(row, dict) or set(row) != {"bytes", "sha256"}
                or type(row["bytes"]) is not int or not 0 < row["bytes"] <= LIMITS[name]
                or not isinstance(row["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])):
            raise ValueError("invalid frontend asset digest or size")
    if (not isinstance(hashes, list) or not 1 <= len(hashes) <= 8
            or any(not isinstance(item, str) or not re.fullmatch(r"sha256-[A-Za-z0-9+/]{43}=", item) for item in hashes)):
        raise ValueError("invalid frontend asset stylesheet hashes")
    if not isinstance(manifest["iris"], dict):
        raise ValueError("frontend asset provenance missing")
    validate_build(manifest["build"])


def bundle():
    try:
        manifest = json.loads(_read("assets.json", 32768), object_pairs_hook=_unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid frontend asset manifest encoding") from exc
    _validate(manifest)
    files = {}
    for name, maximum in LIMITS.items():
        raw = _read(name, maximum)
        expected = manifest["files"][name]
        if len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            raise ValueError("frontend asset integrity mismatch")
        files[name] = raw
    return manifest, files


def asset(name):
    if name not in PUBLIC:
        raise ValueError("frontend asset is not public")
    _, files = bundle()
    return files[name], PUBLIC[name]
