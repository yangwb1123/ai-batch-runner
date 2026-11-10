#!/usr/bin/env python3
"""Offline frontend drift gate; exact rebuilding is the separate web-check gate."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pbatch.hub_web_assets import bundle
from pbatch.hub_web_provenance import check_sources


def main():
    try:
        manifest, _ = bundle()
        check_sources(ROOT / 'web', manifest)
    except (OSError, ValueError, RecursionError) as exc:
        print(f'web-assets-check: FAIL: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    print('web-assets-check: PASS: compiled bytes, build inputs and Iris provenance match')
    print('This is not a rebuild, independent signature, SSO check or production approval.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
