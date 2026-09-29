#!/usr/bin/env bash
# aero-id campaign runner: the engine lives in this checkout, the target repo is
# the working directory. Mirrors scripts/sverp/run-campaign.sh.
#
#   run-campaign.sh                 # campaign, as configured by campaign.yaml
#   run-campaign.sh --dry-run       # plan only, no files or state written
#   run-campaign.sh --jobs 4 --max-directions 2
#
# AERO_ID_REPO overrides the target repository.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
REPO="${AERO_ID_REPO:-$HOME/aero-id}"

# The validator registry is project data: this project's pi-batch.yaml declares
# the gate it accepts work on (repo-gate = the target repository's own AGENTS.md
# / HARNESS.md command). Without this the tool's own registry would win and the
# campaign would gate on validators the target repository never declared.
export PBATCH_PROJECT_CONFIG="$HERE/pi-batch.yaml"

test -d "$REPO" || { echo "FAIL: $REPO does not exist (set AERO_ID_REPO)" >&2; exit 1; }

# campaign mode requires the config and the pipeline template inside the
# repository ("campaign path escapes repository" otherwise), so sync them into
# the gitignored .pi-batch/aero-id/. This directory is the source of truth.
SYNC_DIR="$REPO/.pi-batch/aero-id"
mkdir -p "$SYNC_DIR"
cp "$HERE/campaign.yaml" "$SYNC_DIR/campaign.yaml"
cp "$HERE/campaign-pipeline.yaml" "$SYNC_DIR/campaign-pipeline.yaml"
cp "$HERE/impl-pipeline.yaml" "$SYNC_DIR/impl-pipeline.yaml"
echo "config synced to $SYNC_DIR"

cd "$REPO"
echo "==> cd $REPO"
echo "==> python $ROOT/pi-batch.py campaign --config $SYNC_DIR/campaign.yaml $*"
exec python3 "$ROOT/pi-batch.py" campaign --config "$SYNC_DIR/campaign.yaml" "$@"
