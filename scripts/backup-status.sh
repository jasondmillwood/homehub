#!/usr/bin/env bash
# Run a backup command and write the homehub status JSON for it.
#   backup-status.sh <status-file> [--repo-bytes-cmd CMD] [--free-path DIR] -- <backup command...>
# Example (any backup command, user timer):
#   backup-status.sh ~/.local/share/homehub/status/usb-copy.json --free-path /mnt/usb -- \
#     rsync -a --delete ~/Documents/ /mnt/usb/documents/
# If HOMEHUB_PUSH_URL (e.g. http://hub:8090/api/backups/push?job=usb) and HOMEHUB_TOKEN_FILE are set, the status
# is also pushed to a hub on another machine.
set -uo pipefail
OUT=${1:?status file}; shift
FREE_PATH=""; REPO_CMD=""
while [ $# -gt 0 ] && [ "$1" != "--" ]; do
  case "$1" in --free-path) FREE_PATH=$2; shift 2 ;; --repo-bytes-cmd) REPO_CMD=$2; shift 2 ;; *) echo "unknown: $1" >&2; exit 2 ;; esac
done
[ "${1:-}" = "--" ] && shift
[ $# -gt 0 ] || { echo "no backup command given" >&2; exit 2; }
start=$(date +%s)
"$@"; rc=$?
end=$(date +%s)
free=null; mounted=null
if [ -n "$FREE_PATH" ]; then
  if mountpoint -q "$FREE_PATH" 2>/dev/null || [ -d "$FREE_PATH" ]; then mounted=true; free=$(df -B1 --output=avail "$FREE_PATH" | tail -1 | tr -d ' ')
  else mounted=false; fi
fi
repo=null; [ -n "$REPO_CMD" ] && repo=$(bash -c "$REPO_CMD" 2>/dev/null | tr -dc 0-9); repo=${repo:-null}
ok=false; [ $rc -eq 0 ] && ok=true
mkdir -p "$(dirname "$OUT")"
printf '{"start":%s,"end":%s,"ok":%s,"rc":%s,"step":"%s","driveFreeBytes":%s,"mounted":%s,"repoBytes":%s}\n' \
  "$start" "$end" "$ok" "$rc" "$(basename "$1")" "$free" "$mounted" "$repo" > "$OUT.tmp" && mv -f "$OUT.tmp" "$OUT"
if [ -n "${HOMEHUB_PUSH_URL:-}" ] && [ -r "${HOMEHUB_TOKEN_FILE:-/nonexistent}" ]; then
  curl -fsS -m 15 -H "X-Hub-Token: $(cat "$HOMEHUB_TOKEN_FILE")" -H "Content-Type: application/json" \
    --data-binary "@$OUT" "$HOMEHUB_PUSH_URL" >/dev/null || echo "push to hub failed" >&2
fi
exit $rc
