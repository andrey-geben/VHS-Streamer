#!/usr/bin/env bash
# Сборка образа vhs-streamer:jetson (только сборка, без запуска)
set -euo pipefail
cd "$(dirname "$0")"
if docker compose version >/dev/null 2>&1; then
    docker compose build
else
    docker-compose build
fi
echo "[OK] Образ vhs-streamer:jetson собран"
