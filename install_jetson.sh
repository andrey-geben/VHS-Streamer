#!/usr/bin/env bash
# =====================================================================
#  VHS-Streamer — установка на NVIDIA Jetson (Axiomtek AIE100 и подобные)
# ---------------------------------------------------------------------
#  Что делает:
#    1. проверяет архитектуру и версию Python;
#    2. для JetPack 4.6 / Python 3.6 собирает отдельную runtime-копию
#       и понижает синтаксис через downgrade_for_py36.py;
#    3. ставит системные пакеты (ffmpeg, OpenCV для Python, venv);
#    4. создаёт .venv и ставит совместимые зависимости;
#    5. готовит bin/ (MediaMTX arm64 + ffmpeg);
#    6. создаёт config.yaml из примера;
#    7. по желанию ставит systemd-службу.
#
#  Запуск:
#      chmod +x install_jetson.sh
#      ./install_jetson.sh                  # без автозапуска
#      ./install_jetson.sh --service        # + systemd
#      ./install_jetson.sh --open-ports     # открыть порты в ufw
#      ./install_jetson.sh --service --open-ports
# =====================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WITH_SERVICE=0
OPEN_PORTS=0
FORCE_LEGACY=0

for arg in "$@"; do
    case "$arg" in
        --service) WITH_SERVICE=1 ;;
        --open-ports) OPEN_PORTS=1 ;;
        --legacy) FORCE_LEGACY=1 ;;
        -h|--help)
            sed -n '2,26p' "$0"; exit 0 ;;
    esac
done

echo
echo "============================================"
echo "  VHS-Streamer — установка на Jetson"
echo "============================================"
echo

# ---------------------------------------------------------------
# 1. Проверки
# ---------------------------------------------------------------
ARCH=$(uname -m)
echo "[1/7] Платформа: $ARCH"
if [ "$ARCH" != "aarch64" ] && [ "$ARCH" != "arm64" ]; then
    echo "[!] Скрипт рассчитан на ARM64 (Jetson). Обнаружено: $ARCH"
    echo "    Для x86 используйте install.sh или Windows-сборку."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "[!] Нет python3. Установите: sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "    Python $PYVER"

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 6) else 1)'; then
    echo "[!] Нужен Python 3.6 или новее (сейчас $PYVER)."
    exit 1
fi

if [ -f /etc/nv_tegra_release ]; then
    echo "    JetPack: $(head -n1 /etc/nv_tegra_release)"
fi

LEGACY=0
if [ "$FORCE_LEGACY" = "1" ]; then
    LEGACY=1
    echo "    Принудительный режим Python 3.6 (--legacy)."
fi
if [ "$LEGACY" = "0" ] && ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    LEGACY=1
    echo
    echo "    Python < 3.10: включаю режим JetPack 4.x / Python 3.6."
    echo "    Будет создана отдельная копия: runtime/py36"
fi

# ---------------------------------------------------------------
# 2. Системные пакеты
# ---------------------------------------------------------------
echo
echo "[2/7] Системные пакеты (ffmpeg, OpenCV, venv)..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3-venv python3-pip python3-dev \
    ffmpeg \
    python3-opencv python3-yaml python3-psutil \
    libgl1-mesa-glx libglib2.0-0 libgomp1 \
    ufw curl tar xz-utils
echo "    Готово."

# ---------------------------------------------------------------
# 3. Runtime-копия для Python 3.6
# ---------------------------------------------------------------
APP_DIR="$ROOT"
REQ_FILE="requirements-linux.txt"
if [ "$LEGACY" = "1" ]; then
    APP_DIR="$ROOT/runtime/py36"
    if [ -d "$APP_DIR" ]; then
        echo
        echo "[3/7] Runtime Python 3.6 уже существует:"
        echo "    $APP_DIR"
        echo "    Для обновления кода удалите runtime/py36 и запустите установщик снова."
    else
        echo
        echo "[3/7] Создаю runtime-копию для Python 3.6..."
        mkdir -p "$APP_DIR"
        tar -C "$ROOT" \
            --exclude='./.git' \
            --exclude='./.venv' \
            --exclude='./runtime' \
            --exclude='./archive' \
            --exclude='./logs' \
            --exclude='./__pycache__' \
            --exclude='./.pytest_cache' \
            --exclude='./dist' \
            --exclude='./dist_*' \
            --exclude='./build' \
            --exclude='./build_vhs' \
            --exclude='./events.db' \
            --exclude='./config.yaml' \
            --exclude='./*.bak-*' \
            -cf - . | tar -C "$APP_DIR" -xf -
        echo "    Копия создана."
    fi

    echo "    Понижаю синтаксис до Python 3.6..."
    python3 "$APP_DIR/downgrade_for_py36.py" "$APP_DIR"

    echo "    Проверяю синтаксис Python 3.6..."
    find "$APP_DIR" -maxdepth 2 -name '*.py' -not -path '*/.venv/*' -print0 \
        | xargs -0 -n1 python3 -m py_compile
    echo "    Синтаксис OK."

    REQ_FILE="requirements-py36.txt"
else
    echo
    echo "[3/7] Python 3.10+: использую основной каталог проекта."
fi

cd "$APP_DIR"

# ---------------------------------------------------------------
# 4. Виртуальное окружение и зависимости
# ---------------------------------------------------------------
echo
echo "[4/7] Виртуальное окружение и зависимости..."
if [ ! -x ".venv/bin/python" ]; then
    # --system-site-packages нужен, чтобы был доступен python3-opencv (cv2)
    python3 -m venv --system-site-packages .venv
    echo "    .venv создано."
else
    echo "    .venv уже существует — использую его."
fi
VENV_PY=".venv/bin/python"

if [ "$LEGACY" = "1" ]; then
    "$VENV_PY" -m pip install -q --upgrade "pip<22" "setuptools<60" "wheel<0.38"
else
    "$VENV_PY" -m pip install -q --upgrade pip
fi
PIP_EXTRA=()
if [ -d "$ROOT/wheels/py36" ]; then
    PIP_EXTRA+=(--find-links "$ROOT/wheels/py36")
    echo "    Использую локальный wheelhouse: wheels/py36"
fi
"$VENV_PY" -m pip install -q "${PIP_EXTRA[@]}" -r "$REQ_FILE"

"$VENV_PY" - <<'PY'
import cv2
import flask
import yaml
print("    OpenCV:", cv2.__version__)
print("    Flask:", flask.__version__)
print("    PyYAML:", yaml.__version__)
PY

# ---------------------------------------------------------------
# 5. bin/ — MediaMTX (arm64) и ffmpeg
# ---------------------------------------------------------------
echo
echo "[5/7] Компоненты bin/ (MediaMTX + FFmpeg)..."
mkdir -p bin
chmod +x bin/mediamtx bin/ffmpeg bin/ffprobe 2>/dev/null || true
if [ ! -x "bin/mediamtx" ]; then
    echo "    Скачиваю MediaMTX для arm64 с GitHub..."
    "$VENV_PY" - <<'PY'
from bootstrap import ensure_all
result = ensure_all()
print("    bootstrap:", result)
PY
else
    echo "    mediamtx уже есть."
fi

if [ ! -x "bin/ffmpeg" ]; then
    ln -sf "$(command -v ffmpeg)" bin/ffmpeg
    echo "    bin/ffmpeg -> $(command -v ffmpeg)"
fi
if [ ! -x "bin/ffprobe" ] && command -v ffprobe >/dev/null 2>&1; then
    ln -sf "$(command -v ffprobe)" bin/ffprobe
fi

# ---------------------------------------------------------------
# 6. Конфигурация
# ---------------------------------------------------------------
echo
echo "[6/7] Конфигурация..."
if [ -f "config.yaml" ]; then
    echo "    config.yaml уже существует — не трогаю."
else
    if [ -f "$ROOT/config.yaml" ]; then
        cp "$ROOT/config.yaml" config.yaml
        echo "    Скопирован config.yaml из корня проекта."
    else
        cp config.example.yaml config.yaml
        echo "    Создан config.yaml из примера."
    fi
    echo "    При первом запуске приложение сгенерирует пароль администратора"
    echo "    и покажет его в консоли."
fi

# ---------------------------------------------------------------
# 7. Служба systemd и порты
# ---------------------------------------------------------------
echo
echo "[7/7] Служба и порты..."
if [ "$WITH_SERVICE" = "1" ]; then
    UNIT=/etc/systemd/system/vhs-streamer.service
    sudo tee "$UNIT" >/dev/null <<UNIT_EOF
[Unit]
Description=VHS-Streamer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/app.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
User=$USER

[Install]
WantedBy=multi-user.target
UNIT_EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now vhs-streamer
    echo "    Служба установлена и запущена (systemctl status vhs-streamer)."
else
    echo "    Автозапуск не включён (запустите с --service, если нужно)."
fi

if [ "$OPEN_PORTS" = "1" ]; then
    sudo ufw allow 8100/tcp  >/dev/null 2>&1 || true
    sudo ufw allow 8081/tcp  >/dev/null 2>&1 || true
    sudo ufw allow 8554/tcp  >/dev/null 2>&1 || true
    sudo ufw allow 8889/tcp  >/dev/null 2>&1 || true
    sudo ufw allow 8189/udp  >/dev/null 2>&1 || true
    sudo ufw allow 8080/tcp  >/dev/null 2>&1 || true
    echo "    Порты открыты в ufw (только если ufw активен)."
else
    echo "    Порты не открывались. Для локальной сети: --open-ports."
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "============================================"
echo "  Установка завершена"
echo "============================================"
echo "  Каталог:  $APP_DIR"
echo "  Панель:   http://${IP:-<ip-машины>}:8100"
echo "  Запуск:   cd $APP_DIR && ./start.sh"
echo "  Логи:     $APP_DIR/logs/vhs-streamer.log"
echo "  Логин:    смотрите вывод первого запуска (генерируется автоматически)"
echo

