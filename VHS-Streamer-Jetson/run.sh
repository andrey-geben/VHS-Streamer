#!/usr/bin/env bash
# =====================================================================
#  VHS-Streamer на Jetson через Docker — сборка и запуск
# ---------------------------------------------------------------------
#  Что делает:
#   1. проверяет docker;
#   2. собирает образ vhs-streamer:jetson;
#   3. поднимает контейнер (network_mode: host);
#   4. печатает адрес панели и подсказку про пароль.
#
#  Запуск:   ./run.sh            (сборка + запуск)
#            ./run.sh --no-build (только запуск)
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"

BUILD=1
VARIANT="ubuntu"
for arg in "$@"; do
    case "$arg" in
        --no-build) BUILD=0 ;;
        --bullseye) VARIANT="bullseye" ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    esac
done

if ! command -v docker >/dev/null 2>&1; then
    echo "[!] Docker не установлен. На Jetson:"
    echo "    sudo apt install docker.io docker-compose-v2"
    echo "    sudo usermod -aG docker \$USER   # и перелогиниться"
    exit 1
fi

# docker compose (v2) или docker-compose (v1)
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    echo "[!] Не найден docker compose. Установите: sudo apt install docker-compose-v2"
    exit 1
fi

if [ "$BUILD" = "1" ]; then
    if [ "$VARIANT" = "bullseye" ]; then
        echo "[*] Собираю образ на Debian 11 (для ядра 4.9 / JetPack 4)..."
        docker build -f Dockerfile.bullseye -t vhs-streamer:jetson ..
    else
        echo "[*] Собираю образ vhs-streamer:jetson (arm64)..."
        $DC build
    fi
fi

echo "[*] Запускаю контейнер..."
$DC up -d

sleep 3
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "============================================"
echo "  VHS-Streamer запущен в контейнере"
echo "============================================"
echo "  Панель:  http://${IP:-<ip-jetson>}:8000"
echo "  Логи:    $DC logs -f"
echo "  Пароль:  $DC logs | grep -A3 'ПЕРВЫЙ ЗАПУСК'"
echo "  Остановка: $DC down"
echo
