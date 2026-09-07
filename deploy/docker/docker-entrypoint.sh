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

# AIMLESS_MODE=daemon_only runs just the daemon (no X/VNC/GUI stack), for use
# as a 24/7 remote mailbox reached over an SSH tunnel. Any other value (or
# unset) keeps the full supervised webtop. supervisord expands %(ENV_RUN_GUI)s
# in autostart, so the toggle is computed here as a boolean string.
if [ "${AIMLESS_MODE:-webtop}" = "daemon_only" ]; then
    export RUN_GUI=false
else
    export RUN_GUI=true
fi

# supervisord refuses to start unless every %(ENV_...)s it references is set —
# including VNC_PASS, which only matters when the GUI stack is running.
export VNC_PASS="${VNC_PASS:-}"

chown -R aimless:aimless /data

exec su-exec aimless:aimless /usr/bin/supervisord -n -c /etc/supervisord.conf