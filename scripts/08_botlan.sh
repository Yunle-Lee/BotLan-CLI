#!/bin/sh
# One step from "Spark Duo is serving" to "BotLan can drive this Spark".
#
#   scripts/08_botlan.sh            start Spark Duo if needed, start the BotLan gateway, print what
#                                   to paste into the BotLan panel
#   scripts/08_botlan.sh --install  same, plus systemd USER units so all of it survives a reboot
#   scripts/08_botlan.sh --stop     stop the gateway (models keep running)
#
# Nothing here needs root. The gateway binds 127.0.0.1 only; the laptop reaches it over SSH.
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)
LOGS=${LOGS:-$HERE/logs}
GPORT=${GPORT:-8091}
KEYFILE=${KEYFILE:-$HOME/.spark-duo/botlan.key}
PIDFILE=$LOGS/botlan-gateway.pid
mkdir -p "$LOGS"

stop_gateway() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" && echo "gateway: stopped pid $(cat "$PIDFILE")"
  fi
  rm -f "$PIDFILE"
}

case "${1:-}" in
  --stop) stop_gateway; exit 0 ;;
esac

# 1. the two models (idempotent: 04 skips whatever is already running)
if ! curl -sf "http://127.0.0.1:${OPORT:-8090}/health" >/dev/null 2>&1; then
  echo "spark duo: not up, starting it (scripts/04_serve.sh)"
  "$HERE/scripts/04_serve.sh"
fi

# 2. the gateway
if [ "${1:-}" = "--install" ]; then
  stop_gateway        # systemd owns it from here
  UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
  mkdir -p "$UNIT_DIR"
  "$HERE/scripts/05_autostart.sh" >/dev/null
  cat > "$UNIT_DIR/botlan-gateway.service" <<UNIT
[Unit]
Description=BotLan gateway (Jev skill router + GELab-Zero-4B, approved command execution)
After=spark-duo.service

[Service]
WorkingDirectory=$HERE
ExecStart=/usr/bin/env python3 $HERE/botlan_gateway.py --port $GPORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable spark-duo.service >/dev/null 2>&1 || true
  systemctl --user enable --now botlan-gateway.service
  echo "systemd: spark-duo + botlan-gateway enabled (user units)"
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    echo "note: units start at login only. To start at boot without a login run once:"
    echo "      sudo loginctl enable-linger $USER"
  fi
elif ! curl -s -o /dev/null "http://127.0.0.1:$GPORT/health" 2>/dev/null; then
  nohup python3 "$HERE/botlan_gateway.py" --port "$GPORT" > "$LOGS/botlan-gateway.log" 2>&1 &
  echo $! > "$PIDFILE"
  echo "gateway: pid $(cat "$PIDFILE") on 127.0.0.1:$GPORT"
fi

i=0
until [ -f "$KEYFILE" ] && curl -sf -H "Authorization: Bearer $(cat "$KEYFILE")" \
        "http://127.0.0.1:$GPORT/health" >/dev/null 2>&1; do
  i=$((i+1)); [ $i -gt 30 ] && { echo "gateway did not come up - see $LOGS/botlan-gateway.log" >&2; exit 1; }
  sleep 1
done

HOSTNAME_=$(hostname)
cat <<EOF

BotLan gateway is ready on $HOSTNAME_.
  1. On the laptop, keep a tunnel open:
       ssh -N -L $GPORT:127.0.0.1:$GPORT $USER@<this-spark>
  2. In BotLan: Add Bot ->
       Base URL  http://127.0.0.1:$GPORT/v1
       Model     jev-step
       API Key   (contents of $KEYFILE - print it with: cat $KEYFILE)
EOF
