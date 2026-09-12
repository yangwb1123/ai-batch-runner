#!/bin/sh
# Proposal/gate pipelines must not let the agent mutate the evidence tree.
set -eu
exec pi "$@" --tools read
