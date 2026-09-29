#!/usr/bin/env bash
# snaplink campaign runner: the engine lives in this checkout, the target repo is
# the working directory. Mirrors scripts/sverp/run-campaign.sh.
#
#   run-campaign.sh --dry-run
#   run-campaign.sh --jobs 4 --max-directions 2
#
# SNAPLINK_REPO overrides the target repository.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
REPO="${SNAPLINK_REPO:-$HOME/snaplink}"

# The validator registry is project data: this project's pi-batch.yaml declares
# the gate it accepts work on (repo-gate = the target repository's own AGENTS.md
# / HARNESS.md command). Without this the tool's own registry would win and the
# campaign would gate on validators the target repository never declared.
export PBATCH_PROJECT_CONFIG="$HERE/pi-batch.yaml"

test -d "$REPO" || { echo "FAIL: $REPO does not exist (set SNAPLINK_REPO)" >&2; exit 1; }

# campaign mode requires the config and the pipeline template inside the
# repository, so sync them into the gitignored .pi-batch/snaplink/. This
# directory is the source of truth for all three documents.
SYNC_DIR="$REPO/.pi-batch/snaplink"
mkdir -p "$SYNC_DIR"
cp "$HERE/campaign.yaml" "$SYNC_DIR/campaign.yaml"
cp "$HERE/campaign-pipeline.yaml" "$SYNC_DIR/campaign-pipeline.yaml"
cp "$HERE/pi-batch.yaml" "$REPO/.pi-batch/snaplink/pi-batch.yaml"
echo "config synced to $SYNC_DIR"

cd "$REPO"
echo "==> cd $REPO"
echo "==> python $ROOT/pi-batch.py campaign --config $SYNC_DIR/campaign.yaml $*"
exec python3 "$ROOT/pi-batch.py" campaign --config "$SYNC_DIR/campaign.yaml" "$@"
