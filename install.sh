#!/usr/bin/env bash
# homehub installer (Linux, systemd user service). Runs as your user. Root is used only for the optional
# SMART step, and only after it prints the exact commands and you confirm.
#
#   curl -fsSL https://raw.githubusercontent.com/jasondmillwood/homehub/v0.1.0/install.sh | bash -s -- --modules starlink,drives
#   ./install.sh --modules starlink,drives,backups --with-dish-monitor --dishes 2 --iface1 wlan0 --iface2 wlan1
#   ./install.sh --dry-run ...            # print every step and file, change nothing
#   ./install.sh --uninstall [--purge]
set -euo pipefail

# GitHub user or organisation that owns the repo (forks: change this one line)
GITHUB_OWNER="jasondmillwood"
REPO_URL="${HOMEHUB_REPO:-https://github.com/${GITHUB_OWNER}/homehub.git}"
REF="${HOMEHUB_REF:-v0.1.0}"
# dish-monitor (the starlink module's data source) is installed from its own release, verified by checksum.
DM_URL="https://raw.githubusercontent.com/jasondmillwood/dish-monitor/v0.1.0/install.sh"
DM_SHA256="f5aa17859ba5723b5c6574cc9a0074dccf236ca120aab0f39811a5c761d81fa0"
LIBRESPEED_VER="1.0.14"
declare -A LIBRESPEED_SHA=([amd64]=89800767ac14085c78a20847ebea23340f6c14a78de0a15c2ac7db8b565c961f
                           [arm64]=75e51a2494d03cb35a92ddbf862b40571a25a1526f3cf3dfa8b1d5d7bc622bd9)
OOKLA_VER="1.2.0"
declare -A OOKLA_SHA=([x86_64]=5690596c54ff9bed63fa3732f818a05dbc2db19ad36ed68f21ca5f64d5cfeeb7
                      [aarch64]=3953d231da3783e2bf8904b6dd72767c5c6e533e163d3742fd0437affa431bd3)

PREFIX="${HOMEHUB_PREFIX:-$HOME/.local/share/homehub}"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/homehub"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/homehub"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ALL_MODULES="starlink speedtest drives backups machines gpu alerts explain"
MODULES="starlink,drives" PORT=8090 HOST=0.0.0.0 MONITOR_URL="http://127.0.0.1:8787"
WITH_DM=0 DISHES=1 IFACE1="" IFACE2="" SPEED="" SMART=0 DEMO=0 MOCK=0 DRY=0 UNINSTALL=0 PURGE=0 SOURCE="" NO_SYSTEMD=0

usage() { sed -n '2,8p' "${BASH_SOURCE[0]}" 2>/dev/null | sed 's/^# \{0,1\}//'; cat <<U
Options:
  --modules a,b,c        modules to enable: $ALL_MODULES (default starlink,drives)
  --monitor-url URL      where dish-monitor runs (default $MONITOR_URL)
  --with-dish-monitor    also install dish-monitor v0.1.0 here (--dishes 1|2, --iface1, --iface2)
  --mock                 with --with-dish-monitor: dish-monitor uses simulated dishes (implied by --demo)
  --speedtest TOOL       librespeed (open source) or ookla (asks you to accept Ookla's EULA first)
  --smart                set up SMART dumps (ROOT: shows the commands and asks before running them)
  --port N  --host ADDR  listen port (8090) and address (0.0.0.0 = LAN; 127.0.0.1 = this machine only)
  --demo                 run the service with simulated data (try it without hardware)
  --source DIR           install from a local checkout   --prefix DIR   --no-systemd
  --dry-run              print every step, change nothing
  --uninstall [--purge]  remove service + code; --purge also deletes config, data and state
U
}
while [ $# -gt 0 ]; do
  case "$1" in
    --modules) MODULES="$2"; shift ;;
    --monitor-url) MONITOR_URL="$2"; shift ;;
    --with-dish-monitor) WITH_DM=1 ;;
    --dishes) DISHES="$2"; shift ;;
    --iface1) IFACE1="$2"; shift ;;
    --iface2) IFACE2="$2"; shift ;;
    --speedtest) SPEED="$2"; shift ;;
    --smart) SMART=1 ;;
    --port) PORT="$2"; shift ;;
    --host) HOST="$2"; shift ;;
    --demo) DEMO=1 ;;
    --mock) MOCK=1 ;;
    --source) SOURCE="$2"; shift ;;
    --prefix) PREFIX="$2"; shift ;;
    --no-systemd) NO_SYSTEMD=1 ;;
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

say() { printf '==> %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi; }
write() { # write <path> <mode>  (content on stdin)
  if [ "$DRY" = 1 ]; then printf '[dry-run] write %s (mode %s):\n' "$1" "$2"; sed 's/^/    | /'; else
    mkdir -p "$(dirname "$1")"; (umask 077; cat > "$1.tmp"); chmod "$2" "$1.tmp"; mv "$1.tmp" "$1"; fi
}
have_systemd() { [ "$NO_SYSTEMD" = 0 ] && command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; }
ask() { # ask "<question>" -> 0 if the user typed the expected word (default y)
  local want="${2:-y}" a=""
  if ! { : < /dev/tty; } 2>/dev/null; then echo "(no terminal to confirm on: skipped)"; return 1; fi
  printf '%s ' "$1" > /dev/tty; read -r a < /dev/tty || true
  [ "$a" = "$want" ] || { [ "$want" = y ] && [ "$a" = Y ]; }
}
fetch() { # fetch <url> <out> <sha256>
  run curl -fsSL --proto '=https' -o "$2" "$1"
  if [ "$DRY" = 1 ]; then echo "[dry-run] verify sha256 $3"; return; fi
  echo "$3  $2" | sha256sum -c --quiet - || { echo "checksum mismatch for $1" >&2; rm -f "$2"; exit 1; }
}

# ---------------- validate ----------------
case "$PORT" in ''|*[!0-9]*) echo "--port must be a number" >&2; exit 2 ;; esac
case "$DISHES" in 1|2) ;; *) echo "--dishes must be 1 or 2" >&2; exit 2 ;; esac
case "$SPEED" in ""|librespeed|ookla) ;; *) echo "--speedtest must be librespeed or ookla" >&2; exit 2 ;; esac
for i in "$IFACE1" "$IFACE2"; do case "$i" in *[!A-Za-z0-9_.:-]*) echo "bad interface name: $i" >&2; exit 2 ;; esac; done
case "$MONITOR_URL" in http://*|https://*) ;; *) echo "--monitor-url must start with http:// or https://" >&2; exit 2 ;; esac
case "$MONITOR_URL" in *[!A-Za-z0-9:/._-]*) echo "bad --monitor-url" >&2; exit 2 ;; esac
MODS=$(echo "$MODULES" | tr ',' ' ')
[ -n "$SPEED" ] && MODS="$MODS speedtest"
for m in $MODS; do case " $ALL_MODULES " in *" $m "*) ;; *) echo "unknown module: $m (choose from: $ALL_MODULES)" >&2; exit 2 ;; esac; done
on() { case " $MODS " in *" $1 "*) return 0 ;; esac; return 1; }

# ---------------- uninstall ----------------
if [ "$UNINSTALL" = 1 ]; then
  say "Uninstalling homehub"
  if have_systemd; then run systemctl --user disable --now homehub.service 2>/dev/null || true; fi
  run rm -f "$UNIT_DIR/homehub.service"
  if have_systemd; then run systemctl --user daemon-reload || true; fi
  run rm -rf "$PREFIX/app" "$PREFIX/bin"
  if [ "$PURGE" = 1 ]; then run rm -rf "$CONF_DIR" "$STATE_DIR" "$PREFIX"
  else say "Kept config ($CONF_DIR) and data ($PREFIX); use --purge to delete them"; fi
  if [ -e /usr/local/sbin/smart-dump ]; then
    say "Root parts from --smart are still installed. To remove them yourself:"
    echo "    sudo systemctl disable --now smart-dump.timer"
    echo "    sudo rm -f /etc/systemd/system/smart-dump.{service,timer} /usr/local/sbin/smart-dump /usr/local/sbin/smart-dump-usb /usr/local/bin/smart-event"
  fi
  say "dish-monitor (if installed) is separate: run its install.sh --uninstall."
  say "Done."
  exit 0
fi

# ---------------- preflight ----------------
say "Preflight"
command -v python3 >/dev/null || { echo "python3 >= 3.10 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "python3 >= 3.10 required, found $(python3 -V)" >&2; exit 1; }

# ---------------- code ----------------
APP="$PREFIX/app"
say "Installing code into $APP"
if [ -n "$SOURCE" ]; then
  [ -f "$SOURCE/homehub/__init__.py" ] || { echo "--source $SOURCE is not a homehub checkout" >&2; exit 1; }
  run mkdir -p "$APP"
  if [ "$DRY" = 1 ]; then echo "[dry-run] copy $SOURCE -> $APP (code only)"; else
    tar -C "$SOURCE" --exclude=.git --exclude=__pycache__ --exclude=config.json -cf - homehub scripts config.example.json LICENSE README.md | tar -C "$APP" -xf -; fi
else
  case "$REPO_URL" in *"<OWNER>"*) echo "GITHUB_OWNER is a placeholder; use --source DIR or HOMEHUB_REPO=<git url>." >&2; exit 1 ;; esac
  command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
  if [ -d "$APP/.git" ]; then run git -C "$APP" fetch -q --depth 1 origin "$REF"; run git -C "$APP" checkout -q FETCH_HEAD
  else run git clone -q --depth 1 --branch "$REF" "$REPO_URL" "$APP"; fi
fi

# ---------------- speed test binary (optional) ----------------
OOKLA_OK=false
if [ -n "$SPEED" ]; then
  run mkdir -p "$PREFIX/bin"
  TMP=$(mktemp -d 2>/dev/null || echo /tmp/homehub-dl)
  if [ "$SPEED" = librespeed ]; then
    case "$(uname -m)" in x86_64) A=amd64 ;; aarch64|arm64) A=arm64 ;; *) A="" ;; esac
    if [ -z "$A" ]; then echo "no LibreSpeed build pinned for $(uname -m); install librespeed-cli yourself" >&2
    else
      say "Downloading LibreSpeed CLI $LIBRESPEED_VER (LGPL-3.0, github.com/librespeed/speedtest-cli)"
      fetch "https://github.com/librespeed/speedtest-cli/releases/download/v$LIBRESPEED_VER/librespeed-cli_${LIBRESPEED_VER}_linux_$A.tar.gz" "$TMP/ls.tgz" "${LIBRESPEED_SHA[$A]}"
      run tar -xzf "$TMP/ls.tgz" -C "$PREFIX/bin" librespeed-cli
    fi
  else
    cat <<T
==> Ookla Speedtest CLI is proprietary software from Ookla. homehub does not include it.
    Before it is downloaded you must read and accept Ookla's terms:
      EULA:            https://www.speedtest.net/about/eula
      Terms of use:    https://www.speedtest.net/about/terms
      Privacy policy:  https://www.speedtest.net/about/privacy
    Each test sends data to Ookla. Prefer an open tool? Re-run with --speedtest librespeed.
T
    if [ "$DRY" = 1 ]; then echo "[dry-run] ask: type 'accept' to accept Ookla's EULA, terms and privacy policy"; OOKLA_OK=true
    elif ask "Type 'accept' to accept Ookla's EULA, terms and privacy policy (anything else skips):" accept; then OOKLA_OK=true
    else echo "Not accepted: Ookla not downloaded; the speedtest module stays off."; MODS=${MODS//speedtest/}; SPEED=""; fi
    if [ "$OOKLA_OK" = true ]; then
      M=$(uname -m); case "$M" in x86_64|aarch64) ;; *) echo "no Ookla build pinned for $M" >&2; exit 1 ;; esac
      fetch "https://install.speedtest.net/app/cli/ookla-speedtest-$OOKLA_VER-linux-$M.tgz" "$TMP/ookla.tgz" "${OOKLA_SHA[$M]}"
      run tar -xzf "$TMP/ookla.tgz" -C "$PREFIX/bin" speedtest
    fi
  fi
fi

# ---------------- config (never overwritten) ----------------
CONF="$CONF_DIR/config.json"
TOKEN_FILE="$CONF_DIR/hub.token"
if [ ! -f "$TOKEN_FILE" ]; then
  say "Creating hub token $TOKEN_FILE (mode 600)"
  python3 -c 'import secrets; print(secrets.token_hex(24))' | write "$TOKEN_FILE" 600
fi
if [ -f "$CONF" ]; then
  say "Keeping existing $CONF (edit it to change modules)"
else
  say "Writing $CONF with modules: $(echo $MODS)"
  TOPIC=""; on alerts && TOPIC="homehub-$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  MODS="$MODS" PORT="$PORT" HOST="$HOST" MON="$MONITOR_URL" TOK="$TOKEN_FILE" SPEED="${SPEED:-librespeed}" OOK="$OOKLA_OK" \
  TOPIC="$TOPIC" BIN="$PREFIX/bin" python3 - <<'PY' | write "$CONF" 600
import json, os
e = os.environ; mods = set(e["MODS"].split())
c = {"site": {"title": "Home", "subtitle": "HUB", "port": int(e["PORT"]), "bind": e["HOST"]},
     "secrets": {"hub_token_file": e["TOK"], "ntfy_token_file": "", "xai_api_key_file": ""},
     "modules": {m: {"enabled": m in mods} for m in "starlink speedtest drives backups machines gpu alerts explain".split()}}
c["modules"]["starlink"]["monitor_url"] = e["MON"]
c["modules"]["speedtest"].update(tool=e["SPEED"], ookla_license_accepted=e["OOK"] == "true",
                                 binary=os.path.join(e["BIN"], "speedtest" if e["SPEED"] == "ookla" else "librespeed-cli"))
if e["TOPIC"]: c["modules"]["alerts"]["ntfy_topic"] = e["TOPIC"]
print(json.dumps(c, indent=2))
PY
  on alerts && say "ntfy topic: $TOPIC  (subscribe to it in the ntfy app; keep it private)"
  on explain && say "explain: put your xAI API key in a file (chmod 600) and set secrets.xai_api_key_file in $CONF"
fi
run mkdir -p "$STATE_DIR"

# ---------------- dish-monitor (optional) ----------------
if [ "$WITH_DM" = 1 ]; then
  say "Installing dish-monitor v0.1.0 (verified by sha256)"
  DMI=$(mktemp 2>/dev/null || echo /tmp/dm-install.sh)
  fetch "$DM_URL" "$DMI" "$DM_SHA256"
  args=(--dishes "$DISHES"); [ -n "$IFACE1" ] && args+=(--iface1 "$IFACE1"); [ -n "$IFACE2" ] && args+=(--iface2 "$IFACE2")
  if [ "$MOCK" = 1 ] || [ "$DEMO" = 1 ]; then args+=(--mock); fi
  [ "$NO_SYSTEMD" = 1 ] && args+=(--no-systemd); [ "$DRY" = 1 ] && args+=(--dry-run)
  if [ "$DRY" = 1 ]; then echo "[dry-run] bash $DMI ${args[*]}"; else bash "$DMI" "${args[@]}"; fi
fi

# ---------------- service ----------------
DEMOARG=""; [ "$DEMO" = 1 ] && DEMOARG=" --demo"
write "$UNIT_DIR/homehub.service" 644 <<U
[Unit]
Description=homehub (home monitoring dashboard, port $PORT)
After=network-online.target

[Service]
WorkingDirectory=$APP
Environment=HOMEHUB_CONFIG=$CONF PYTHONUNBUFFERED=1
ExecStart=$(command -v python3) -m homehub --config $CONF$DEMOARG
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes

[Install]
WantedBy=default.target
U
if [ "$DRY" = 0 ]; then (cd "$APP" && python3 -m homehub --config "$CONF" --check); fi
if have_systemd; then
  run systemctl --user daemon-reload
  run systemctl --user enable --now homehub.service
  say "Tip: 'loginctl enable-linger \$USER' keeps it running after logout."
else
  say "systemd user session not available; start it with:  cd $APP && python3 -m homehub --config $CONF$DEMOARG"
fi

# ---------------- root step: SMART (optional, confirmed) ----------------
if [ "$SMART" = 1 ]; then
  SRC="$APP/scripts/root"
  ROOTCMDS=$(cat <<R
command -v smartctl >/dev/null || { (command -v apt-get >/dev/null && apt-get install -y smartmontools) || (command -v pacman >/dev/null && pacman -S --needed --noconfirm smartmontools) || (command -v dnf >/dev/null && dnf install -y smartmontools); }
install -m 0755 -o root -g root "$SRC/smart-dump" /usr/local/sbin/smart-dump
install -m 0755 -o root -g root "$SRC/smart-dump-usb" /usr/local/sbin/smart-dump-usb
install -m 0755 -o root -g root "$SRC/smart-event" /usr/local/bin/smart-event
install -m 0644 -o root -g root "$SRC/systemd/smart-dump.service" "$SRC/systemd/smart-dump.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now smart-dump.timer
R
)
  echo "==> The SMART step needs root. These exact commands will run with sudo:"
  echo "$ROOTCMDS" | sed 's/^/    # /'
  echo "    (optional drive-event hook: add '-M exec /usr/local/bin/smart-event' to the DEVICESCAN line of the smartmontools daemon config)"
  echo "    (USB drive that sleeps: see README 'Sleep-safe USB SMART' for the one-line drop-in on your backup unit)"
  if [ "$DRY" = 1 ]; then echo "[dry-run] would ask: Run these commands with sudo? [y/N]"
  elif ask "Run these commands with sudo? [y/N]"; then sudo sh -ec "$ROOTCMDS"; say "SMART dumps installed (every 30 min, sleeping drives skipped)."
  else say "Skipped. You can run the commands above yourself later."; fi
fi

say "Done. Open http://localhost:$PORT  ·  config: $CONF  ·  uninstall: install.sh --uninstall"
