#!/bin/bash
# Run the scanner (every N minutes) and the dashboard as macOS background services (launchd).
# They start at login and restart if they crash. TradingView Desktop must be open with
# remote debugging (port 9222) for scans to succeed; failed scans are logged and retried next tick.
#
#   scripts/service.sh install     # install + start both services
#   scripts/service.sh uninstall   # stop + remove both services
#   scripts/service.sh status
#   scripts/service.sh logs
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
AGENTS="$HOME/Library/LaunchAgents"
CLOUDFLARED="$(command -v cloudflared || echo /opt/homebrew/bin/cloudflared)"
TUNNEL_CFG="$HOME/.cloudflared/config.yml"
LABELS=(com.fxsignals.scanner com.fxsignals.dashboard com.fxsignals.telegram)
# The Cloudflare tunnel (public domain access) is included once ~/.cloudflared/config.yml exists.
[ -f "$TUNNEL_CFG" ] && LABELS+=(com.fxsignals.tunnel)

plist() {  # label, log name, program args...
  local label="$1" logname="$2"; shift 2
  local args=""
  for a in "$@"; do args+="<string>$a</string>"; done
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>$args</array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$ROOT/logs/$logname.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/$logname.err.log</string>
</dict>
</plist>
EOF
}

case "${1:-}" in
  install)
    mkdir -p "$AGENTS" "$ROOT/logs"
    plist com.fxsignals.scanner run "$PY" -u -m fxsignals run > "$AGENTS/com.fxsignals.scanner.plist"
    plist com.fxsignals.dashboard dashboard "$PY" -u -m fxsignals dashboard > "$AGENTS/com.fxsignals.dashboard.plist"
    plist com.fxsignals.telegram telegram "$PY" -u -m fxsignals telegram-bot > "$AGENTS/com.fxsignals.telegram.plist"
    if [ -f "$TUNNEL_CFG" ]; then
      plist com.fxsignals.tunnel tunnel "$CLOUDFLARED" --no-autoupdate --config "$TUNNEL_CFG" tunnel run > "$AGENTS/com.fxsignals.tunnel.plist"
    fi
    for l in "${LABELS[@]}"; do
      launchctl bootout "gui/$(id -u)/$l" 2>/dev/null || true
      # bootout returns before the job is fully gone; wait so bootstrap doesn't fail with error 5
      for _ in 1 2 3 4 5 6 7 8 9 10; do launchctl print "gui/$(id -u)/$l" >/dev/null 2>&1 || break; sleep 0.5; done
      launchctl bootstrap "gui/$(id -u)" "$AGENTS/$l.plist"
    done
    echo "Installed. Dashboard: http://forex.localhost:8765  Logs: $ROOT/logs/"
    ;;
  uninstall)
    for l in "${LABELS[@]}"; do
      launchctl bootout "gui/$(id -u)/$l" 2>/dev/null || true
      rm -f "$AGENTS/$l.plist"
    done
    echo "Removed."
    ;;
  status)
    for l in "${LABELS[@]}"; do launchctl print "gui/$(id -u)/$l" 2>/dev/null | grep -E "^\s*(state|pid|last exit code)" | sed "s/^/$l: /" || echo "$l: not installed"; done
    ;;
  logs)
    tail -n 40 "$ROOT/logs/run.out.log" "$ROOT/logs/run.err.log" 2>/dev/null
    ;;
  *) echo "usage: $0 install|uninstall|status|logs"; exit 1 ;;
esac
