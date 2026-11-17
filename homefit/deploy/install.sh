#!/bin/bash
# HomeFit 상시 실행 등록 (사용자가 직접 실행). 먼저 Claude 세션의 임시 프로세스를 멈춘다.
set -euo pipefail
cd "$(dirname "$0")"
pkill -f "server.py --port 8765" || true; pkill -f "server.py --host 0.0.0.0 --port 8765" || true
pkill -f "valkey-server --bind 127.0.0.1 --port 6379" || true
sleep 1
sudo cp homefit-valkey.service homefit.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homefit-valkey.service homefit.service
systemctl --no-pager status homefit.service | head -5
