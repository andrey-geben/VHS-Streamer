#!/bin/bash
# =====================================================================
#  Скрипт запуска VHS-Streamer на Jetson
# =====================================================================
set -e

echo "=============================================="
echo "  VHS-Streamer на Jetson"
echo "=============================================="
echo ""

# ---------- Проверка Python ----------
echo "[*] Проверяю Python..."
python3 --version
echo ""

# ---------- Проверка доступных аппаратных кодеков ----------
echo "[*] Проверяю доступные H264-кодеки FFmpeg..."

# Проверяем наличие nvv4l2h264enc (специфичный для Jetson)
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "nvv4l2h264enc"; then
    JETSON_ENCODER="nvv4l2h264enc"
    echo "    Найден Jetson-энкодер: $JETSON_ENCODER"
elif ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "h264_nvenc"; then
    JETSON_ENCODER="h264_nvenc"
    echo "    Найден NVENC-энкодер: $JETSON_ENCODER"
elif ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "h264_v4l2m2m"; then
    JETSON_ENCODER="h264_v4l2m2m"
    echo "    Найден V4L2-энкодер: $JETSON_ENCODER"
else
    JETSON_ENCODER="libx264"
    echo "    Аппаратные кодеки не найдены, использую libx264 (CPU)"
fi

# ---------- Сохранение кодека в переменную окружения ----------
export VHS_JETSON_ENCODER="$JETSON_ENCODER"
echo ""

# ---------- Проверка MediaMTX ----------
if [ -f "/app/bin/mediamtx" ]; then
    echo "[*] MediaMTX найден: /app/bin/mediamtx"
else
    echo "[!] MediaMTX не найден в /app/bin/mediamtx"
    echo "    Скачайте его или проверьте Dockerfile."
    exit 1
fi
echo ""

# ---------- Запуск сервера ----------
echo "[*] Запускаю VHS-Streamer..."
echo "    Web-интерфейс: http://0.0.0.0:8000"
echo "    RTSP:          rtsp://0.0.0.0:8554"
echo "    WebRTC:        http://0.0.0.0:8889"
echo ""

# Запускаем основной процесс (Python)
# Если нужно передать кодек в приложение, можно использовать переменную окружения,
# которую мы добавим в код позже.
exec python3 app.py