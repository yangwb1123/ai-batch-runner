#!/usr/bin/env python3
"""Fixed package import/asset probe used by bounded installation diagnostics."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if argv != ["--json"]:
        return 2
    import pbatch
    import yaml
    from pbatch import config
    from pbatch.health_installation import CORE_ENTRYPOINTS
    from pbatch.hub_web_assets import bundle
    from pbatch.text_io import read_text_bounded, read_yaml_bounded

    package = Path(pbatch.__file__).resolve().parent
    for name, attribute in CORE_ENTRYPOINTS.items():
        if not callable(getattr(importlib.import_module(name), attribute, None)):
            raise ValueError("required callable is unavailable")
    for name in ("role_keywords.yaml", "task_keywords.yaml"):
        value = read_yaml_bounded(package / name, 524288, yaml, "package registry")
        if not isinstance(value, dict) or not value:
            raise ValueError("package registry is missing or malformed")
    for name in ("README.md", "pwa_assets/manifest.webmanifest", "pwa_assets/sw.js",
                 "pwa_assets/icon-192.png", "pwa_assets/icon-512.png"):
        path = package / name
        if not path.is_file() or not 0 < path.stat().st_size <= 1048576:
            raise ValueError("package resource is missing or invalid")
    manifest = json.loads(read_text_bounded(package / "pwa_assets/manifest.webmanifest", 65536))
    if not isinstance(manifest, dict) or not manifest.get("name"):
        raise ValueError("PWA manifest is invalid")
    defaults = read_yaml_bounded(Path(config.TOOL_ROOT) / "pi-batch.yaml", 2097152, yaml, "bundled defaults")
    if not isinstance(defaults, dict) or not defaults:
        raise ValueError("bundled defaults are malformed")
    _verify_entrypoints()
    bundle()
    print(json.dumps({"schema_version": 1, "package_root": str(package),
                      "modules": list(CORE_ENTRYPOINTS), "package_assets": 13}))
    return 0


def _verify_entrypoints() -> None:
    from importlib.metadata import distribution
    from pbatch.health_installation import installation_kind
    if installation_kind() != "wheel":
        return
    entries = distribution("ai-batch-runner").entry_points
    for name in ("abr", "pi-batch"):
        values = [row.value for row in entries if row.group == "console_scripts" and row.name == name]
        if values != ["pbatch.cli:main"]:
            raise ValueError("installed command entrypoints are malformed")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (Exception, KeyboardInterrupt) as exc:
        print(f"installation runtime probe failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
