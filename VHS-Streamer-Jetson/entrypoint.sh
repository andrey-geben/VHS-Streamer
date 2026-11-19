#!/bin/bash
# =====================================================================
#  Скрипт запуска VHS-Streamer в контейнере на Jetson
# ---------------------------------------------------------------------
#  1. Проверяет Python/FFmpeg и доступные аппаратные кодеки.
#  2. Готовит config.yaml и bin/ (MediaMTX arm64, ссылки на ffmpeg).
#  3. Запускает приложение.
# =====================================================================
set -e

echo "=============================================="
echo "  VHS-Streamer на Jetson (Docker)"
echo "=============================================="
echo

# ---------- Python ----------
echo "[*] Python: $(python3 --version 2>&1)"
python3 -c 'import cv2; print("[*] OpenCV:", cv2.__version__)' || {
    echo "[!] OpenCV не найден. Установите: apt install python3-opencv"
    exit 1
}

# ---------- Аппаратные кодеки ----------
echo "[*] Проверяю H264-кодеки FFmpeg..."
JETSON_ENCODER="libx264"
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "nvv4l2h264enc"; then
    JETSON_ENCODER="nvv4l2h264enc"
elif ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "h264_nvenc"; then
    JETSON_ENCODER="h264_nvenc"
elif ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "h264_v4l2m2m"; then
    JETSON_ENCODER="h264_v4l2m2m"
fi
echo "    Использую: $JETSON_ENCODER"
export VHS_JETSON_ENCODER="$JETSON_ENCODER"

# ---------- config.yaml ----------
if [ ! -f /app/config.yaml ]; then
    echo "[*] Создаю config.yaml из примера"
    cp /app/config.example.yaml /app/config.yaml
    echo "    Пароль администратора будет сгенерирован при первом запуске."
fi

# ---------- ffmpeg рядом с программой ----------
mkdir -p /app/bin
[ -x /app/bin/ffmpeg ]  || ln -sf "$(command -v ffmpeg)"  /app/bin/ffmpeg
[ -x /app/bin/ffprobe ] || ln -sf "$(command -v ffprobe)" /app/bin/ffprobe

# ---------- MediaMTX (arm64) ----------
if [ ! -x /app/bin/mediamtx ]; then
    echo "[*] MediaMTX не найден — скачиваю arm64-сборку..."
    cd /app
    python3 - <<'PY'
from bootstrap import ensure_all
print("    bootstrap:", ensure_all())
PY
fi

if [ ! -x /app/bin/mediamtx ]; then
    echo "[!] MediaMTX не установлен. Без интернета положите bin/mediamtx вручную:"
    echo "    https://github.com/bluenviron/mediamtx/releases (linux_arm64v8)"
    exit 1
fi

echo
echo "[*] Запускаю VHS-Streamer..."
echo "    Панель: http://0.0.0.0:8000"
echo "    RTSP:   rtsp://0.0.0.0:8554"
echo "    WebRTC: http://0.0.0.0:8889"
echo

cd /app
exec python3 app.py
