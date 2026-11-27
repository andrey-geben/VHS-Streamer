#!/usr/bin/env bash
# =====================================================================
#  VHS-Streamer — перенос установки на другой ARM по SSH
# ---------------------------------------------------------------------
#  Запускается с флешки/рабочей машины, где есть ssh и tar.
#
#  Примеры:
#    ./migrate_to_arm.sh user@192.168.1.50
#    ./migrate_to_arm.sh user@new-arm --data-from /opt/vhs-streamer
#    ./migrate_to_arm.sh user@new-arm --data-from /opt/vhs-streamer \
#        --stop-source --open-ports
#
#  Требования:
#    * SSH-ключ и доступ без пароля (BatchMode);
#    * sudo без пароля (NOPASSWD) для установки пакетов и systemd;
#    * на новом ARM — Ubuntu/JetPack, aarch64.
#
#  Скрипт НЕ передаёт пароли и не хранит SSH-секреты.
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET=""
DATA_FROM=""
REMOTE_DIR="/opt/vhs-streamer"
PORT=8100
SERVICE_ARGS="--service"
STOP_SOURCE=0
DRY_RUN=0

usage() {
    sed -n '2,24p' "$0"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --data-from) DATA_FROM="${2:-}"; shift 2 ;;
        --remote-dir) REMOTE_DIR="${2:-}"; shift 2 ;;
        --port) PORT="${2:-}"; shift 2 ;;
        --service-args) SERVICE_ARGS="${2:-}"; shift 2 ;;
        --stop-source) STOP_SOURCE=1; shift ;;
        --open-ports) SERVICE_ARGS="$SERVICE_ARGS --open-ports"; shift ;;
        -h|--help) usage; exit 0 ;;
        -*)
            echo "Неизвестный параметр: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [ -z "$TARGET" ]; then TARGET="$1"; shift; else
                echo "Лишний аргумент: $1" >&2; exit 2
            fi
            ;;
    esac
done

if [ -z "$TARGET" ]; then
    echo "Укажите цель: user@host" >&2
    usage >&2
    exit 2
fi

for required in install_jetson.sh requirements-py36.txt bin/mediamtx bin/ffmpeg; do
    if [ ! -e "$SCRIPT_DIR/$required" ]; then
        echo "[!] В комплекте нет: $required" >&2
        exit 1
    fi
done

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)
remote() {
    ssh "${SSH_OPTS[@]}" "$TARGET" "$@"
}
say() {
    if [ "$DRY_RUN" = "1" ]; then
        printf '[dry-run] %s\n' "$*"
    else
        printf '%s\n' "$*"
    fi
}

echo "== VHS-Streamer: перенос на $TARGET =="
if [ "$DRY_RUN" = "1" ]; then
    echo "== РЕЖИМ DRY-RUN: команды только печатаются =="
fi

# 1. Связь и sudo
say "[1/7] Проверяю SSH..."
remote true
REMOTE_USER="$(remote 'id -un')"
echo "    Пользователь на цели: $REMOTE_USER"
if ! remote 'sudo -n true' >/dev/null 2>&1; then
    echo "[!] На цели нужен sudo без пароля (NOPASSWD) для установки." >&2
    echo "    Настройте sudoers или запустите установку под root." >&2
    exit 1
fi

# 2. Каталоги
say "[2/7] Готовлю каталоги..."
remote "sudo mkdir -p '$REMOTE_DIR' '/tmp/vhs-streamer-stage' && sudo chown -R '$REMOTE_USER':'$REMOTE_USER' '$REMOTE_DIR' '/tmp/vhs-streamer-stage'"

# 3. Передача комплекта
say "[3/7] Передаю комплект..."
if [ "$DRY_RUN" = "1" ]; then
    say "tar -C '$SCRIPT_DIR' -czf - . | ssh $TARGET 'tar -C /tmp/vhs-streamer-stage -xzf -'"
else
    tar -C "$SCRIPT_DIR" \
        --exclude='./SHA256SUMS' \
        --exclude='./VHS-Streamer-ARM-USB.zip' \
        -czf - . | remote "tar -C '/tmp/vhs-streamer-stage' -xzf -"
    remote "cp -a '/tmp/vhs-streamer-stage/.' '$REMOTE_DIR/'"
fi

# 4. Установка
say "[4/7] Запускаю установщик на цели..."
remote "cd '$REMOTE_DIR' && chmod +x install_jetson.sh start.sh stop.sh && ./install_jetson.sh $SERVICE_ARGS"

# 5. Данные
if [ -n "$DATA_FROM" ]; then
    if [ ! -d "$DATA_FROM" ]; then
        echo "[!] Нет каталога данных: $DATA_FROM" >&2
        exit 1
    fi
    if [ "$STOP_SOURCE" = "1" ]; then
        say "[5/7] Останавливаю источник..."
        sudo systemctl stop vhs-streamer >/dev/null 2>&1 || true
    fi

    data_items=()
    for item in config.yaml archive events.db; do
        [ -e "$DATA_FROM/$item" ] && data_items+=("$item")
    done
    if [ "${#data_items[@]}" -eq 0 ]; then
        echo "[!] В $DATA_FROM нет config.yaml/archive/events.db" >&2
        exit 1
    fi

    say "[5/7] Переношу данные: ${data_items[*]}"
    remote "sudo systemctl stop vhs-streamer >/dev/null 2>&1 || true"
    remote_data="$(remote "if [ -d '$REMOTE_DIR/runtime/py36' ]; then echo '$REMOTE_DIR/runtime/py36'; else echo '$REMOTE_DIR'; fi")"
    echo "    Каталог данных на цели: $remote_data"
    if [ "$DRY_RUN" = "1" ]; then
        say "tar -C '$DATA_FROM' -czf - ${data_items[*]} | ssh $TARGET 'tar -C $remote_data -xzf -'"
    else
        tar -C "$DATA_FROM" -czf - "${data_items[@]}" | remote "tar -C '$remote_data' -xzf -"
        remote "sudo systemctl start vhs-streamer"
    fi
else
    say "[5/7] Данные не указаны (--data-from). Новый экземпляр стартует с чистым конфигом."
fi

# 6. Проверка
say "[6/7] Проверяю службу..."
if [ "$DRY_RUN" != "1" ]; then
    remote "systemctl is-active --quiet vhs-streamer"
    remote "curl -fsS --max-time 5 'http://127.0.0.1:$PORT/' >/dev/null 2>&1 || true"
    echo "    Служба активна."
fi

echo
echo "== Готово =="
echo "  Панель: http://<ip-цели>:$PORT"
echo "  Логи:   ssh $TARGET 'sudo journalctl -u vhs-streamer -n 100 --no-pager'"
echo "  Старый экземпляр: остановите вручную, если он ещё работает."

