#!/bin/sh
# Fix up the bind-mounted /data volume (host dirs are initially root-owned),
# then drop to the non-root user and run the whole supervised stack as uid 1000.
set -e

mkdir -p /data/state /data/config /data/logs
chown -R aimless:aimless /data

exec su-exec aimless:aimless /usr/bin/supervisord -n -c /etc/supervisord.conf