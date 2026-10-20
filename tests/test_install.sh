#!/usr/bin/env bash
# Installer smoke test: runs in a throwaway HOME, never uses sudo, never touches the network.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export HOME="$T/home" XDG_CONFIG_HOME="$T/home/.config" XDG_DATA_HOME="$T/home/.local/share"
mkdir -p "$HOME"
fail() { echo "FAIL: $*" >&2; exit 1; }
I() { bash "$REPO/install.sh" --source "$REPO" --no-systemd --port 28944 "$@"; }

# 1. dry run writes nothing, shows the root step without running it
out="$(I --dry-run --modules drives,backups --smart </dev/null 2>&1)"
[ -z "$(ls -A "$HOME")" ] || fail "dry run wrote files"
grep -q "These exact commands will run with sudo" <<<"$out" && grep -q "would ask: Run these commands with sudo" <<<"$out" || fail "dry run did not show root commands"
grep -qi "would" <<<"$out" || fail "dry run not labelled"

# 2. owner URL is filled in, no placeholders left
grep -q 'GITHUB_OWNER="jasondmillwood"' "$REPO/install.sh" || fail "owner"
! grep -nE 'YOUR_|<owner>|OWNER_PLACEHOLDER' "$REPO/install.sh" || fail "placeholder"

# 3. --with-dish-monitor in dry run names the pinned, checksummed installer
out="$(I --dry-run --with-dish-monitor </dev/null 2>&1)"
grep -q "dish-monitor/v0.1.0/install.sh" <<<"$out" || fail "dish-monitor url"
grep -q "f5aa17859ba5723b" <<<"$out" || fail "dish-monitor sha"
out="$(I --dry-run --with-dish-monitor --mock </dev/null 2>&1)"
grep -q -- "--dishes 1 --mock --no-systemd --dry-run" <<<"$out" || fail "--mock not passed to dish-monitor"

# 4. Ookla is never downloaded without an interactive acceptance (no terminal here): module stays off
setsid bash "$REPO/install.sh" --source "$REPO" --no-systemd --port 28944 --speedtest ookla --modules starlink,speedtest </dev/null >"$T/o" 2>&1 || { cat "$T/o"; fail "ookla run"; }
grep -q "Not accepted: Ookla not downloaded" "$T/o" || fail "ookla refusal text"
grep -q "speedtest.net/about/eula" "$T/o" || fail "eula link"
! grep -q "install.speedtest.net" "$T/o" || fail "ookla fetched"
[ ! -e "$XDG_DATA_HOME/homehub/bin/speedtest" ] || fail "ookla binary present"
python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); assert not c["modules"]["speedtest"]["enabled"]' "$XDG_CONFIG_HOME/homehub/config.json" || fail "speedtest enabled"
bash "$REPO/install.sh" --source "$REPO" --no-systemd --uninstall --purge </dev/null >/dev/null 2>&1

# 5. real install (no systemd, no root)
I --modules starlink,drives,backups --monitor-url http://127.0.0.1:8787 </dev/null >"$T/i" 2>&1 || { cat "$T/i"; fail install; }
CFG="$XDG_CONFIG_HOME/homehub/config.json"
[ "$(stat -c %a "$CFG")" = 600 ] || fail "config mode"
python3 - "$CFG" <<'PY' || fail "config contents"
import json, sys
c = json.load(open(sys.argv[1]))
on = sorted(k for k, v in c["modules"].items() if v.get("enabled"))
assert on == ["backups", "drives", "starlink"], on
assert c["modules"]["starlink"]["monitor_url"] == "http://127.0.0.1:8787"
assert c["site"]["port"] == 28944
assert c["secrets"]["hub_token_file"].startswith("/")
PY
[ -s "$XDG_CONFIG_HOME/homehub/hub.token" ] || fail "token"
I --uninstall </dev/null >/dev/null 2>&1 || fail uninstall
[ -f "$CFG" ] || fail "uninstall without --purge removed config"
I --uninstall --purge </dev/null >/dev/null 2>&1 || fail purge
[ ! -e "$XDG_CONFIG_HOME/homehub" ] && [ ! -e "$XDG_DATA_HOME/homehub" ] || fail "purge left files"
echo "install tests: ok"
