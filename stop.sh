#!/usr/bin/env bash
# VHS-Streamer — остановка (Linux)
cd "$(dirname "$0")"

if [ -f run.pid ]; then
    PID=$(cat run.pid)
    if kill -0 "$PID" 2>/dev/null; then
        echo "[*] Останавливаю PID=$PID..."
        kill -TERM "$PID"
        sleep 3
        kill -KILL "$PID" 2>/dev/null || true
    fi
    rm -f run.pid
fi

# Дочерние процессы
pkill -f 'bin/mediamtx' 2>/dev/null || true
pkill -f 'bin/ffmpeg' 2>/dev/null || true

echo "[OK] Остановлено."