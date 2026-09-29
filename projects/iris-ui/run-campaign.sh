#!/usr/bin/env bash
# iris-ui campaign runner: the engine lives in this checkout, the target repo is
# the working directory.
#
#   run-campaign.sh --dry-run
#
# IRIS_UI_REPO overrides the target repository.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
REPO="${IRIS_UI_REPO:-$HOME/workspace/job/iris-ui}"

# The validator registry is project data: this project's pi-batch.yaml declares
# the gate it accepts work on.
export PBATCH_PROJECT_CONFIG="$HERE/pi-batch.yaml"

test -d "$REPO" || { echo "FAIL: $REPO does not exist (set IRIS_UI_REPO)" >&2; exit 1; }

SYNC_DIR="$REPO/.pi-batch/iris-ui"
mkdir -p "$SYNC_DIR"
for f in campaign.yaml pipeline.yaml; do
  [ -f "$HERE/$f" ] && cp "$HERE/$f" "$SYNC_DIR/$f"
done
echo "config synced to $SYNC_DIR"

cd "$REPO"
exec python3 "$ROOT/pi-batch.py" campaign --config "$SYNC_DIR/campaign.yaml" "$@"
