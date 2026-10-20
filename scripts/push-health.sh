#!/usr/bin/env bash
# Linux machine -> hub health report (machines module, host must have "push": true).
#   HOMEHUB_URL=http://hub:8090 HOMEHUB_TOKEN_FILE=~/.config/homehub/hub.token push-health.sh
# Run it from a user timer every 10-15 minutes.
set -euo pipefail
: "${HOMEHUB_URL:?}" "${HOMEHUB_TOKEN_FILE:?}"
host=${HOMEHUB_HOST:-$(hostname -s | tr 'A-Z' 'a-z')}
up=$(cut -d. -f1 /proc/uptime); load=$(cut -d' ' -f1 /proc/loadavg)
free=$(df --output=pcent / | tail -1 | tr -dc 0-9); free=$((100 - free))
curl -fsS -m 15 -H "X-Hub-Token: $(cat "$HOMEHUB_TOKEN_FILE")" -H "Content-Type: application/json" \
  --data "{\"uptime_s\":$up,\"load\":$load,\"disk_free_pct\":$free}" "$HOMEHUB_URL/api/machines/push?host=$host" >/dev/null
