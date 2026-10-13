# VHS-Streamer

Сервер видеонаблюдения с RTSP-раздачей через MediaMTX, DVR-подобной
циклической записью, веб-интерфейсом и интеграцией с NVR
(Интеллект-икс, Macroscop, Trassir и др.).

Проект рассчитан на корпоративную эксплуатацию: несколько сетевых
интерфейсов, изоляция сегментов, передача одного и того же потока
в разные сети без дополнительной нагрузки на камеру.

---

## Возможности

- **Захват камер**
  - IP-камеры (RTSP/HTTP) — прямое подключение через OpenCV.
  - Аналоговые камеры (USB, карты захвата, dshow/V4L2).
- **Раздача RTSP через MediaMTX**
  - До **3 потоков** с одной камеры (лимит настраивается).
  - Работает во всех плеерах и NVR (VLC, Интеллект-икс, ffplay).
  - Готовые ссылки в UI для каждого сетевого интерфейса.
- **Транскодирование**
  - Автоопределение рабочих H264-кодеков: `libx264`, `h264_qsv`,
    `h264_nvenc`, `h264_amf` (проверяется реальным запуском).
  - GPU-ускорение (Intel Quick Sync, NVIDIA NVENC, AMD AMF).
  - Пресеты под разные сценарии: мобильный, веб, полное качество.
- **Архив (DVR)**
  - Циклическая запись сегментами по N минут.
  - Автоочистка по размеру и по возрасту (лимиты настраиваются).
  - Структура `archive/<camera_id>/<YYYY-MM-DD>/<HH-MM-SS>.mp4`.
- **Веб-UI**
  - Сетка камер (WebRTC-превью через MediaMTX).
  - Просмотр архива, воспроизведение сегментов.
  - Управление камерами, потоками, архивом, безопасностью.
  - Роли `admin` / `viewer`.
- **Безопасность**
  - Аутентификация по сессиям (cookie) и по API-токену.
  - Пароли и токен хранятся в `config.yaml` (не в git).
- **Bootstrap**
  - При первом запуске сам скачивает MediaMTX и FFmpeg.
- **Скрипты**
  - `start.bat` / `stop.bat` / `restart.bat` / `status.bat` — Windows.
  - `start.sh` / `stop.sh` — Linux.

---

## Архитектура

```
IP-камера ───────┐
                 ├──▶ [ MediaMTX :8554 ] ──▶ RTSP-клиенты
Аналоговая ──────┘   (пути создаются           (VLC, NVR,
камера (FFmpeg)       динамически)              Интеллект-икс)
                              │
                              │ :8889
                              ▼
                        WebRTC-превью
                        в браузере

┌──────────────────────────────────────────────┐
│  Flask-сервер :8000                          │
│  ├── камеры (захват)                         │
│  ├── рекордеры (циклическая запись)          │
│  ├── MediaMTXManager (управление RTSP)       │
│  ├── streams_api (потоки)                    │
│  ├── settings_api (настройки)                │
│  └── diagnostics (сбор отчётов)              │
└──────────────────────────────────────────────┘
```

**Ключевой принцип.** Камера отдаёт поток **один раз** серверу
(для аналоговых — через FFmpeg в raw-путь MediaMTX). Дальше
MediaMTX размножает поток на любое число клиентов. Камера
не видит конечных потребителей.

---

## Требования

- **Python 3.12+**
- **Windows 10/11** или **Linux** (Ubuntu 22.04+)
- **FFmpeg** и **MediaMTX** — скачиваются автоматически при
  первом запуске (bootstrap).
- Для GPU-транскодирования — соответствующие драйверы:
  - Intel — Intel Graphics Driver
  - NVIDIA — NVIDIA Display Driver + CUDA
  - AMD — Adrenalin Driver

---

## Установка

### Windows

```powershell
git clone https://github.com/andrey-geben/VHS-Streamer.git
cd VHS-Streamer

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

copy config.example.yaml config.yaml
# Откройте config.yaml и задайте secret_key, пароли, API-токен

start.bat
```

Откройте `http://localhost:8000` — войдите под учёткой из `config.yaml`.

### Linux

```bash
git clone https://github.com/andrey-geben/VHS-Streamer.git
cd VHS-Streamer

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp config.example.yaml config.yaml
# Отредактируйте config.yaml

chmod +x start.sh stop.sh
./start.sh
```

---

## Настройка

Основные разделы в `config.yaml`:

### `server`
- `host`, `port` — адрес и порт Flask.
- `secret_key` — длинная случайная строка для подписи сессий.
  Сгенерировать:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  ```

### `network.interfaces`
Список сетевых интерфейсов для генерации RTSP-ссылок в UI:

```yaml
network:
  interfaces:
    - name: eth0
      ip: 10.0.1.10
      description: "Сеть охраны труда"
    - name: eth1
      ip: 192.168.1.10
      description: "Сеть безопасности"
    - name: eth2
      ip: 172.16.0.10
      description: "Изолированная сеть"
```

### `security`
- `users` — логины/пароли и роли (`admin` / `viewer`).
- `api_token` — токен для внешних запросов к API.

### `cameras`
```yaml
cameras:
  - id: ip_1
    name: "IP камера — Вход"
    type: ip
    source: "rtsp://user:pass@192.168.1.100:554/stream1"
    fps: 25
    width: 1280
    height: 720
    record: true

  - id: analog_1
    name: "Аналоговая камера 1"
    type: analog
    source: "HD User Facing"     # Windows dshow: имя устройства
    capture_index: 0             # OpenCV: индекс
    fps: 25
    width: 720
    height: 576
    record: true
```

**Важно для аналоговых камер на Windows:** dshow принимает **имя**
устройства, а OpenCV — **индекс**. Оба указываются отдельно.

Список устройств:
```powershell
.\bin\ffmpeg.exe -list_devices true -f dshow -i dummy
```

### `archive`
- `path` — куда писать (можно указать путь к NAS).
- `segment_minutes` — длина сегмента.
- `max_size_gb` / `max_age_days` — лимиты (удаляется самое старое).

### `streams`
Создаются автоматически через веб-UI, но можно задать вручную:

```yaml
streams:
  - id: ip_1_s1
    camera_id: ip_1
    path: /ip_1_s1
    copy: true          # без транскодирования
    enabled: true
```

**Правила:**
- На одну камеру — не более 3 потоков.
- `copy: true` — ретрансляция без нагрузки на CPU.
- `copy: false` — транскодирование с параметрами:
  - `codec` (`libx264` / `h264_qsv` / `h264_nvenc` / `h264_amf`)
  - `bitrate` (`500k`, `1500k`, ...)
  - `scale` (`640:-2`, `854:-2`, ...)
  - `fps` (15, 20, 25)

---

## Запуск и остановка

### Windows
- `start.bat` — запустить сервер в отдельном окне.
- `stop.bat` — остановить сервер и все дочерние процессы.
- `restart.bat` — перезапустить.
- `status.bat` — показать состояние (PID, порты).

### Linux
```bash
./start.sh   # запуск в фоне
./stop.sh    # остановка
```

---

## Веб-интерфейс

- **Главная** (`/`) — сетка камер с WebRTC-превью.
- **Архив** (`/archive`) — список сегментов по датам.
- **Настройки** (`/settings`):
  - управление камерами;
  - настройки архива;
  - потоки RTSP (создание, лимит 3 на камеру, готовые ссылки);
  - диагностика (сбор ZIP-отчёта);
  - безопасность (только для `admin`).

---

## Интеграция с NVR (Интеллект-икс, Macroscop, Trassir)

В разделе **Настройки → Дублирование потока** у каждого потока
показаны готовые RTSP-адреса для каждого сетевого интерфейса:

```
rtsp://127.0.0.1:8554/analog_1_s1        (локально)
rtsp://10.0.1.10:8554/analog_1_s1        (Сеть охраны труда)
rtsp://192.168.1.10:8554/analog_1_s1     (Сеть безопасности)
rtsp://172.16.0.10:8554/analog_1_s1      (Изолированная сеть)
```

Скопируйте нужный адрес (кнопка 📋) и вставьте в настройках
NVR как RTSP-поток. Порт 8554, транспорт — **TCP**.

**Интеллект-икс:** формат адреса — `rtsp://<IP>:<port>/<path>`.
Поддерживает несколько потоков с одной камеры.

---

## Мультисетевая работа

Камера отдаёт поток один раз серверу. MediaMTX слушает `0.0.0.0:8554`
и доступен по всем сетевым интерфейсам. Клиент из конкретной сети
подключается по IP сервера в этой сети.

Изоляция между сетями — на firewall (iptables/nftables/Windows
Firewall). MediaMTX не ограничивает клиентов по интерфейсам.

---

## API

Все эндпоинты возвращают JSON. Авторизация:
- **Сессия** — для браузера (cookie).
- **API-токен** — для скриптов: заголовок `X-API-Token` или
  query-параметр `?token=...`.

Основные эндпоинты:

| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/status` | Общий статус камер и рекордеров |
| GET | `/api/cameras` | Список камер |
| POST | `/api/cameras` | Создать камеру |
| PUT | `/api/cameras/<id>` | Обновить камеру |
| DELETE | `/api/cameras/<id>` | Удалить камеру |
| GET | `/api/streams` | Список потоков с URL |
| POST | `/api/streams` | Создать поток |
| DELETE | `/api/streams/<id>` | Удалить поток |
| POST | `/api/streams/<id>/toggle` | Вкл/выкл поток |
| GET | `/api/transcode/codecs` | Доступные кодеки и пресеты |
| GET | `/api/diagnostics/download` | ZIP с диагностикой |

Пример:
```bash
curl -H "X-API-Token: ВАШ_ТОКЕН" http://localhost:8000/api/status
```

---

## Структура проекта

```
VHS-Streamer/
├── app.py                  # Flask, точка входа
├── bootstrap.py            # Скачивание MediaMTX и FFmpeg
├── auth.py                 # Аутентификация
├── camera.py               # Захват, MJPEG, публикация кадров
├── recorder.py             # Циклическая запись в архив
├── duplicator.py           # MJPEG-дублирование на отдельный порт
├── mediamtx_manager.py     # Управление MediaMTX и FFmpeg-команды
├── streams_api.py          # API потоков RTSP
├── settings_api.py         # API настроек и камер
├── diagnostics.py          # Сбор ZIP-отчёта
├── preview.py              # Loopback для аналоговых камер
├── transcode.py            # Определение H264-кодеков
├── config.example.yaml     # Шаблон конфига (без секретов)
├── config.yaml             # Рабочий конфиг (не в git)
├── mediamtx.yml            # Конфиг MediaMTX
├── requirements.txt
├── start.bat / stop.bat / restart.bat / status.bat
├── start.sh / stop.sh
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── index.html
│   ├── archive.html
│   └── settings.html
├── bin/                    # Скачивается bootstrap (не в git)
├── archive/                # Записи (не в git)
└── logs/                   # Логи (не в git)
```

---

## Диагностика проблем

### Камера не открывается
- **IP:** проверьте URL RTSP в VLC.
- **Аналоговая (Windows):** dshow принимает только имя устройства.
  Узнать:
  ```powershell
  .\bin\ffmpeg.exe -list_devices true -f dshow -i dummy
  ```
- **Аналоговая (Linux):** проверьте `v4l2-ctl --list-devices`.

### Поток не виден в VLC
- Проверьте `http://127.0.0.1:9997/v3/paths/list` — есть ли путь,
  поле `ready: true`.
- Убедитесь, что VLC настроен на TCP:
  `Медиа → Открыть URL → :rtsp-tcp`.

### GPU-кодек не работает
- Автоопределение проверяет кодеки реальным запуском FFmpeg.
- Если нужный отсутствует — смотрите `logs/vhs-streamer.log`.
- Для NVIDIA: `Cannot load nvEncodeAPI64.dll` — обновите драйвер.

### Ошибки `corrupted macroblock` в логе
- Это шум декодера OpenCV при чтении RTSP. Не влияет на качество
  видео и работу потоков. Снижается уменьшением битрейта loopback
  в `config.yaml` → `mediamtx.loopback_codec`.

### Сбор диагностики
- В UI: **Настройки → Диагностика → Скачать**.
- Или из консоли: `python diagnostics.py`.

---

## Лицензия

Проект распространяется в закрытом режиме. Все права принадлежат
автору.

---

## Автор

**Andrew Geben** — [github.com/andrey-geben](https://github.com/andrey-geben)