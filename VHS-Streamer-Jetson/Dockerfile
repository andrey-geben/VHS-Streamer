# =====================================================================
#  VHS-Streamer — образ для NVIDIA Jetson (ARM64)
# ---------------------------------------------------------------------
#  Базовый образ Ubuntu 22.04 повторяет окружение JetPack 6:
#  Python 3.10 и системный OpenCV из apt — так надёжнее, чем колёса
#  opencv-python под aarch64.
#
#  Сборка:  docker build -t vhs-streamer:jetson .
#  Запуск:  см. docker-compose.yml (нужен network_mode: host)
# =====================================================================
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Yekaterinburg

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-opencv \
        ffmpeg \
        ca-certificates \
        curl \
        tar \
        xz-utils \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости Python (OpenCV уже стоит системным пакетом)
COPY requirements-linux.txt /app/requirements-linux.txt
RUN pip3 install --no-cache-dir -r /app/requirements-linux.txt

# Код приложения
COPY . /app

# ffmpeg ищем рядом с программой: кладём ссылки на системные бинарники
RUN mkdir -p /app/bin \
    && ln -sf /usr/bin/ffmpeg /app/bin/ffmpeg \
    && ln -sf /usr/bin/ffprobe /app/bin/ffprobe

RUN chmod +x /app/VHS-Streamer-Jetson/entrypoint.sh || true

EXPOSE 8000 8081 8554 8889 8080
EXPOSE 8189/udp

ENTRYPOINT ["/bin/bash", "/app/VHS-Streamer-Jetson/entrypoint.sh"]
