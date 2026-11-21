#!/bin/sh
set -eu

TRACKING_BEARER=$(cat /run/tracking/proxy/bearer)
case "$TRACKING_BEARER" in
    *[!A-Za-z0-9_-]*) exit 1 ;;
esac
test "${#TRACKING_BEARER}" = 43
export TRACKING_BEARER
# The upstream image adds cap_net_bind_service to /usr/bin/caddy. A plain
# copy omits file capabilities so the binary can run under drop: [ALL].
rm -f /tmp/caddy
cp /usr/bin/caddy /tmp/caddy
chmod 0500 /tmp/caddy
exec /tmp/caddy run --config /etc/tracking/Caddyfile --adapter caddyfile
