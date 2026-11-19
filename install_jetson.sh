#!/usr/bin/env bash
# =====================================================================
#  VHS-Streamer — установка на NVIDIA Jetson (Axiomtek AIE100 и подобные)
# ---------------------------------------------------------------------
#  Что делает:
#    1. проверяет архитектуру (aarch64) и версию Python;
#    2. ставит системные пакеты (ffmpeg, OpenCV для Python, venv);
#    3. создаёт .venv и ставит зависимости;
#    4. готовит bin/ (MediaMTX arm64 + ffmpeg), если их нет;
#    5. создаёт config.yaml из примера;
#    6. по желанию ставит systemd-службу (автозапуск);
#    7. открывает порты в ufw.
#
#  Запуск:
#      chmod +x install_jetson.sh
#      ./install_jetson.sh              # без автозапуска
#      ./install_jetson.sh --service    # + systemd (автозапуск)
# =====================================================================
set -euo pipefail

cd "$(dirname "$0")"

WITH_SERVICE=0
for arg in "$@"; do
    case "$arg" in
        --service) WITH_SERVICE=1 ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
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
if [ "$(printf '%s\n' "3.10" "$PYVER" | sort -V | head -n1)" != "3.10" ]; then
    echo "[!] Нужен Python 3.10 или новее (сейчас $PYVER)."
    echo "    На JetPack 5 (Ubuntu 20.04) стоит 3.8 — обновитесь до JetPack 6"
    echo "    (Ubuntu 22.04, Python 3.10) или используйте Docker-вариант."
    exit 1
fi

if [ -f /etc/nv_tegra_release ]; then
    echo "    JetPack: $(head -n1 /etc/nv_tegra_release)"
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
    python3-opencv \
    libgl1 libglib2.0-0 libgomp1 \
    ufw curl tar xz-utils
echo "    Готово."

# ---------------------------------------------------------------
# 3. Виртуальное окружение
# ---------------------------------------------------------------
echo
echo "[3/7] Виртуальное окружение .venv..."
if [ -x ".venv/bin/python" ]; then
    echo "    Уже существует — пропускаю."
else
    # --system-site-packages нужен, чтобы был доступен python3-opencv (cv2)
    python3 -m venv --system-site-packages .venv
    echo "    Создано."
fi
VENV_PY=".venv/bin/python"

# ---------------------------------------------------------------
# 4. Зависимости Python
# ---------------------------------------------------------------
echo
echo "[4/7] Зависимости Python..."
"$VENV_PY" -m pip install --upgrade pip -q
# На ARM opencv берём из apt (python3-opencv), поэтому без opencv-python
"$VENV_PY" -m pip install -q -r requirements-linux.txt
"$VENV_PY" - <<'PY'
try:
    import cv2
    print("    OpenCV:", cv2.__version__)
except Exception as exc:
    print("    [!] OpenCV не найден:", exc)
    raise SystemExit(1)
PY

# ---------------------------------------------------------------
# 5. bin/ — MediaMTX (arm64) и ffmpeg
# ---------------------------------------------------------------
echo
echo "[5/7] Компоненты bin/ (MediaMTX + FFmpeg)..."
mkdir -p bin
if [ -x "bin/mediamtx" ]; then
    echo "    mediamtx уже есть."
else
    echo "    Скачиваю MediaMTX для arm64 с GitHub..."
    "$VENV_PY" - <<'PY'
from bootstrap import ensure_all
result = ensure_all()
print("    bootstrap:", result)
PY
fi
# ffmpeg из apt — кладём ссылку в bin/, чтобы приложение нашло его рядом
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
    cp config.example.yaml config.yaml
    echo "    Создан config.yaml из примера."
    echo "    При первом запуске приложение сгенерирует пароль администратора"
    echo "    и покажет его в консоли."
fi

# ---------------------------------------------------------------
# 7. Служба systemd и порты
# ---------------------------------------------------------------
echo
echo "[7/7] Служба и порты..."
if [ "$WITH_SERVICE" = "1" ]; then
    APP_DIR="$(pwd)"
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

sudo ufw allow 8000/tcp  >/dev/null 2>&1 || true
sudo ufw allow 8081/tcp  >/dev/null 2>&1 || true
sudo ufw allow 8554/tcp  >/dev/null 2>&1 || true
sudo ufw allow 8889/tcp  >/dev/null 2>&1 || true
sudo ufw allow 8189/udp  >/dev/null 2>&1 || true
sudo ufw allow 8080/tcp  >/dev/null 2>&1 || true
echo "    Порты 8000/8081/8554/8889/8189udp/8080 разрешены (если ufw активен)."

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "============================================"
echo "  Установка завершена"
echo "============================================"
echo "  Панель:   http://${IP:-<ip-машины>}:8000"
echo "  Запуск:   ./start.sh          (или systemctl start vhs-streamer)"
echo "  Логи:     logs/vhs-streamer.log"
echo "  Логин:    смотрите вывод первого запуска (генерируется автоматически)"
echo
