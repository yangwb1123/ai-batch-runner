#!/usr/bin/env bash
# snaplink-console campaign runner: the engine lives in this checkout, the target
# repo is the working directory. Mirrors scripts/sverp/run-campaign.sh.
#
#   run-campaign.sh                     # campaign.yaml (B6 审计读路径)
#   run-campaign.sh --advance           # campaign-advance.yaml (第二轮)
#   run-campaign.sh --dry-run           # plan only
#
# CONSOLE_REPO overrides the target repository. The campaign's
# implementation.pipeline points at examples/repository-campaign-pipeline.yaml,
# which stays in the target repository (a repository-level example template the
# runner checkout also ships), and campaign mode requires the config itself to
# be inside the repository.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
REPO="${CONSOLE_REPO:-$HOME/snaplink-console}"

CONFIG=campaign.yaml
if [ "${1:-}" = "--advance" ]; then CONFIG=campaign-advance.yaml; shift; fi

test -d "$REPO" || { echo "FAIL: $REPO does not exist (set CONSOLE_REPO)" >&2; exit 1; }

SYNC_DIR="$REPO/.pi-batch/snaplink-console"
mkdir -p "$SYNC_DIR"
cp "$HERE/$CONFIG" "$SYNC_DIR/campaign.yaml"
echo "config synced to $SYNC_DIR ($CONFIG)"

cd "$REPO"
echo "==> cd $REPO"
echo "==> python $ROOT/pi-batch.py campaign --config $SYNC_DIR/campaign.yaml $*"
exec python3 "$ROOT/pi-batch.py" campaign --config "$SYNC_DIR/campaign.yaml" "$@"
