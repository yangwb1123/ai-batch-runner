#!/usr/bin/env bash
# aero-im campaign runner: the engine lives in this checkout, the target repo is
# the working directory. Mirrors scripts/sverp/run-campaign.sh.
#
#   run-campaign.sh              # campaign, as configured by campaign.yaml
#   run-campaign.sh --dry-run    # plan only, no files or state written
#   run-campaign.sh --jobs 4 --max-directions 2
#
# AERO_IM_REPO overrides the target repository. The campaign's
# implementation.pipeline points at examples/repository-campaign-pipeline.yaml,
# which stays in the target repository (a repository-level example template that
# the runner checkout also ships), and campaign mode requires the config itself
# to be inside the repository.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
REPO="${AERO_IM_REPO:-$HOME/aero-im}"

test -d "$REPO" || { echo "FAIL: $REPO does not exist (set AERO_IM_REPO)" >&2; exit 1; }

SYNC_DIR="$REPO/.pi-batch/aero-im"
mkdir -p "$SYNC_DIR"
cp "$HERE/campaign.yaml" "$SYNC_DIR/campaign.yaml"
echo "config synced to $SYNC_DIR"

cd "$REPO"
echo "==> cd $REPO"
echo "==> python $ROOT/pi-batch.py campaign --config $SYNC_DIR/campaign.yaml $*"
exec python3 "$ROOT/pi-batch.py" campaign --config "$SYNC_DIR/campaign.yaml" "$@"
