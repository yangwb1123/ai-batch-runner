#!/usr/bin/env bash
# Compatibility wrapper for the Python campaign coordinator.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"
exec python pi-batch.py campaign "$@"
