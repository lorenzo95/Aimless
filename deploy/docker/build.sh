#!/bin/bash
# Sync the freshly packaged aimless artifacts from the repo, then build and start.
set -e
cd "$(dirname "$0")"

mkdir -p dist daemon
cp -f ../../dist/aimless.pyz dist/
cp -f ../../daemon/*.go ../../daemon/go.mod ../../daemon/go.sum daemon/
echo "artifacts synced from ../../dist + ../../daemon"

docker compose up -d --build "$@"