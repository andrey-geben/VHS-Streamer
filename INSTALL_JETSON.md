# VHS-Streamer — установка на NVIDIA Jetson (Axiomtek AIE100-N2NX и аналоги)

## Что это за железо

Axiomtek AIE100 — семейство безвентиляторных Edge AI компьютеров на NVIDIA Jetson
(`AIE100-ONA` — Orin Nano, `AIE100-ONX` — Orin NX, `AIE100-903-FL-NX` — Xavier NX,
`AIE100-T2NX` — TX2 NX). У всех:

- процессор **ARM64 (aarch64)**, не x86;
- операционная система — **Ubuntu из состава NVIDIA JetPack**;
- Windows-сборка `VHS-Streamer.exe` здесь **не запускается** — нужна установка из исходников.

Проверить на месте:

```bash
uname -m                 # должно быть aarch64
cat /etc/nv_tegra_release   # версия L4T/JetPack
python3 --version        # на TX2 NX — 3.6; на Orin/Ubuntu 22.04 — 3.10+
```

Для **AIE100-T2NX (TX2 NX)** максимум — **JetPack 4.6 / Ubuntu 18.04 /
Python 3.6**. Установщик автоматически создаёт отдельную копию `runtime/py36`,
понижает синтаксис и ставит совместимые зависимости (Flask 2.x).

Для Orin (JetPack 5/6) и Ubuntu 20.04/22.04:
Python 3.8–3.9 идёт через legacy/runtime, Python 3.10+ — обычный режим.

## Установка (5 минут)

```bash
# 1. скопировать папку проекта на Jetson (scp, флешка, git clone)
cd VHS-Streamer

# 2. запустить установщик
chmod +x install_jetson.sh
./install_jetson.sh              # без автозапуска
# или
./install_jetson.sh --service    # с автозапуском через systemd
```

Скрипт по шагам:

1. проверяет `aarch64` и Python ≥ 3.6;
2. для Python < 3.10 создаёт `runtime/py36`, понижает синтаксис и проверяет его;
3. ставит системные пакеты: `ffmpeg`, `python3-opencv`, `python3-venv`,
   библиотеки для OpenCV (`libgl1-mesa-glx`, `libglib2.0-0`, `libgomp1`);
4. создаёт `.venv` с `--system-site-packages` (чтобы был доступен системный OpenCV);
5. ставит `requirements-py36.txt` для Python 3.6 или `requirements-linux.txt` для 3.10+;
6. готовит `bin/`: скачивает **MediaMTX для arm64** и ссылается на системный `ffmpeg`;
7. создаёт `config.yaml` из корня проекта или из примера;
8. при `--service` ставит systemd-службу; порты открываются только с `--open-ports`.

Для обновления legacy-версии удалите `runtime/py36` и запустите установщик снова.

## Запуск

```bash
./start.sh                      # вручную
sudo systemctl status vhs-streamer   # если ставили с --service
```

Первый запуск создаст пароль администратора и напечатает его в консоли — запишите.
Дальше панель открывается по адресу `http://<ip-jetson>:8100`.
Для доступа из локальной сети установите с `--open-ports`; для edge-схемы
порты открывать не нужно — доступ идёт через WireGuard.

## Offline-установка (без интернета)

На машине с интернетом скачайте заранее:

- MediaMTX: `mediamtx_*_linux_arm64v8.tar.gz` из
  [релизов MediaMTX](https://github.com/bluenviron/mediamtx/releases);
- FFmpeg для arm64: статическую сборку
  [johnvansickle.com/ffmpeg](https://johnvansickle.com/ffmpeg/).

Разложите на Jetson:

```
VHS-Streamer/bin/mediamtx
VHS-Streamer/bin/ffmpeg
VHS-Streamer/bin/ffprobe
```

и выполните `chmod +x bin/mediamtx bin/ffmpeg bin/ffprobe`, после чего
`install_jetson.sh` не будет ничего скачивать.

## Аппаратное кодирование на Jetson

Intel Quick Sync (QSV) на Jetson нет — он есть только на Intel. Зато у Jetson
свой энкодер **NVENC**. Чтобы он заработал, нужен FFmpeg с поддержкой
Jetson (`nvv4l2h264enc`/`h264_nvmpi`), а не стандартный Ubuntu-пакет.
Проверить:

```bash
ffmpeg -hide_banner -encoders 2>/dev/null | grep -Ei "nvenc|nvmpi|v4l2"
```

Если таких кодировщиков нет — приложение автоматически останется на `libx264`
(CPU). Для 4–8 камер это нормально, для большего числа нужен FFmpeg с NVENC.

## Особенности и ограничения

- **OpenCV** берётся из `apt` (`python3-opencv`): колёс `opencv-python`
  под aarch64 может не быть, особенно на старых JetPack.
- **`pyinstaller`** на Jetson не нужен — запускаем из исходников.
- **архив и `events.db`** лежат в папке программы; при обновлении версии
  сохраняйте `config.yaml`, `archive/`, `events.db`.
- **Порты**: 8100 (панель), 8081 (ONVIF), 8554 (RTSP), 8889 + 8189/udp (WebRTC),
  8080 (дубли). По умолчанию установщик их не открывает; для локальной сети
  используйте `--open-ports`, для edge-схемы — только WireGuard.
- Питание: не отключайте Jetson «из розетки» — используйте `sudo poweroff`,
  иначе можно повредить файловую систему SD/NVMe.

## Обновление версии

```bash
sudo systemctl stop vhs-streamer
# распаковать/скопировать новые файлы, сохранив config.yaml, archive/, events.db
sudo systemctl start vhs-streamer
```

## Если что-то не работает

| Симптом | Что проверить |
|---|---|
| `aarch64` не определяется | это не Jetson; для x86 используйте `install.sh` |
| Python 3.6/3.8 | это нормально: установщик соберёт `runtime/py36` |
| `cv2` не импортируется | `sudo apt install python3-opencv`, venv создавать с `--system-site-packages` |
| панель не открывается из сети | `sudo ufw status`, порт 8100 |
| камеры не подключаются | `logs/vhs-streamer.log`, доступность RTSP камеры с Jetson (`ffprobe rtsp://...`) |
| превью чёрные | WebRTC идёт на 8889/8189udp, проверьте их и `mediamtx.yml` |

## Вариант через Docker (работает и на JetPack 5, и на JetPack 6)

Если на Jetson Python 3.8 (JetPack 5) или не хочется ставить пакеты в систему —
используйте контейнер. Docker-образ собирается на `ubuntu:22.04` с Python 3.10
и системным OpenCV, поэтому версия Python на самом Jetson не важна.

```bash
# на Jetson: docker и compose (один раз)
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER      # затем перелогиниться

cd VHS-Streamer/VHS-Streamer-Jetson
chmod +x run.sh build.sh entrypoint.sh
./run.sh                            # собрать и запустить
```

Скрипт собирает образ `vhs-streamer:jetson`, поднимает контейнер с
`network_mode: host` (нужно для RTSP/WebRTC и доступа к камерам) и монтирует
каталог проекта в `/app`, поэтому `config.yaml`, `archive/`, `logs/` и `events.db`
остаются на диске Jetson и переживают пересоздание контейнера.

Полезные команды:

```bash
docker compose logs -f                 # логи
docker compose logs | grep -A3 "ПЕРВЫЙ ЗАПУСК"   # пароль администратора
docker compose down                    # остановить
docker compose up -d                   # запустить снова
```

Docker сам скачает MediaMTX для arm64 при первом запуске (нужен интернет).
Без интернета положите `bin/mediamtx` (linux_arm64v8) и `bin/ffmpeg` заранее —
entrypoint их подхватит.

Ограничение: аппаратный NVENC внутри контейнера не используется (для него нужен
NVIDIA Container Runtime и FFmpeg с Jetson-патчами). Кодирование — `libx264`,
для 4–8 камер на Orin Nano/NX этого достаточно.

### JetPack 4.x (Ubuntu 18.04, Python 3.6.9)

Если `python3 --version` показывает **3.6** — это Ubuntu 18.04 и JetPack 4.x.
Нативный запуск **поддерживается**: установщик создаёт `runtime/py36`,
понижает синтаксис и ставит Flask 2.x.

```bash
cd VHS-Streamer
chmod +x install_jetson.sh start.sh
./install_jetson.sh --service
```

Docker остаётся резервным вариантом, если нативное окружение не собирается:

```bash
sudo apt install -y docker.io docker-compose
sudo usermod -aG docker $USER
cd VHS-Streamer/VHS-Streamer-Jetson
chmod +x run.sh build.sh entrypoint.sh
./run.sh --bullseye
```

`--bullseye` собирает образ на `python:3.10-slim-bullseye`: glibc 2.31
заводится на ядре 4.9 надёжнее, чем Ubuntu 22.04. OpenCV ставится колесом
`opencv-python-headless` (aarch64).

Ограничение обоих вариантов: NVENC не задействован без Jetson-патчей FFmpeg;
кодирование — `libx264`. Для 4–8 камер это приемлемо.
Для Jetson с JetPack 4 (Xavier NX / TX2 NX) это нормально для 4–8 камер;
для большего числа камер имеет смысл обновить JetPack до 5/6 и собрать FFmpeg
с `nvv4l2h264enc`.

