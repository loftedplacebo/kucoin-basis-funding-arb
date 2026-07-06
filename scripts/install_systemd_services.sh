#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${KUCOIN_BASIS_USER:-$(id -un)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8766}"

cd "$APP_DIR"

"$PYTHON_BIN" -m venv .venv
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -r requirements.txt

write_service() {
  local name="$1"
  local description="$2"
  local command="$3"

  sudo tee "/etc/systemd/system/${name}.service" >/dev/null <<SERVICE
[Unit]
Description=${description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/python ${command}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE
}

write_service \
  "kucoin-basis-scanner" \
  "KuCoin basis opportunity scanner" \
  "kucoin_basis/run_scanner.py --loop --interval 60"

write_service \
  "kucoin-basis-strategy" \
  "KuCoin basis paper strategy" \
  "kucoin_basis/run_paper_strategy.py --loop --interval 60"

write_service \
  "kucoin-basis-dashboard" \
  "KuCoin basis local dashboard" \
  "kucoin_basis/run_funding_dashboard.py --host ${DASHBOARD_HOST} --port ${DASHBOARD_PORT}"

sudo systemctl daemon-reload
sudo systemctl enable kucoin-basis-scanner kucoin-basis-strategy kucoin-basis-dashboard
sudo systemctl restart kucoin-basis-scanner kucoin-basis-strategy kucoin-basis-dashboard

sudo systemctl --no-pager --full status \
  kucoin-basis-scanner \
  kucoin-basis-strategy \
  kucoin-basis-dashboard

echo
echo "Dashboard listens on ${DASHBOARD_HOST}:${DASHBOARD_PORT} on the VPS."
echo "From your laptop, tunnel it with:"
echo "  ssh -L ${DASHBOARD_PORT}:127.0.0.1:${DASHBOARD_PORT} ${APP_USER}@YOUR_VPS_HOST"
