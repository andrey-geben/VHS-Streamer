# VHS-Streamer

Сервер видеонаблюдения с RTSP-раздачей через MediaMTX, транскодированием,
DVR-архивом, поддержкой аудио и веб-интерфейсом.

Проект рассчитан на корпоративную эксплуатацию: несколько сетевых
интерфейсов, изоляция сегментов, передача одного и того же потока в разные
сети без дополнительной нагрузки на камеру. Готов к интеграции с NVR
(Интеллект-икс, Macroscop, Trassir и др.).

---

## Возможности

### Захват
- **IP-камеры** (RTSP/HTTP) — прямое подключение через OpenCV.
- **Аналоговые камеры** — USB, карты захвата, dshow (Windows) / V4L2 (Linux).
- **Loopback через MediaMTX** — все камеры читаются один раз, дальше
  раздаются множеству клиентов без перегрузки.

### Раздача RTSP
- **До 3 потоков** с одной камеры (лимит настраивается).
- Работает во всех плеерах и NVR (VLC, Интеллект-икс, ffplay).
- Готовые ссылки в UI для каждого сетевого интерфейса.
- **WebRTC-превью** в браузере — полное качество, минимальная задержка.

### Транскодирование
- **Автоопределение рабочих H264-кодеков**: `libx264`, `h264_qsv`,
  `h264_nvenc`, `h264_amf`. Проверяется реальным запуском FFmpeg при старте.
- **GPU-ускорение** — Intel Quick Sync, NVIDIA NVENC, AMD AMF.
- **Пресеты** в UI: «Мобильный (эконом)», «Мобильный», «Веб / удалённый»,
  «Полное качество».
- **Оценка нагрузки CPU** прямо в форме создания потока.

### Аудио (для IP-камер)
- **Проверка наличия звука** через FFprobe — кнопка «Проверить звук» в UI.
- **Включение/выключение** захвата — галочка в настройках камеры.
- **Запись в архив** — звук попадает в MP4 автоматически, если включён.
- **Passthrough без перекодирования** — минимальная нагрузка.

### Архив (DVR)
- Циклическая запись сегментами по N минут.
- Автоочистка по размеру и по возрасту.
- **Без перекодирования** — `-c:v copy`, качество как с камеры.
- Структура `archive/<camera_id>/<YYYY-MM-DD>/<HH-MM-SS>.mp4`.

### Веб-интерфейс
- Сетка камер (WebRTC-превью).
- Просмотр и воспроизведение архива.
- Управление камерами, потоками, архивом, безопасностью.
- Роли `admin` / `viewer`.
- **Диагностика** — ZIP-отчёт в один клик.
- Страница **«О программе»** с информацией об авторе и лицензии.

### Безопасность
- Аутентификация по сессиям (cookie) и API-токену.
- Пароли и токен — только в `config.yaml` (не в git).

### Развёртывание
- **`install.bat`** / **`install.sh`** — установка на новую машину в один клик.
- **Bootstrap** — при первом запуске сам скачивает MediaMTX и FFmpeg.
- **Сборка `.exe`** через PyInstaller — не требует Python у конечного
  пользователя.
- **Bat-скрипты** для эксплуатации: `start.bat`, `stop.bat`,
  `restart.bat`, `status.bat`.

---

## Архитектура

```
IP-камера ───┐
             ├──▶ [ MediaMTX :8554 ] ──▶ RTSP-клиенты
Аналоговая ──┘   (raw-пути:            (VLC, NVR, Интеллект-икс)
камера            __raw_<cam_id>)                │
(через FFmpeg)                                   │
                                                 │ :8889
                                                 ▼
                                        WebRTC-превью в браузере

                            ┌─────────────────────────┐
                            │  Пользовательские пути: │
                            │  /<cam_id>_s1 (copy)    │
                            │  /<cam_id>_s2 (transc.) │
                            └─────────────────────────┘

┌───────────────────────────────────────────────────┐
│  Flask-сервер :8000                               │
│  ├── камеры (Camera)                              │
│  ├── рекордеры (Recorder, FFmpeg-подпроцесс)      │
│  ├── MediaMTXManager (управление RTSP)            │
│  ├── streams_api (потоки)                         │
│  ├── settings_api (настройки)                     │
│  ├── audio_probe (проверка звука)                 │
│  ├── transcode (определение кодеков)              │
│  └── diagnostics (сбор отчётов)                   │
└───────────────────────────────────────────────────┘
```

**Ключевой принцип.** Камера отдаёт поток **один раз** серверу.
MediaMTX размножает его на любое число клиентов. Камера не видит
конечных потребителей. Это снимает ограничение на число одновременных
подключений и снижает нагрузку.

---

## Требования

- **Python 3.12+** (только для установки из исходников или сборки)
- **Windows 10/11** или **Linux** (Ubuntu 22.04+)
- **FFmpeg** и **MediaMTX** — скачиваются автоматически при первом запуске
- Для GPU-транскодирования — драйверы:
  - Intel — Intel Graphics Driver
  - NVIDIA — NVIDIA Display Driver + CUDA
  - AMD — Adrenalin Driver

---

## Быстрый старт

### Windows

```powershell
git clone https://github.com/andrey-geben/VHS-Streamer.git
cd VHS-Streamer

.\install.bat
```

Скрипт **автоматически**:
1. Проверит наличие Python 3.12+.
2. Создаст виртуальное окружение `.venv`.
3. Установит зависимости из `requirements.txt`.
4. Скопирует `config.example.yaml` → `config.yaml`.
5. Проверит структуру проекта.
6. Предложит сразу запустить сервер.

После завершения — **откройте `config.yaml`** и задайте:
- `server.secret_key` — длинная случайная строка;
- `security.users` — логины и пароли;
- `security.api_token` — токен для API.

Затем запустите сервер:

```powershell
.\start.bat
```

Откройте `http://localhost:8000` и войдите под учёткой из `config.yaml`.

### Linux

```bash
git clone https://github.com/andrey-geben/VHS-Streamer.git
cd VHS-Streamer

chmod +x install.sh start.sh stop.sh
./install.sh
```

Скрипт делает то же самое. После завершения:

```bash
./start.sh
```

### Развёртывание из `.exe` (без Python)

Если Python на целевой машине нет:

1. Соберите `.exe` (см. раздел «Сборка `.exe`»).
2. Скопируйте папку `dist\VHS-Streamer\` на целевую машину.
3. Запустите `VHS-Streamer.exe`.
4. При первом запуске bootstrap скачает MediaMTX и FFmpeg.

**Требования к целевой машине:**
- Windows 10/11 x64.
- Microsoft Visual C++ Redistributable 2015-2022 (обычно уже стоит).
- Драйверы GPU (только если нужно аппаратное транскодирование).

---

## Настройка

Основные разделы `config.yaml`:

### `server`
```yaml
server:
  host: 0.0.0.0
  port: 8000
  secret_key: "CHANGE-ME"    # python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### `network.interfaces`
Список интерфейсов для генерации RTSP-ссылок в UI:
```yaml
network:
  interfaces:
    - { name: eth0, ip: 10.0.1.10,    description: "Сеть охраны труда" }
    - { name: eth1, ip: 192.168.1.10, description: "Сеть безопасности" }
    - { name: eth2, ip: 172.16.0.10,  description: "Изолированная сеть" }
```

### `security`
```yaml
security:
  users:
    - { username: admin,  password: "CHANGE-ME", role: admin  }
    - { username: viewer, password: "CHANGE-ME", role: viewer }
  api_token: "CHANGE-ME"
```

### `mediamtx`
```yaml
mediamtx:
  binary: "./bin/mediamtx.exe"
  config: "./mediamtx.yml"
  rtsp_port: 8554
  api_url: "http://127.0.0.1:9997"
  loopback_codec: auto    # auto | libx264 | h264_qsv | h264_nvenc | h264_amf
```

### `cameras`

**IP-камера:**
```yaml
- id: ip_1
  name: "IP камера — Вход"
  type: ip
  source: "rtsp://user:pass@192.168.1.100:554/stream1"
  fps: 25
  width: 1280
  height: 720
  record: true
  transport: "tcp"
  has_audio: null    # заполняется автоматически при проверке через UI
  audio: false       # захватывать звук (только если has_audio: true)
```

**Аналоговая камера (Windows):**
```yaml
- id: analog_1
  name: "Аналоговая камера 1"
  type: analog
  source: "HD User Facing"    # имя устройства dshow
  capture_index: 0            # индекс для OpenCV
  fps: 25
  width: 720
  height: 576
  record: true
```

Список устройств dshow:
```powershell
.\bin\ffmpeg.exe -list_devices true -f dshow -i dummy
```

### `archive`
```yaml
archive:
  enabled: true
  path: "./archive"
  segment_minutes: 5
  max_size_gb: 50
  max_age_days: 30
```

### `streams`
Создаются через веб-UI, но можно задать вручную:
```yaml
streams:
  - id: ip_1_s1
    camera_id: ip_1
    path: /ip_1_s1
    copy: true          # без транскодирования
    enabled: true

  - id: ip_1_s2
    camera_id: ip_1
    path: /ip_1_s2
    copy: false         # транскодирование
    codec: h264_qsv
    bitrate: "600k"
    scale: "640:-2"
    fps: 15
    enabled: true
```

**Правила:**
- На одну камеру — не более 3 потоков.
- `copy: true` — ретрансляция без нагрузки на CPU.
- `copy: false` — транскодирование.

---

## Запуск и остановка

### Windows
| Команда | Что делает |
|---|---|
| `install.bat` | Установка (venv, зависимости, конфиг) |
| `start.bat` | Запустить сервер в отдельном окне |
| `stop.bat` | Остановить сервер и все дочерние процессы |
| `restart.bat` | Перезапустить |
| `status.bat` | Показать PID и слушающие порты |

### Linux
| Команда | Что делает |
|---|---|
| `./install.sh` | Установка |
| `./start.sh` | Запуск в фоне |
| `./stop.sh` | Остановка |

### Запуск из консоли (для разработки)
```powershell
python app.py
```

---

## Веб-интерфейс

| Раздел | Что там |
|---|---|
| `/` | Сетка камер с WebRTC-превью, полный экран по клику |
| `/archive` | Архив по датам, воспроизведение сегментов |
| `/settings` | Камеры, архив, потоки, диагностика, безопасность |
| `/about` | Информация о программе, авторе, лицензии |

**Роли:**
- `admin` — полный доступ, включая раздел «Безопасность».
- `viewer` — только просмотр камер и архива.

---

## Интеграция с NVR

В разделе **Настройки → Дублирование потока** у каждого потока
показаны готовые RTSP-адреса для каждого сетевого интерфейса:

```
rtsp://127.0.0.1:8554/analog_1_s1        (локально)
rtsp://10.0.1.10:8554/analog_1_s1        (Сеть охраны труда)
rtsp://192.168.1.10:8554/analog_1_s1     (Сеть безопасности)
rtsp://172.16.0.10:8554/analog_1_s1      (Изолированная сеть)
```

Скопируйте нужный адрес (кнопка 📋) и вставьте в настройках NVR
как RTSP-поток. Порт 8554, транспорт — **TCP**.

**Интеллект-икс:** формат адреса `rtsp://<IP>:<port>/<path>`.
Поддерживает несколько потоков с одной камеры.

---

## Транскодирование

В настройках при создании потока поставьте галочку **«Транскодировать»**:

- **Кодек** — выпадающий список рабочих кодеков. Рекомендованный выбирается
  автоматически (QSV / NVENC / AMF, если доступны).
- **Пресет** — кнопки быстрого выбора:
  - **Мобильный (эконом)** — 426×240, 300 кбит/с, 12 fps
  - **Мобильный** — 640×360, 600 кбит/с, 15 fps
  - **Веб / удалённый** — 854×480, 1200 кбит/с, 20 fps
  - **Полное качество** — без масштабирования, 3000 кбит/с, 25 fps
- **Битрейт / Разрешение / FPS** — можно править вручную.
- **Оценка нагрузки** — примерная загрузка CPU.

---

## Аудио (IP-камеры)

1. Откройте камеру в `/settings` (кнопка ✎).
2. Нажмите **«Проверить звук»** — FFprobe определит, есть ли аудио в потоке.
3. Если звук есть — поставьте галочку **«Захватывать звук»**.
4. Сохраните и перезапустите сервер.

Звук попадёт:
- в RTSP-раздачу (VLC покажет дорожку),
- в архив (MP4 будет со звуком).

---

## Мультисетевая работа

Камера отдаёт поток один раз серверу. MediaMTX слушает `0.0.0.0:8554`
и доступен по всем сетевым интерфейсам. Клиент из конкретной сети
подключается по IP сервера в этой сети.

Изоляция между сетями — на firewall (iptables / nftables / Windows
Firewall). MediaMTX не ограничивает клиентов по интерфейсам.

---

## API

Авторизация:
- **Сессия** — для браузера (cookie).
- **API-токен** — для скриптов: `X-API-Token: <token>` или `?token=<token>`.

| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/status` | Общий статус камер, рекордеров, версия приложения |
| GET | `/api/cameras` | Список камер |
| POST | `/api/cameras` | Создать камеру |
| PUT | `/api/cameras/<id>` | Обновить камеру |
| DELETE | `/api/cameras/<id>` | Удалить камеру |
| POST | `/api/cameras/<id>/restart` | Перезапустить камеру |
| POST | `/api/cameras/<id>/probe_audio` | Проверить наличие звука |
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

## Сборка `.exe` (Windows)

Сборка через PyInstaller:

```powershell
# Убедитесь, что сервер остановлен
Get-Process python, mediamtx, ffmpeg -ErrorAction SilentlyContinue | Stop-Process -Force

# Собрать
.\build_exe.bat
```

**Результат:** `dist\VHS-Streamer\` — папка с готовым приложением:
```
VHS-Streamer.exe
_internal/           ← библиотеки Python, OpenCV, NumPy
bin/                 ← MediaMTX, FFmpeg (скачаются при первом запуске)
templates/
static/
config.yaml
config.example.yaml
mediamtx.yml
start.bat / stop.bat / restart.bat / status.bat
```

**Метаданные `.exe`:** правый клик на файле → Свойства → Подробно —
там указаны версия, автор, копирайт, название продукта.

**Развёртывание:** скопируйте всю папку `dist\VHS-Streamer` на целевую
машину. Python устанавливать не нужно. При первом запуске bootstrap
скачает MediaMTX и FFmpeg.

---

## Структура проекта

```
VHS-Streamer/
├── app.py                  # Flask, точка входа
├── bootstrap.py            # Скачивание MediaMTX и FFmpeg
├── helper.py               # Пути для dev и .exe
├── __version__.py          # Версия и авторство
├── auth.py                 # Аутентификация
├── camera.py               # Захват, MJPEG, публикация кадров
├── recorder.py             # Циклическая запись через FFmpeg
├── duplicator.py           # MJPEG-дублирование на отдельный порт
├── mediamtx_manager.py     # Управление MediaMTX, FFmpeg-команды
├── streams_api.py          # API потоков RTSP
├── settings_api.py         # API настроек и камер
├── audio_probe.py          # Проверка звука через FFprobe
├── transcode.py            # Определение H264-кодеков
├── diagnostics.py          # Сбор ZIP-отчёта
├── preview.py              # Loopback-хелперы
├── config.example.yaml     # Шаблон конфига (без секретов)
├── config.yaml             # Рабочий конфиг (не в git)
├── mediamtx.yml            # Конфиг MediaMTX
├── app.spec                # PyInstaller spec
├── version_info.txt        # Метаданные .exe
├── build_exe.bat           # Сборка .exe
├── install.bat             # Установка (Windows)
├── install.sh              # Установка (Linux)
├── icon.ico                # Иконка
├── LICENSE                 # Проприетарная лицензия
├── requirements.txt
├── start.bat / stop.bat / restart.bat / status.bat
├── start.sh / stop.sh
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── index.html
│   ├── archive.html
│   ├── settings.html
│   └── about.html
├── static/
│   └── favicon.ico
├── bin/                    # Скачивается bootstrap (не в git)
├── archive/                # Записи (не в git)
├── logs/                   # Логи (не в git)
└── dist/                   # Сборка .exe (не в git)
```

---

## Диагностика

### Быстрые проверки

**Камера не открывается:**
- IP: проверьте URL в VLC напрямую.
- Аналоговая (Windows): dshow принимает только **имя** устройства:
  ```powershell
  .\bin\ffmpeg.exe -list_devices true -f dshow -i dummy
  ```
- Аналоговая (Linux): `v4l2-ctl --list-devices`.

**Поток не виден в VLC:**
- `http://127.0.0.1:9997/v3/paths/list` — есть ли путь, `ready: true`.
- В VLC: **Медиа → Открыть URL → :rtsp-tcp** (принудительный TCP).

**GPU-кодек не работает:**
- Автоопределение проверяет кодеки реальным запуском FFmpeg.
- NVIDIA: `Cannot load nvEncodeAPI64.dll` — обновите драйвер.

**`corrupted macroblock` в логе** — шум декодера OpenCV, на качество
потока не влияет.

### Сбор диагностики

- **Из UI:** Настройки → Диагностика → Скачать.
- **Из консоли:** `python diagnostics.py`.

Архив содержит: систему, конфиг (без секретов), состояние MediaMTX,
устройства FFmpeg, последние строки логов.

---

## Лицензия

Проект распространяется в закрытом режиме. Все права принадлежат автору.

См. файл [LICENSE](LICENSE).

---

## Автор

**Andrew Geben** — [github.com/andrey-geben](https://github.com/andrey-geben)