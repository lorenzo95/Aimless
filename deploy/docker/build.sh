#!/bin/bash
# Build the evergreen container and start it. No artifact sync: the entrypoint
# fetches the client pyz + daemon from the git dist artifacts on first start
# (or whenever the pinned AIMLESS_VERSION marker changes).
set -e
cd "$(dirname "$0")"

docker compose up -d --build "$@"