#!/bin/sh
# Fix up the bind-mounted /data volume (host dirs are initially root-owned),
# fetch the per-release aimless artifacts if needed, then drop to the non-root
# user and run the whole supervised stack as uid 1000.
set -e

mkdir -p /data/state /data/config /data/logs /data/bin

VERSION="${AIMLESS_VERSION:-main}"
BASE="https://raw.githubusercontent.com/lorenzo95/Aimless/$VERSION/dist"

fetch() {
    target="$1"
    url="$2"
    marker="$target.version"
    if [ "$AIMLESS_FETCH" = "always" ] \
       || [ ! -f "$target" ] \
       || [ ! -f "$marker" ] \
       || [ "$(cat "$marker" 2>/dev/null)" != "$VERSION" ]; then
        echo "fetching $(basename "$target") from $VERSION …"
        wget -qO "$target.tmp" "$url"
        mv -f "$target.tmp" "$target"
        printf '%s\n' "$VERSION" > "$marker"
        chmod +x "$target"
    fi
}

# static daemon + client pyz — both fetched from the git dist artifacts
fetch /data/bin/aimless.pyz          "$BASE/aimless.pyz"
fetch /data/bin/aimlessd-linux-amd64 "$BASE/aimlessd-linux-amd64"

chown -R aimless:aimless /data

exec su-exec aimless:aimless /usr/bin/supervisord -n -c /etc/supervisord.conf