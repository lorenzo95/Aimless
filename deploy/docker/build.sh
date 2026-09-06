#!/bin/bash
# Build the evergreen container locally (instead of pulling from GHCR) and start
# it. No artifact sync: the entrypoint fetches the client pyz + daemon from the
# git dist artifacts on first start (or whenever the pinned AIMLESS_VERSION
# marker changes).
set -e
cd "$(dirname "$0")"

docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build "$@"