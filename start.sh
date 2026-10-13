#!/usr/bin/env bash
# VHS-Streamer — запуск сервера (Linux)
set -e
cd "$(dirname "$0")"

if [ -f run.pid ]; then
    OLD_PID=$(cat run.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[!] Сервер уже запущен (PID=$OLD_PID)."
        echo "    Сначала: ./stop.sh"
        exit 1
    fi
    rm -f run.pid
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "[!] Не найдено .venv. Создайте: python3 -m venv .venv"
    exit 1
fi

echo "[*] Запускаю VHS-Streamer..."
nohup .venv/bin/python app.py > logs/console.log 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > run.pid

echo "[OK] Сервер запущен. PID=$NEW_PID"
echo "     Web UI:  http://localhost:8000"
echo "     Логи:    logs/vhs-streamer.log"
echo "     Стоп:    ./stop.sh"