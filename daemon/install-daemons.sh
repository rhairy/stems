#!/usr/bin/env bash
# Install the stems web server and worker as launchd login agents for your user.
# They start when you log in and are meant for local use only: http://localhost:8765
#
#   ./daemon/install-daemons.sh               install (or reinstall) and start
#   ./daemon/install-daemons.sh --uninstall   stop and remove
#
# Options: --out DIR (default ~/Music/Stems), --port N (default 8765)
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

UNINSTALL=0 OUT="$HOME/Music/Stems" PORT=8765
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) UNINSTALL=1 ;;
    --out) OUT="$2"; shift ;;
    --port) PORT="$2"; shift ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
  shift
done

[ "$(id -u)" != 0 ] || { echo "Login agents are per-user: run this without sudo."; exit 1; }
DEST="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

for svc in web worker; do
  launchctl bootout "$DOMAIN/com.stems.$svc" 2>/dev/null || true
done
if [ "$UNINSTALL" = 1 ]; then
  rm -f "$DEST/com.stems.web.plist" "$DEST/com.stems.worker.plist"
  echo "Removed com.stems.web and com.stems.worker."
  exit 0
fi

[ -x "$REPO/.venv/bin/stemsd" ] || { echo "Run ./install.sh first."; exit 1; }

LOGDIR="$HOME/Library/Logs/stems"
mkdir -p "$LOGDIR" "$DEST" "$OUT"
: > "$LOGDIR/web.log"; : > "$LOGDIR/worker.log"

for svc in web worker; do
  PLIST="$DEST/com.stems.$svc.plist"
  sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" -e "s|__USER__|$(id -un)|g" \
      -e "s|__OUT__|$OUT|g" -e "s|__PUBLIC_URL__|http://localhost:$PORT|g" -e "s|__PORT__|$PORT|g" \
      "$REPO/daemon/com.stems.$svc.plist.in" > "$PLIST"
  chmod 644 "$PLIST"
  plutil -lint "$PLIST" >/dev/null
  launchctl bootstrap "$DOMAIN" "$PLIST"
done

echo "Installed login agents, writing to $OUT."
echo "  Open:  http://localhost:$PORT/"
echo "  Logs:  $LOGDIR/{web,worker}.log"
sleep 3
if curl -fsS "http://localhost:$PORT/api/health" >/dev/null 2>&1; then
  echo "Web server is up."
else
  echo "Web server isn't answering yet; check $LOGDIR/web.log"
fi
