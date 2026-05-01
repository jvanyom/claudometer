#!/usr/bin/env bash
# Install Claudometer on any Linux machine with systemd + a desktop session.
#
# Usage:
#   ./install.sh [--poll-interval N] [--org-id UUID] [--non-interactive]
#
#   --poll-interval N   Minutes between polls (default: keep existing, or 5).
#   --org-id UUID       Skip auto-discovery and use this org UUID.
#   --non-interactive   Fail instead of prompting for missing input.
#
# Idempotent: re-run any time to change the polling interval, refresh the
# cookie, or re-render systemd units after editing the templates.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/claudometer"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/claudometer"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

POLL_INTERVAL=""
ORG_ID_OVERRIDE=""
INTERACTIVE=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --poll-interval) POLL_INTERVAL="$2"; shift 2 ;;
    --poll-interval=*) POLL_INTERVAL="${1#*=}"; shift ;;
    --org-id) ORG_ID_OVERRIDE="$2"; shift 2 ;;
    --org-id=*) ORG_ID_OVERRIDE="${1#*=}"; shift ;;
    --non-interactive) INTERACTIVE=0; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown flag: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$SYSTEMD_USER_DIR" "$AUTOSTART_DIR"

# ---------------------------------------------------------------------------
# 1. Prerequisites: python3, notify-send, systemctl --user, AppIndicator gir.
# ---------------------------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1" >&2; return 1; }; }
need python3
need systemctl
need notify-send || echo "notify-send missing; install libnotify-bin (or your distro's equivalent) for desktop notifications."

if ! python3 -c "import gi; gi.require_version('AyatanaAppIndicator3','0.1'); \
                 from gi.repository import AyatanaAppIndicator3" 2>/dev/null \
   && ! python3 -c "import gi; gi.require_version('AppIndicator3','0.1'); \
                 from gi.repository import AppIndicator3" 2>/dev/null; then
  echo "Tray needs AppIndicator Python bindings."
  if command -v apt-get >/dev/null 2>&1; then
    echo "Installing gir1.2-ayatanaappindicator3-0.1 (sudo)..."
    sudo apt-get install -y gir1.2-ayatanaappindicator3-0.1
  elif command -v dnf >/dev/null 2>&1; then
    echo "Installing libayatana-appindicator-gtk3 (sudo)..."
    sudo dnf install -y libayatana-appindicator-gtk3
  elif command -v pacman >/dev/null 2>&1; then
    echo "Installing libayatana-appindicator (sudo)..."
    sudo pacman -S --needed libayatana-appindicator
  else
    echo "Install the AppIndicator Python GI binding for your distro before running install.sh again." >&2
    exit 3
  fi
fi

# ---------------------------------------------------------------------------
# 2. Cookie: prompt unless one is saved (and they don't want to replace).
# ---------------------------------------------------------------------------
COOKIE_PATH="$CONFIG_DIR/sessionKey"
prompt_cookie=1
if [[ -s "$COOKIE_PATH" ]]; then
  if [[ $INTERACTIVE -eq 1 ]]; then
    read -r -p "sessionKey already saved. Replace it? [y/N] " ans
    [[ "${ans,,}" == "y" ]] || prompt_cookie=0
  else
    prompt_cookie=0
  fi
fi
if [[ $prompt_cookie -eq 1 ]]; then
  if [[ $INTERACTIVE -eq 0 ]]; then
    echo "No cookie saved and --non-interactive set." >&2; exit 4
  fi
  echo
  echo "Paste your claude.ai sessionKey cookie value (input hidden)."
  echo "How to find it: open https://claude.ai logged in, F12 -> Storage"
  echo "(Firefox) or Application (Chrome) -> Cookies -> https://claude.ai"
  echo "-> sessionKey -> copy the Value field."
  echo
  read -r -s -p "sessionKey: " cookie_value
  echo
  [[ -n "$cookie_value" ]] || { echo "Empty value." >&2; exit 5; }
  printf '%s' "$cookie_value" > "$COOKIE_PATH"
  chmod 600 "$COOKIE_PATH"
  echo "Saved to $COOKIE_PATH (chmod 600)"
fi

# ---------------------------------------------------------------------------
# 3. Config: copy template if missing; resolve org_id.
# ---------------------------------------------------------------------------
CONFIG_PATH="$CONFIG_DIR/config.ini"
if [[ ! -f "$CONFIG_PATH" ]]; then
  cp "$REPO_DIR/config.example.ini" "$CONFIG_PATH"
  echo "Wrote $CONFIG_PATH"
fi

current_org="$(awk -F'=' '/^[[:space:]]*org_id[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}' "$CONFIG_PATH" || true)"

resolve_org() {
  if [[ -n "$ORG_ID_OVERRIDE" ]]; then
    echo "$ORG_ID_OVERRIDE"; return
  fi
  if [[ -n "$current_org" && "$current_org" != "REPLACE_WITH_YOUR_ORG_UUID" ]]; then
    echo "$current_org"; return
  fi
  echo "Discovering organization UUID via /api/organizations..." >&2
  local discovered
  if ! discovered="$(COOKIE_FILE="$COOKIE_PATH" \
        python3 "$REPO_DIR/scripts/discover_org.py" --verbose 2>/dev/null)"; then
    if [[ $INTERACTIVE -eq 1 ]]; then
      echo "Auto-discovery failed. Find your UUID at"
      echo "  https://claude.ai/api/organizations (DevTools network tab)"
      read -r -p "Paste org UUID: " manual
      [[ -n "$manual" ]] || { echo "Empty." >&2; exit 6; }
      echo "$manual"; return
    fi
    echo "Auto-discovery failed and --non-interactive set." >&2; exit 6
  fi
  # Take first line; if multiple, ask user which one.
  local count
  count="$(echo "$discovered" | wc -l)"
  if [[ "$count" -gt 1 && $INTERACTIVE -eq 1 ]]; then
    echo "Multiple organizations found:" >&2
    nl -ba <<<"$discovered" >&2
    read -r -p "Pick number [1]: " pick
    pick="${pick:-1}"
    sed -n "${pick}p" <<<"$discovered"
  else
    head -n1 <<<"$discovered"
  fi
}

ORG_ID="$(resolve_org)"
[[ -n "$ORG_ID" ]] || { echo "No org UUID resolved." >&2; exit 7; }

# Patch org_id into config.ini.
python3 - "$CONFIG_PATH" "$ORG_ID" <<'PY'
import sys, re, pathlib
p = pathlib.Path(sys.argv[1]); new = sys.argv[2]
text = p.read_text()
text = re.sub(r'(?m)^(\s*org_id\s*=\s*).*$', rf'\1{new}', text)
p.write_text(text)
PY
echo "org_id = $ORG_ID"

# ---------------------------------------------------------------------------
# 4. Polling interval: pick value, render systemd units.
# ---------------------------------------------------------------------------
existing_interval=""
if [[ -f "$SYSTEMD_USER_DIR/claudometer.timer" ]]; then
  existing_interval="$(awk -F'=' '/^OnUnitActiveSec=/{print $2}' \
                        "$SYSTEMD_USER_DIR/claudometer.timer" \
                       | sed 's/min$//' | tr -d '[:space:]')"
fi
if [[ -z "$POLL_INTERVAL" ]]; then
  POLL_INTERVAL="${existing_interval:-5}"
fi
if ! [[ "$POLL_INTERVAL" =~ ^[0-9]+$ ]] || [[ "$POLL_INTERVAL" -lt 1 ]]; then
  echo "Invalid --poll-interval: $POLL_INTERVAL (must be integer >= 1)" >&2; exit 8
fi

PY_BIN="$(command -v python3)"
sed -e "s|@PYTHON@|$PY_BIN|g" -e "s|@REPO@|$REPO_DIR|g" \
    "$REPO_DIR/systemd/claudometer.service.in" \
    > "$SYSTEMD_USER_DIR/claudometer.service"
sed -e "s|@INTERVAL@|$POLL_INTERVAL|g" \
    "$REPO_DIR/systemd/claudometer.timer.in" \
    > "$SYSTEMD_USER_DIR/claudometer.timer"
echo "Installed systemd units (poll every ${POLL_INTERVAL}min)"

systemctl --user daemon-reload
systemctl --user enable --now claudometer.timer >/dev/null
systemctl --user restart claudometer.timer
echo "Timer running. Next:"
systemctl --user list-timers --no-pager 2>/dev/null | grep -E "claude|NEXT" || true

# ---------------------------------------------------------------------------
# 5. Tray autostart + immediate launch.
# ---------------------------------------------------------------------------
sed -e "s|@PYTHON@|$PY_BIN|g" -e "s|@REPO@|$REPO_DIR|g" \
    "$REPO_DIR/autostart/claudometer-tray.desktop.in" \
    > "$AUTOSTART_DIR/claudometer-tray.desktop"
chmod 644 "$AUTOSTART_DIR/claudometer-tray.desktop"
echo "Autostart entry: $AUTOSTART_DIR/claudometer-tray.desktop"

if pgrep -f "tray.py" >/dev/null; then
  echo "Restarting tray to pick up changes..."
  pkill -f "tray.py" || true
  sleep 1
fi
nohup "$PY_BIN" "$REPO_DIR/tray.py" \
    >/tmp/claudometer-tray.log 2>&1 &
disown
echo "Tray launched (logs: /tmp/claudometer-tray.log)"

# ---------------------------------------------------------------------------
# 6. Self-test: one fetch, one notification.
# ---------------------------------------------------------------------------
echo
echo "Running self-test..."
"$PY_BIN" "$REPO_DIR/monitor.py" --selftest --debug || {
  echo
  echo "Self-test failed. Check:"
  echo "  $STATE_DIR/last_response.json"
  echo "  journalctl --user -u claudometer.service -n 50"
  exit 9
}

echo
echo "Done. Useful commands:"
echo "  journalctl --user -u claudometer.service -f   # tail monitor logs"
echo "  systemctl --user list-timers | grep claude     # next poll"
echo "  tail -f /tmp/claudometer-tray.log             # tail tray logs"
echo "  ./install.sh --poll-interval 1                 # change interval"
