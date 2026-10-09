#!/bin/sh
# Install a systemd USER unit so Spark Duo comes back after a reboot.
# Not enabled automatically — run this when you want it persisted.
set -eu
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/spark-duo.service" <<UNIT
[Unit]
Description=Spark Duo (Jev gate + StepFun GELab-Zero-4B VLM on the DGX Spark)
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=%h/spark-duo
Environment=JEV_SCORE_BIN=%h/spark-duo/bin/jev-score
ExecStart=%h/spark-duo/scripts/04_serve.sh
ExecStop=/bin/sh -c 'kill \$(cat %h/spark-duo/logs/orchestrator.pid %h/spark-duo/logs/vlm.pid 2>/dev/null) 2>/dev/null || true'
TimeoutStartSec=600

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
echo "unit written: $UNIT_DIR/spark-duo.service"
echo "enable with:  systemctl --user enable --now spark-duo.service"
echo "keep it running after logout: loginctl enable-linger $USER"
