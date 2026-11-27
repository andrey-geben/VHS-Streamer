"""
VHS-Streamer — главный сервер.
"""
import atexit
import logging
import os
import platform
import shutil
from urllib.parse import quote
import threading
import sys
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
#
import base64
import hashlib
import logging
import secrets
#
import yaml
from flask import (
    Flask, render_template, Response, abort,
    request, redirect, url_for, jsonify, g, session, send_from_directory,
    current_app,
)

# ----------------------------------------------------------------------
# Авторство и версия
# ----------------------------------------------------------------------
from __version__ import (
    APP_NAME,
    APP_TITLE,
    APP_FOOTER,
    __version__,
    __author__,
    __copyright__,
    __license__,
    __url__,
)

# ----------------------------------------------------------------------
# Пути и логирование
# ----------------------------------------------------------------------
from helper import app_dir, resource_dir
from first_run import ensure_config, print_first_run_banner
from events_manager import EventsManager

BASE_DIR = app_dir()                 # куда писать (рядом с .exe или корень проекта)
RES_DIR = resource_dir()             # откуда читать шаблоны
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "vhs-streamer.log"

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

LOG_DIR.mkdir(exist_ok=True)

_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
_file = RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_file.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logging.basicConfig(
    level=logging.INFO, handlers=[_console, _file], force=True,
)
logger = logging.getLogger("vhs-streamer")

# --- Баннер авторства в логе ---
logger.info("=" * 60)
logger.info(APP_FOOTER)
logger.info("=" * 60)
logger.info("Логи пишутся в: %s", LOG_FILE)


# ======================================================================
#  Bootstrap
# ======================================================================
from bootstrap import (
    ensure_all as bootstrap_ensure_all,
    ensure_bin_dir as bootstrap_ensure_bin_dir,
    is_ready as bootstrap_is_ready,
    get_path as bootstrap_get_path,
    BIN_DIR as BOOTSTRAP_BIN_DIR,
)

try:
    bootstrap_ensure_bin_dir()
    logger.info("Папка bin/ готова: %s", BOOTSTRAP_BIN_DIR)
except RuntimeError as e:
    logger.error("Не удалось подготовить папку bin/: %s", e)
    sys.exit(1)

if not bootstrap_is_ready():
    logger.info("Компоненты не найдены — запускаю bootstrap...")
    try:
        result = bootstrap_ensure_all()
        logger.info("Bootstrap завершён: %s", result)
    except Exception as e:
        logger.exception("Bootstrap упал: %s", e)

if not bootstrap_is_ready():
    # ------------ подсказка -----------------
    # На головной станции часто нет интернета: bootstrap не может скачать
    # MediaMTX/FFmpeg. Вместо молчаливого выхода объясняем, что делать.
    msg = (
        "\n" + "=" * 70 + "\n"
        "  НЕ НАЙДЕНЫ КОМПОНЕНТЫ: MediaMTX и/или FFmpeg\n"
        "\n"
        f"  Искали в: {BOOTSTRAP_BIN_DIR}\n"
        "\n"
        "  Что сделать (любой вариант):\n"
        "   1) Если папка bin/ есть рядом с программой — проверьте права на неё.\n"
        "   2) Без интернета: положите в bin/ файлы\n"
        "        mediamtx.exe, ffmpeg.exe, ffprobe.exe\n"
        "      (взять с машины разработчика или из релиза).\n"
        "   3) С интернетом: удалите bin/ и запустите снова —\n"
        "      компоненты скачаются автоматически.\n"
        + "=" * 70
    )
    print(msg, file=sys.stderr, flush=True)
    logger.error("MediaMTX/FFmpeg не установлены. %s", BOOTSTRAP_BIN_DIR)
    try:
        input("Нажмите Enter, чтобы закрыть окно...")
    except Exception:
        pass
    sys.exit(1)


# ======================================================================
#  Импорт наших модулей
# ======================================================================
from camera import Camera
from recorder import Recorder
from event_recorder import EventRecorder
from duplicator import StreamDuplicator
import auth as auth_mod
from auth import can_access_camera
from settings_api import settings_bp
from onvif_server import onvif_bp
from streams_api import streams_bp, _sync_mediamtx_auth
from diagnostics import diagnostics_bp
from integrations import integration_bp
from mediamtx_manager import MediaMTXManager
import mediamtx_manager as _mtx_helpers
from preview import get_loopback_url, raw_path_name
from events_manager import EventsManager
from system_monitor import get_system_stats

# ======================================================================
#  Конфиг
# ======================================================================
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ------------ подсказка -----------------
# Первый запуск на новой машине: config.yaml может отсутствовать.
# Создаём его из примера, но пароль задаёт пользователь через /setup.
config_existed = CONFIG_PATH.exists()
first_run_info = None
try:
    first_run_info = ensure_config(CONFIG_PATH, BASE_DIR / "config.example.yaml")
except Exception as e:
    logger.exception("Не удалось создать config.yaml при первом запуске: %s", e)

config = load_config()

# ------------ подсказка -----------------
# Флаги обслуживания обрабатываем до старта служб: на целевой машине
# проще набрать VHS-Streamer.exe --reset-auth, чем править YAML руками.
from cli_auth import handle_argv  # noqa: E402
if handle_argv(sys.argv[1:], CONFIG_PATH):
    sys.exit(0)
server_cfg = config.get("server", {})

security_cfg = config.get("security", {})
archive_cfg = config.get("archive", {})

# ------------ мастер первого запуска -----------------
SETUP_TOKEN = None
SETUP_REQUIRED = False
_setup_admin = next(
    (u for u in security_cfg.get("users", []) if u.get("role") == "admin"),
    None,
)
_setup_password = str((_setup_admin or {}).get("password") or "").strip()
if not config_existed or not _setup_admin or _setup_password in ("", "CHANGE-ME"):
    SETUP_REQUIRED = True
    SETUP_TOKEN = secrets.token_urlsafe(9)
    _port = int(server_cfg.get("port", 8100))
    print(
        "\n" + "=" * 66 +
        "\n  ПЕРВЫЙ ЗАПУСК VHS-Streamer\n"
        "  Откройте в браузере:\n"
        f"  http://127.0.0.1:{_port}/setup?token={SETUP_TOKEN}\n\n"
        "  Задайте логин, пароль администратора и API-токен.\n"
        "  Код действует до завершения настройки.\n" +
        "=" * 66 + "\n",
        flush=True,
    )
    logger.warning("Первый запуск: требуется /setup (код в консоли)")
elif first_run_info:
    _port = server_cfg.get("port", 8100)
    print_first_run_banner(first_run_info, f"http://127.0.0.1:{_port}")


def archive_storage_status(cfg=None) -> dict:
    """Проверяет, что архив настроен на подключённый внешний носитель."""
    cfg = cfg if isinstance(cfg, dict) else archive_cfg
    raw = str(cfg.get("path") or "").strip()
    if not raw:
        return {
            "configured": "", "path": "", "available": False,
            "external": False, "writable": False, "free_gb": 0,
            "reason": "Путь к внешнему архиву не задан",
        }
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    path = path.resolve()
    result = {
        "configured": raw, "path": str(path), "available": False,
        "external": False, "writable": False, "free_gb": 0, "reason": "",
    }
    if BASE_DIR == path or BASE_DIR in path.parents:
        result["reason"] = "Путь архива находится внутри системного каталога программы"
        return result
    if not path.exists() or not path.is_dir():
        result["reason"] = "Внешнее хранилище не подключено: путь не найден"
        return result

    external = False
    if os.name == "nt":
        system_drive = os.environ.get("SystemDrive", "C:").lower()
        external = bool(path.drive) and path.drive.lower() != system_drive
    else:
        try:
            import psutil
            mounts = [
                str(part.mountpoint)
                for part in psutil.disk_partitions(all=False)
                if part.mountpoint
            ]
            mount = max(
                (m for m in mounts if path == Path(m) or Path(m) in path.parents),
                key=len, default="",
            )
            external = bool(mount) and mount != "/"
        except Exception:
            external = False
    result["external"] = external
    if not external:
        result["reason"] = "Архив должен находиться на отдельном внешнем носителе"
        return result
    if not os.access(path, os.W_OK):
        result["reason"] = "Нет прав на запись во внешнее хранилище"
        return result
    try:
        result["free_gb"] = round(shutil.disk_usage(path).free / (1024 ** 3), 1)
    except OSError:
        result["free_gb"] = 0
    result["available"] = True
    result["writable"] = True
    result["reason"] = "Внешнее хранилище подключено"
    return result


archive_storage = archive_storage_status(archive_cfg)
dup_cfg = config.get("duplicate_stream", {})
mediamtx_cfg = config.get("mediamtx", {})
cameras_cfg = config.get("cameras", [])
streams_cfg = config.get("streams", [])


# ------------ подсказка -----------------
# Если прошлый запуск упал, ffmpeg/mediamtx могли остаться висеть.
# Проверяем, свободен ли наш порт: если да — это осиротевшие процессы,
# их надо погасить, иначе они держат камеры и порты.
def _our_port_busy(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0



# Страховка: при аварийном выходе (sys.exit/необработанное исключение)
# всё равно гасим дочерние процессы.
def _cleanup_at_exit():
    try:
        from proc_tree import kill_leftovers
        kill_leftovers(BOOTSTRAP_BIN_DIR)
    except Exception:
        pass


atexit.register(_cleanup_at_exit)

try:
    from proc_tree import kill_leftovers, count_bin_processes
    if not _our_port_busy(int(server_cfg.get("port", 8100))):
        _orphans = count_bin_processes(BOOTSTRAP_BIN_DIR)
        if _orphans:
            logger.warning("Найдено %d зависших процессов в bin/ — закрываю", _orphans)
            kill_leftovers(BOOTSTRAP_BIN_DIR)
except Exception as exc:
    logger.warning("Проверка зависших процессов не удалась: %s", exc)

# ======================================================================
#  MediaMTX (до камер!)
# ======================================================================
_mediamtx_bin_name = "mediamtx.exe" if IS_WINDOWS else "mediamtx"
_mediamtx_binary = bootstrap_get_path(_mediamtx_bin_name)
_mediamtx_config = BASE_DIR / "mediamtx.yml"

# ----------------------------------------------------------------------
#  Синхронизация auth в mediamtx.yml — ДО mediamtx.start()
# ----------------------------------------------------------------------
#  Если файл оставлен от прошлого запуска в «битом» состоянии
#  (например, authInternalUsers: [] — пустой список ЗАКРЫВАЕТ API),
#  MediaMTX стартует с закрытым API, и все последующие
#  mediamtx.add_path() получат 401 Unauthorized.
#  Поэтому синхронизацию надо делать ДО старта.
# ----------------------------------------------------------------------
try:
    _sync_mediamtx_auth(config, _mediamtx_config)
except Exception as e:
    logger.exception(
        "Не удалось синхронизировать auth mediamtx.yml: %s", e,
    )

# ------------ подсказка -----------------
# На машине, куда перенесли программу, mediamtx.yml мог остаться от другой
# установки: в нём абсолютные пути к ffmpeg с чужого компьютера, и MediaMTX
# бесконечно пытается их запускать. Пути всё равно генерируются приложением,
# поэтому чистим их перед стартом.
def _sanitize_mediamtx_paths(cfg_path: Path) -> None:
    try:
        if not cfg_path.exists():
            return
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        old = data.get("paths") or {}
        if old:
            data["paths"] = {}
            cfg_path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            logger.warning(
                "mediamtx.yml: убрано %s устаревших путей (создаются заново)", len(old))
    except Exception as e:
        logger.warning("mediamtx.yml: не удалось очистить пути: %s", e)


_sanitize_mediamtx_paths(_mediamtx_config)

mediamtx: MediaMTXManager | None = None
try:
    mediamtx = MediaMTXManager(_mediamtx_binary, _mediamtx_config)
    mediamtx.start()
    logger.info("MediaMTX запущен на порту %s",
                mediamtx_cfg.get("rtsp_port", 8554))
except Exception as e:
    logger.exception("Не удалось запустить MediaMTX: %s", e)
    mediamtx = None


# ======================================================================
#  Loopback для всех камер + восстановление пользовательских потоков
# ======================================================================
#  Все камеры (IP и аналоговые) публикуются в MediaMTX как raw-пути.
#  Camera, рекордер и RTSP-клиенты читают из MediaMTX, а не из
#  физической камеры. Это решает проблему эксклюзивного захвата
#  на Windows и снижает нагрузку на камеру.
# ======================================================================


# ------------ подсказка -----------------
# MediaMTX может упасть (кривой путь, конфликт порта). Раньше приложение
# этого не замечало: камеры теряли поток, API 9997 не отвечал. Сторож
# проверяет процесс и поднимает его заново.
def _mediamtx_watchdog():
    while True:
        time.sleep(15)
        try:
            if mediamtx is None:
                continue
            if not mediamtx.is_alive():
                logger.warning("MediaMTX не работает — перезапускаю")
                if mediamtx.restart():
                    logger.info("MediaMTX перезапущен")
                continue

            # Бывает, что при старте путь не создался (занят, гонка с API).
            # Тогда он появлялся только при добавлении исходящего потока.
            _restored = False
            for cid, cam in list(cameras.items()):
                name = f"__raw_{cid}"
                try:
                    if mediamtx.get_path_status(name) is None:
                        mediamtx.add_path(
                            name, _mtx_helpers.build_raw_path_config(cam))
                        logger.warning("MediaMTX: восстановлен raw-путь %s", name)
                        _restored = True
                except Exception:
                    continue

            # Пути, добавленные через API, живут в памяти MediaMTX и теряются
            # при его перезапуске. Сразу пишем их в mediamtx.yml.
            if _restored:
                try:
                    _sync_mediamtx_auth(config, _mediamtx_config)
                except Exception:
                    pass
        except Exception:
            logger.exception("watchdog MediaMTX")


if mediamtx is not None:
    threading.Thread(target=_mediamtx_watchdog, name="mediamtx-watchdog",
                     daemon=True).start()

loopback_urls: dict[str, str] = {}  # cam_id -> rtsp URL

if mediamtx:
    # 1) Создаём raw-пути для ВСЕХ камер
    for cam_cfg in cameras_cfg:
        cam_id = cam_cfg.get("id")
        raw_name = raw_path_name(cam_id)
        fake_cam = SimpleNamespace(
            id=cam_id,
            type=cam_cfg.get("type", "ip"),
            source=cam_cfg["source"],
            fps=cam_cfg.get("fps", 25),
            audio=cam_cfg.get("audio", False),
        )
        try:
            mediamtx.add_path(
                raw_name, _mtx_helpers.build_raw_path_config(fake_cam)
            )
            loopback_urls[cam_id] = get_loopback_url(cam_id)
        except Exception as e:
            logger.error("Не удалось создать raw-путь для %s: %s", cam_id, e)

    # 2) Ждём готовности всех raw-потоков (до 15 сек на каждый)
    if loopback_urls:
        logger.info("Ожидаю готовности raw-потоков...")
        for cam_id in list(loopback_urls.keys()):
            raw_name = raw_path_name(cam_id)
            deadline = time.time() + 15
            while time.time() < deadline:
                st = mediamtx.get_path_status(raw_name)
                if st and st.get("ready"):
                    logger.info("Raw-поток готов: %s", raw_name)
                    break
                time.sleep(0.5)
            else:
                logger.warning(
                    "Raw-поток не готов за 15 сек: %s — "
                    "камера будет недоступна", raw_name,
                )

    # 3) Восстанавливаем пользовательские потоки из config.yaml
    for stream_cfg in streams_cfg:
        if not stream_cfg.get("enabled", True):
            continue
        cam_id = stream_cfg.get("camera_id")
        cam_cfg = next((c for c in cameras_cfg if c.get("id") == cam_id), None)
        if not cam_cfg:
            logger.warning("Поток %s: камера %s не найдена — пропускаю",
                           stream_cfg.get("id"), cam_id)
            continue
        try:
            raw = raw_path_name(cam_id)
            name = stream_cfg["path"].lstrip("/")
            path_cfg = _mtx_helpers.build_stream_path_config(stream_cfg, raw)
            mediamtx.add_path(name, path_cfg)
            logger.info("Восстановлен поток: %s", name)
        except Exception as e:
            logger.error("Не удалось восстановить поток %s: %s",
                         stream_cfg.get("id"), e)


# ======================================================================
#  Камеры и рекордеры
# ======================================================================
cameras: dict[str, Camera] = {}
recorders: dict[str, Recorder] = {}

cameras: dict[str, Camera] = {}
recorders: dict[str, Recorder] = {}

# ======================================================================
#  Менеджер ONVIF-событий
# ======================================================================
#  Создаётся ПОСЛЕ cameras = {} (иначе лямбда не увидит имя) и ДО цикла
#  камер — чтобы EventRecorder в этом цикле получил рабочий экземпляр.
# ======================================================================
events_manager: EventsManager | None = None
try:
    events_manager = EventsManager(
        cameras_provider=lambda: cameras,
        poll_interval=2.0,
        subscription_ttl_min=10,
    )
    events_manager.start()
    logger.info("EventsManager запущен (опрос каждые 2 сек)")
except Exception as e:
    logger.exception("Не удалось запустить EventsManager: %s", e)
    events_manager = None

for cam_cfg in cameras_cfg:
    try:
        cfg = dict(cam_cfg)
        if cam_cfg["id"] in loopback_urls:
            cfg["preview_source"] = loopback_urls[cam_cfg["id"]]

        cam = Camera(cfg)
        cameras[cam.id] = cam
        cam.start()

        # Архив возможен только при подключённом внешнем носителе.
        archive_on = (
            archive_storage.get("available", False)
            and archive_cfg.get("record_all", True)
        )
        # Режим записи камеры: off / always / events
        # Поддержка старого формата: если record_mode нет,
        # считаем record: true → always, record: false → off.
        mode = cam_cfg.get("record_mode")
        if mode is None:
            mode = "always" if cam_cfg.get("record", False) else "off"
        if not archive_on:
            mode = "off"

        if mode == "off":
            logger.info("[%s] Запись в архив отключена", cam.id)
        elif mode == "always":
            try:
                rec = Recorder(cam, archive_cfg, cam_cfg)
                rec.start()
                recorders[cam.id] = rec
                logger.info("[%s] Режим записи: постоянная", cam.id)
            except Exception:
                logger.exception("[%s] Ошибка создания Recorder", cam.id)
        elif mode == "events":
            if not events_manager:
                logger.warning(
                    "[%s] Режим записи 'events', но EventsManager "
                    "не запущен — пропускаю", cam.id,
                )
            else:
                try:
                    rec = EventRecorder(
                        cam, archive_cfg, cam_cfg, events_manager,
                    )
                    rec.start()
                    recorders[cam.id] = rec
                    logger.info("[%s] Режим записи: по событиям", cam.id)
                except Exception:
                    logger.exception(
                        "[%s] Ошибка создания EventRecorder", cam.id,
                    )
    except Exception as e:
        logger.exception("Не удалось инициализировать камеру %s: %s",
                         cam_cfg.get("id"), e)


# ======================================================================
#  MotionDetector (OpenCV) — независимый от ONVIF способ узнавать о движении
# ======================================================================
#  ВАЖНО: блок должен быть ВНЕ цикла по камерам. Если он внутри —
#  при 2+ камерах создаётся по одному MotionDetectorManager на итерацию,
#  старые остаются в памяти с подписками → дубли событий и утечка памяти.
#  Работает для любой камеры (IP, USB, аналог), потому что кадры всё
#  равно проходят через Camera._run() → Camera.subscribe().
#  События идут через EventsManager.ingest_event() — в тот же канал,
#  что и ONVIF-события: пишутся в /events и триггерят EventRecorder.
# ----------------------------------------------------------------------
motion_detector_mgr = None
_motion_cfg = config.get("motion_detection")
if _motion_cfg is None:
    logger.info(
        "MotionDetector: секция motion_detection отсутствует — выключен. "
        "Добавьте её в config.yaml, чтобы включить."
    )
elif isinstance(_motion_cfg, dict) and _motion_cfg.get("enabled", True) is False:
    logger.info("MotionDetector: выключен в config.yaml (enabled: false)")
elif events_manager is None:
    logger.warning(
        "MotionDetector: EventsManager не запущен — выключен "
        "(некому принимать события)"
    )
else:
    try:
        from motion_detector import MotionDetectorManager

        motion_detector_mgr = MotionDetectorManager(
            _motion_cfg, events_manager.ingest_event
        )
        _attached = 0
        for cam_id, cam in cameras.items():
            cam_cfg = next(
                (c for c in cameras_cfg if c.get("id") == cam_id), {}
            )
            motion_detector_mgr.attach_camera(cam_id, cam_cfg)
            cam.subscribe(
                lambda frame, cid=cam_id: motion_detector_mgr.process_frame(
                    cid, frame
                )
            )
            _attached += 1
        logger.info(
            "MotionDetector: подключён к %d камерам (%s)",
            _attached, ", ".join(cameras.keys()),
        )
    except Exception as e:
        logger.exception("Не удалось запустить MotionDetector: %s", e)
        motion_detector_mgr = None

# ======================================================================
#  Flask
# ======================================================================
# ------------ подсказка -----------------
# Шаблоны и статику ищем сначала РЯДОМ с exe: тогда их можно обновить,
# не пересобирая программу. Если рядом пусто — берём встроенные
# из _MEIPASS. Раньше приоритет был у встроенных, и после замены одного
# exe в кадре оставалась старая разметка (iframe на MediaMTX напрямую).
def _resource_dir(name: str) -> Path:
    external = BASE_DIR / name
    if external.exists():
        return external
    return RES_DIR / name


_TEMPLATES_DIR = _resource_dir("templates")
_STATIC_DIR = _resource_dir("static")

app = Flask(
    __name__,
    template_folder=str(_TEMPLATES_DIR),
    static_folder=str(_STATIC_DIR),
    static_url_path="/static",
)
app.secret_key = server_cfg.get("secret_key", "dev-secret-change-me")
app.config["APP_CONFIG"] = config
app.permanent_session_lifetime = timedelta(
    hours=int(security_cfg.get("session_lifetime_hours", 24))
)

app.config["CAMERAS"] = cameras
app.config["RECORDERS"] = recorders
app.config["VHS_CONFIG"] = config
app.config["CONFIG_PATH"] = str(CONFIG_PATH)
app.config["MEDIAMTX"] = mediamtx
app.config["MEDIAMTX_CONFIG_PATH"] = str(_mediamtx_config)
app.config["MEDIAMTX_HELPERS"] = {
    "build_raw_path_config": _mtx_helpers.build_raw_path_config,
    "build_stream_path_config": _mtx_helpers.build_stream_path_config,
}
app.config["EVENTS_MANAGER"] = events_manager
app.config["LOOPBACK_URLS"] = loopback_urls
app.config["MOTION_DETECTOR_MGR"] = motion_detector_mgr
app.config["SESSION_COOKIE_NAME"] = "vhs_streamer_session"

app.register_blueprint(settings_bp)
app.register_blueprint(onvif_bp)
app.register_blueprint(streams_bp)
app.register_blueprint(diagnostics_bp)
app.register_blueprint(integration_bp)


# ======================================================================
#  Хелперы для шаблонов
# ======================================================================
def archive_root() -> Path:
    """Папка архива. Относительные пути считаем от папки программы,
    а не от текущего каталога процесса (иначе при запуске из ярлыка
    или планировщика архив уезжает в другое место)."""
    raw = Path(config.get("archive", {}).get("path", "./archive"))
    if not raw.is_absolute():
        raw = BASE_DIR / raw
    return raw.resolve()


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


app.jinja_env.globals["archive_root"] = archive_root
app.jinja_env.globals["human_size"] = human_size
app.jinja_env.globals["APP_FOOTER"] = APP_FOOTER
app.jinja_env.globals["APP_NAME"] = APP_NAME
app.jinja_env.globals["APP_VERSION"] = __version__

# ======================================================================
#  Журнал событий
# ======================================================================
@app.route("/events")
@auth_mod.login_required
def events_page():
    return render_template("events.html", user=g.user)
# ======================================================================
#  Мастер первого запуска
# ======================================================================
@app.route("/setup", methods=["GET", "POST"])
def setup_page():
    global SETUP_REQUIRED, SETUP_TOKEN
    if not SETUP_REQUIRED or not SETUP_TOKEN:
        return redirect(url_for("login"))
    token = request.args.get("token") or request.form.get("token") or ""
    if not secrets.compare_digest(token, SETUP_TOKEN):
        abort(403)

    error = None
    suggested_token = secrets.token_urlsafe(24)
    if request.method == "POST":
        username = str(request.form.get("username") or "").strip()
        password = str(request.form.get("password") or "")
        confirm = str(request.form.get("password_confirm") or "")
        api_token = str(request.form.get("api_token") or "").strip()
        if len(username) < 3:
            error = "Логин должен быть не короче 3 символов"
        elif len(password) < 8:
            error = "Пароль должен быть не короче 8 символов"
        elif password != confirm:
            error = "Пароли не совпадают"
        else:
            security = config.setdefault("security", {})
            users = security.setdefault("users", [])
            admin = next((u for u in users if u.get("role") == "admin"), None)
            if admin is None:
                admin = {"role": "admin"}
                users.insert(0, admin)
            admin["username"] = username
            admin["password"] = password
            admin["role"] = "admin"
            security["users"] = [
                u for u in users
                if u is admin or str(u.get("password") or "") not in (
                    "CHANGE-ME", "viewer", "admin",
                )
            ]
            security["api_token"] = api_token or suggested_token
            security["setup_completed"] = True
            server = config.setdefault("server", {})
            if str(server.get("secret_key") or "") in ("", "CHANGE-ME"):
                server["secret_key"] = secrets.token_hex(32)
            try:
                CONFIG_PATH.write_text(
                    yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.exception("Не удалось сохранить первый запуск: %s", exc)
                error = "Не удалось сохранить настройки. Проверьте права на config.yaml."
            else:
                app.config["APP_CONFIG"] = config
                SETUP_REQUIRED = False
                SETUP_TOKEN = None
                logger.info("Первый запуск завершён: администратор %s", username)
                return redirect(url_for("login"))
    return render_template(
        "setup.html", token=token, error=error,
        suggested_token=suggested_token, version=__version__,
    )


# ======================================================================
#  Аутентификация
# ======================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if auth_mod.do_login(u, p):
            response = redirect(request.args.get("next") or url_for("index"))
            user = session.get("user") or {}
            secret = (config.get("security", {}) or {}).get("sso_secret", "")
            return auth_mod.vhs_sso.set_cookie(response, secret, user.get("username", u), user.get("role", "admin"))
        error = "Неверный логин или пароль"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    auth_mod.do_logout()
    response = redirect(url_for("login"))
    return auth_mod.vhs_sso.clear_cookie(response)


@app.route("/favicon.ico")
def favicon():
    from flask import send_file
    return send_file(
        RES_DIR / "static" / "favicon.ico",
        mimetype="image/x-icon",
    )
# ======================================================================
#  PWA: manifest.json и service worker
# ======================================================================
@app.route("/manifest.json")
def pwa_manifest():
    resp = send_from_directory(
        RES_DIR / "static", "manifest.json",
        mimetype="application/manifest+json",
    )
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/sw.js")
def pwa_service_worker():
    resp = send_from_directory(
        RES_DIR / "static", "sw.js",
        mimetype="application/javascript",
    )
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

# ======================================================================
#  О программе
# ======================================================================
@app.route("/about")
@auth_mod.login_required
def about_page():
    return render_template(
        "about.html",
        user=g.user,
        is_arm=platform.machine().lower() in ("aarch64", "arm64", "armv7l", "armv6l"),
        info={
            "version": __version__,
            "author": __author__,
            "copyright": __copyright__,
            "license": __license__,
            "url": __url__,
        },
    )


# ======================================================================
#  Справка
# ======================================================================
@app.route("/help")
@auth_mod.login_required
def help_page():
    return render_template(
        "help.html",
        user=g.user,
        is_arm=platform.machine().lower() in ("aarch64", "arm64", "armv7l", "armv6l"),
        port=int(server_cfg.get("port", 8100)),
        archive_storage=archive_storage_status(),
        app_version=__version__,
    )


# ======================================================================
#  Служебный health-check (без авторизации, только статус)
# ======================================================================
@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "vhs-streamer",
        "version": __version__,
        "archive": archive_storage.get("available", False),
        "archive_reason": archive_storage.get("reason", ""),
        "archive_path": archive_storage.get("path", ""),
    })


# ======================================================================
#  Главная
# ======================================================================
@app.route("/")
@auth_mod.login_required
def index():
    # Фильтр по доступу: viewer видит только разрешённые камеры
    visible_cams = [
        c for c in cameras.values() if can_access_camera(c.id)
    ]
    visible_recorders = {
        cid: r for cid, r in recorders.items()
        if can_access_camera(cid)
    }

    cam_statuses = [c.get_status() for c in visible_cams]
    rec_statuses = {cid: r.get_status() for cid, r in visible_recorders.items()}

    total_archive_bytes = 0
    for r in visible_recorders.values():
        try:
            total_archive_bytes += r.get_status()["archive_size_mb"] * 1024 * 1024
        except Exception:
            pass

    sysinfo = {
        "cameras_total": len(visible_cams),
        "cameras_online": sum(1 for s in cam_statuses if s["connected"]),
        "recorders": len(visible_recorders),
        "archive_size": human_size(total_archive_bytes),
        "archive_path": str(archive_root()),
        "duplicate_enabled": dup_cfg.get("enabled", False),
        "duplicate_port": dup_cfg.get("port", 8080) if dup_cfg.get("enabled") else None,
        "mediamtx_ready": mediamtx is not None,
        "mediamtx_port": mediamtx_cfg.get("rtsp_port", 8554),
        "streams_total": len(streams_cfg),
        "streams_enabled": sum(1 for s in streams_cfg if s.get("enabled", True)),
        "loopback_cameras": list(loopback_urls.keys()),
    }

    # ------------ подсказка -----------------
    # Превью в iframe авторизуется в MediaMTX по web-токену. Если его нет
    # в сессии (старая сессия, перезапуск) — создаём на месте, иначе
    # MediaMTX вернёт 401 и плитка останется чёрной.
    _mtx_token = auth_mod.get_mtx_token()
    _mtx_user = ((g.user or session.get("user") or {}).get("username"))
    if not _mtx_token and _mtx_user:
        _mtx_token = auth_mod.create_mtx_token(_mtx_user)
        session["mtx_token"] = _mtx_token

    return render_template(
        "index.html",
        cameras=visible_cams,
        rec_statuses=rec_statuses,
        sysinfo=sysinfo,
        user=g.user,
        mtx_user=_mtx_user,
        mtx_token=_mtx_token,
    )

# ======================================================================
# A.1: MediaMTX authMethod: http — заглушка (разрешает всё, логирует)
# ======================================================================
#заменили на А.3.
# ======================================================================
# A.3: MediaMTX authMethod: http — реальная проверка
# ======================================================================
@app.route("/internal/mediamtx_auth", methods=["POST"])
def internal_mediamtx_auth():
    """
    Проверка прав для MediaMTX authMethod: http.

    MediaMTX при подключении шлёт POST:
      {user, password, ip, action, path, protocol, id, query}

    Действия `api` и `publish` уже исключены из HTTP-auth
    (authHTTPExclude в mediamtx.yml). Сюда приходит только `read`.

    Правила (A.3):
      1. loopback + RTSP (Camera/recorder читают raw-путь) → 200.
         Ломать нельзя — иначе встанет весь пайплайн записи/анализа.
         Браузер сюда НЕ попадает: он идёт с protocol='webrtc'.
      2. loopback + WebRTC (браузер) и любые внешние клиенты →
         требуется валидная пара user/password:
           - web-токен из Flask-сессии (A.4), или
           - vhs_<stream_id>:<read_password>, или
           - обычный пользователь из config.yaml.
      3. Для web_token / config_user проверяем can_access_camera(cam_id).
         Для stream_password — совпадение path с путём потока.
    """
    data = request.get_json(silent=True) or {}
    ip = str(data.get("ip") or "")
    action = str(data.get("action") or "")
    path = str(data.get("path") or "")
    protocol = str(data.get("protocol") or "")
    user = str(data.get("user") or "")
    password = str(data.get("password") or "")

    # A.3: защищаем только read (publish/api отсечены authHTTPExclude)
    if action != "read":
        return "", 200

    # 1. Loopback RTSP — Camera, recorder, motion_detector
    if ip in ("127.0.0.1", "::1") and protocol != "webrtc":
        return "", 200

    # 2. Проверка creds
    user_info = auth_mod.resolve_mtx_credentials(user, password)
    if not user_info:
        if not user and not password:
            logger.info(
                "mediamtx_auth DENY: креды не переданы (ip=%r protocol=%r path=%r)",
                ip, protocol, path)
        else:
            logger.info(
                "mediamtx_auth DENY: неверные креды (user=%r ip=%r protocol=%r path=%r)",
                user, ip, protocol, path)
        return "", 401

    kind = user_info.get("kind")

    # 2a. Stream-password: путь должен совпадать с путём потока
    if kind == "stream_password":
        expected = (user_info.get("stream_path") or "").lstrip("/")
        if path.lstrip("/") == expected:
            return "", 200
        logger.info(
            "mediamtx_auth DENY: stream path mismatch (stream=%r path=%r)",
            user_info.get("stream_id"), path,
        )
        return "", 401

    # 2b. Web-token / config-user: проверяем доступ к камере
    if path.startswith("__raw_"):
        cam_id = path[len("__raw_"):]
        if auth_mod.can_access_camera(cam_id, user_info):
            return "", 200
        logger.info(
            "mediamtx_auth DENY: no cam access (user=%r cam=%r)",
            user_info.get("username"), cam_id,
        )
        return "", 401

    # 2c. Путь пользовательского потока (без __raw_)
    mtx_path = path.lstrip("/")
    for s in config.get("streams", []):
        if (s.get("path") or "").lstrip("/") == mtx_path:
            cam_id = s.get("camera_id")
            if auth_mod.can_access_camera(cam_id, user_info):
                return "", 200
            logger.info(
                "mediamtx_auth DENY: no cam access (user=%r stream=%r)",
                user_info.get("username"), s.get("id"),
            )
            return "", 401

    logger.info("mediamtx_auth DENY: unknown path=%r", path)
    return "", 401
# ======================================================================
#  MJPEG
# ======================================================================
@app.route("/video_feed/<camera_id>")
@auth_mod.login_required
def video_feed(camera_id: str):
    if not can_access_camera(camera_id):
        logger.warning(
            "access denied to video_feed %s by user %s",
            camera_id, g.user.get("username") if g.user else "?",
        )
        abort(403)
    cam = cameras.get(camera_id)
    if not cam:
        abort(404)
    return Response(
        cam.mjpeg_generator(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ======================================================================
#  Архив
# ======================================================================
@app.route("/archive")
@auth_mod.login_required
def archive_page():
    root = archive_root()
    cams_data = []
    for cam in cameras.values():
        if not can_access_camera(cam.id):
            continue
        cam_dir = root / cam.id
        days = []
        if cam_dir.exists():
            for d in sorted(cam_dir.iterdir(), reverse=True):
                if d.is_dir():
                    files = sorted(
                        (f for f in d.iterdir() if f.is_file()),
                        key=lambda f: f.name, reverse=True,
                    )
                    total = sum(f.stat().st_size for f in files)
                    days.append({
                        "date": d.name,
                        "count": len(files),
                        "size": human_size(total),
                        "files": [
                            {"name": f.name, "size": human_size(f.stat().st_size)}
                            for f in files[:200]
                        ],
                    })
        cams_data.append({
            "camera": cam,
            "days": days,
            "recorder": recorders.get(cam.id).get_status() if cam.id in recorders else None,
        })
    return render_template(
        "archive.html",
        cams_data=cams_data, archive_root=str(root), user=g.user,
        archive_storage=archive_storage_status(),
    )


@app.route("/archive/<camera_id>/<date>/<filename>")
@auth_mod.login_required
def archive_file(camera_id: str, date: str, filename: str):
    if not can_access_camera(camera_id):
        abort(403)
    root = archive_root() / camera_id / date
    if not root.is_dir():
        abort(404)
    return send_from_directory(root, filename, as_attachment=False, conditional=True)


@app.route("/api/archive/<camera_id>/timeline")
@auth_mod.login_required
def api_archive_timeline(camera_id: str):
    """
    Timeline архива для камеры на конкретную дату.

    Query: ?date=YYYY-MM-DD (по умолчанию — сегодня)

    Возвращает:
        {
          "ok": true,
          "camera_id": "...",
          "date": "2026-11-04",
          "dates": ["2026-11-04", "2026-11-03", ...],   # все доступные даты
          "segments": [
            {
              "file": "18-12-30.mp4",
              "url": "/archive/.../18-12-30.mp4",
              "start_ts": 1793785950.0,
              "end_ts":   1793785980.0,
              "size_mb":  12.3,
            }
          ],
          "events": [ {ts, topic_short, severity, is_active, ...} ],
          "archive_enabled": true/false,   # идёт ли запись СЕЙЧАС
          "cam_record_mode": "events" | "always" | "off",
        }
    """
    if not can_access_camera(camera_id):
        return jsonify({"error": "forbidden"}), 403

    cam = cameras.get(camera_id)
    if not cam:
        return jsonify({"error": "camera not found"}), 404

    date_str = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    try:
        day_start = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "date должен быть YYYY-MM-DD"}), 400

    day_start_ts = day_start.timestamp()
    day_end_ts = day_start_ts + 86400

    cam_root = archive_root() / camera_id
    cam_day_dir = cam_root / date_str

    # 1) Все даты, за которые есть папки (для календаря)
    dates = []
    if cam_root.is_dir():
        try:
            dates = sorted(
                (d.name for d in cam_root.iterdir()
                 if d.is_dir() and len(d.name) == 10),
                reverse=True,
            )
        except Exception:
            dates = []

    # 2) Сегменты за день (из файлов *.mp4)
    segments = []
    if cam_day_dir.is_dir():
        try:
            files = sorted(cam_day_dir.glob("*.mp4"), key=lambda f: f.name)
        except Exception:
            files = []

        for f in files:
            # Имя файла: "HH-MM-SS.mp4" или "HH-MM-SS_N.mp4"
            # первые 8 символов — время
            try:
                hhmmss = f.stem[:8].replace("-", ":")
                seg_start = datetime.strptime(
                    f"{date_str} {hhmmss}", "%Y-%m-%d %H:%M:%S"
                ).timestamp()
            except (ValueError, IndexError):
                continue

            try:
                st = f.stat()
                mtime = st.st_mtime
                size_mb = round(st.st_size / (1024 * 1024), 2)
            except Exception:
                mtime = seg_start
                size_mb = 0

            segments.append({
                "file": f.name,
                "url": f"/archive/{camera_id}/{date_str}/{f.name}",
                "start_ts": seg_start,
                "end_ts": mtime,   # эвристика: mtime ≈ момент закрытия файла
                "size_mb": size_mb,
            })

    # 3) Корректируем end_ts: не больше начала следующего сегмента
    for i in range(len(segments) - 1):
        if segments[i]["end_ts"] > segments[i + 1]["start_ts"]:
            segments[i]["end_ts"] = segments[i + 1]["start_ts"]

    # 4) События за день (маркеры на timeline)
    events_data = []
    em = current_app.config.get("EVENTS_MANAGER")
    if em:
        try:
            events_data = em.db.query(
                camera_id=camera_id,
                since_ts=day_start_ts,
                until_ts=day_end_ts,
                limit=10000,
            )
        except Exception as e:
            logger.warning("[%s] Ошибка чтения событий: %s", camera_id, e)

    # 5) Флаг «идёт ли запись сейчас» (для предупреждения в модалке)
    archive_on = (
        archive_storage.get("available", False)
        and archive_cfg.get("record_all", True)
    )
    cam_mode = getattr(cam, "record_mode", "off") or "off"
    archive_enabled = archive_on and cam_mode != "off"

    return jsonify({
        "ok": True,
        "camera_id": camera_id,
        "date": date_str,
        "dates": dates,
        "segments": segments,
        "events": events_data,
        "archive_enabled": archive_enabled,
        "cam_record_mode": cam_mode,
        "archive_storage": archive_storage,
    })


# ======================================================================
#  API: статус
# ======================================================================
@app.route("/api/status")
@auth_mod.login_required
def api_status():
    # Фильтр по доступу: viewer не должен видеть статус чужих камер
    visible_cam_ids = [cid for cid in cameras if can_access_camera(cid)]
    visible_recorder_ids = [cid for cid in recorders if can_access_camera(cid)]
    visible_loopback = [cid for cid in loopback_urls if can_access_camera(cid)]
    return jsonify({
        "app": {
            "name": APP_NAME,
            "version": __version__,
            "author": __author__,
        },
        "cameras": [
            cameras[cid].get_status() for cid in visible_cam_ids
        ],
        "recorders": {
            cid: recorders[cid].get_status() for cid in visible_recorder_ids
        },
        "mediamtx": {
            "running": mediamtx is not None,
            "port": mediamtx_cfg.get("rtsp_port", 8554),
            "loopback_cameras": visible_loopback,
        },
    })


# ======================================================================
#  API: системный мониторинг (CPU / RAM / диски / GPU)
# ======================================================================
# ------------ подсказка -----------------
# Главной странице нужно знать, есть ли сигнал: если raw-путь не поднят,
# MediaMTX в iframe отдаёт JSON {"status":"error",...} — некрасиво.
# ------------ подсказка -----------------
# Исходящих клонов может быть много, смотреть все не нужно — достаточно
# индикатора «жив/нет» и возможности открыть один для проверки.
@app.route("/api/streams/status", methods=["GET"])
@auth_mod.login_required
def api_streams_status():
    """Статус исходящих потоков (клонов): ready + число читателей."""
    out = []
    for st in streams_cfg:
        path = str(st.get("path") or "").strip().lstrip("/")
        item = {
            "id": st.get("id"),
            "camera_id": st.get("camera_id"),
            "path": path,
            "enabled": bool(st.get("enabled", True)),
            "ready": False,
            "readers": 0,
            "source": "",
        }
        if path and mediamtx is not None:
            try:
                info = mediamtx.get_path_status(path) or {}
                item["ready"] = bool(info.get("ready"))
                item["readers"] = len(info.get("readers") or [])
                src = info.get("source") or {}
                item["source"] = src.get("type", "") if isinstance(src, dict) else str(src)
            except Exception:
                pass
        out.append(item)
    return jsonify(out)


# ------------ подсказка -----------------
# Плитка грузит не MediaMTX напрямую, а нашу страницу-плеер: она знает,
# готов ли входящий путь, и не показывает JSON-ошибку MediaMTX.
@app.route("/player/<cam_id>")
@auth_mod.login_required
def player_page(cam_id: str):
    cam = cameras.get(cam_id)
    if cam is None:
        abort(404)

    token = auth_mod.get_mtx_token()
    username = ((g.user or session.get("user") or {}).get("username"))
    if not token and username:
        token = auth_mod.create_mtx_token(username)
        session["mtx_token"] = token

    host = request.host.split(":")[0]
    # WHEP-клиент сам отправляет Basic Auth; MediaMTX-страницу не используем.
    whep_url = f"http://{host}:8889/__raw_{cam_id}/whep"
    return render_template(
        "player.html",
        cam_id=cam_id,
        cam_name=cam.name,
        whep_url=whep_url,
        mtx_user=username or "",
        mtx_pass=token or "",
    )


@app.route("/api/cameras/status", methods=["GET"])
@auth_mod.login_required
def api_cameras_status():
    """Короткий статус камер для оверлея «Нет сигнала» на плитках."""
    out = {}
    for cid, cam in cameras.items():
        try:
            st = cam.get_status() or {}
            # get_status() возвращает словарь, а не объект
            item = {
                "connected": bool(st.get("connected")),
                "raw_ready": False,
                "error": st.get("last_error") or "",
            }
            # Превью показывает именно входящий raw-путь, поэтому важно
            # его состояние, а не подключение нашего RTSP-читателя.
            if mediamtx is not None:
                info = mediamtx.get_path_status(f"__raw_{cid}") or {}
                item["raw_ready"] = bool(info.get("ready"))
            out[cid] = item
        except Exception:
            out[cid] = {"connected": False, "raw_ready": False, "error": ""}
    return jsonify(out)


@app.route("/api/system/stats")
@auth_mod.login_required
def api_system_stats():
    """
    Возвращает текущие метрики системы:
        cpu:   {percent, cores_logical, cores_physical, freq_mhz}
        ram:   {total_mb, used_mb, available_mb, percent}
        disks: [{mount, label, fstype, total_gb, used_gb, free_gb, percent}]
        gpu:   [{name, vram_mb, utilization_percent}]
        gpu_ready: bool     (фоновый поток уже отработал)
        gpu_error: str|None
        platform: "Windows" / "Linux" / "Darwin"
        ts: float

    GPU-данные из кэша (фоновый поток обновляет каждые 5 сек),
    поэтому вызов мгновенный и не блокирует Flask.
    """
    return jsonify(get_system_stats())


# ======================================================================
#  Запуск
# ======================================================================
if __name__ == "__main__":
    # ---- MJPEG-дублирование ----
    duplicator = None
    if dup_cfg.get("enabled", False):
        duplicator = StreamDuplicator(cameras, dup_cfg, config)
        duplicator.start()
        logger.info("MJPEG-дублирование включено на порту %s", dup_cfg.get("port"))
    else:
        logger.info("MJPEG-дублирование выключено")

    host = server_cfg.get("host", "0.0.0.0")
    port = int(server_cfg.get("port", 8100))
    logger.info("Основной сервер: http://%s:%s", host, port)

    # Warm-up системного мониторинга: запускает фоновый поток GPU,
    # чтобы к первому запросу /api/system/stats данные уже были готовы.
    # Сам вызов возвращает CPU/RAM/диски, GPU появится через 3–7 сек.
    try:
        _warm = get_system_stats()
        logger.info(
            "system_monitor: warm-up OK (cpu=%.0f%%, ram=%.0f%%, "
            "дисков=%d, GPU готов: %s)",
            _warm["cpu"]["percent"] or 0,
            _warm["ram"]["percent"] or 0,
            len(_warm["disks"]),
            _warm["gpu_ready"],
        )
    except Exception:
        logger.exception("system_monitor: warm-up упал (не критично)")
    logger.info("Архив: %s", archive_root())
    if loopback_urls:
        logger.info("Loopback-камеры: %s", list(loopback_urls.keys()))

    # ---- ONVIF-прокси ----
    onvif_proxy_cfg = config.get("onvif_proxy", {}) or {}
    onvif_thread = None
    if onvif_proxy_cfg.get("enabled", False):
        import threading as _threading
        from flask import Flask as _Flask

        onvif_port = int(onvif_proxy_cfg.get("port", 8081))
        onvif_host = onvif_proxy_cfg.get("host", "0.0.0.0")

        # Второй экземпляр Flask ТОЛЬКО для ONVIF
        onvif_app = _Flask("onvif_proxy_app")
        onvif_app.secret_key = app.secret_key
        # Копируем общие данные, чтобы ONVIF-хендлеры их видели
        onvif_app.config["CAMERAS"] = cameras
        onvif_app.config["RECORDERS"] = recorders
        onvif_app.config["VHS_CONFIG"] = config
        onvif_app.config["CONFIG_PATH"] = str(CONFIG_PATH)
        onvif_app.config["MEDIAMTX"] = mediamtx
        onvif_app.config["MEDIAMTX_HELPERS"] = app.config["MEDIAMTX_HELPERS"]
        onvif_app.config["LOOPBACK_URLS"] = loopback_urls
        onvif_app.config["EVENTS_MANAGER"] = events_manager
        onvif_app.register_blueprint(onvif_bp)

        def _run_onvif():
            logger.info("ONVIF-прокси на http://%s:%s", onvif_host, onvif_port)
            onvif_app.run(
                host=onvif_host,
                port=onvif_port,
                threaded=True,
                debug=False,
                use_reloader=False,
            )

        onvif_thread = _threading.Thread(
            target=_run_onvif,
            name="onvif-proxy",
            daemon=True,
        )
        onvif_thread.start()
    else:
        logger.info("ONVIF-прокси выключен")
    # ---- Запуск и корректное завершение ----
    try:
        app.run(host=host, port=port, threaded=True, debug=False)
    finally:
        logger.info("Останавливаю компоненты...")

        # 1. Менеджер событий (работает с камерами)
        if events_manager:
            try:
                events_manager.stop()
                logger.info("EventsManager остановлен")
            except Exception:
                logger.exception("Ошибка остановки EventsManager")

        # 2. Рекордеры (пишут в архив)
        for rec in recorders.values():
            try:
                rec.stop()
            except Exception:
                pass

        # 3. Камеры (читают raw-потоки)
        # MotionDetector не имеет своего потока — он синхронный,
        # снимать подписку не нужно: вместе с камерой умирает и callback.
        for cam in cameras.values():
            try:
                cam.stop()
            except Exception:
                pass

        # 4. MJPEG-дублирование
        if duplicator:
            try:
                duplicator.stop()
            except Exception:
                pass

        # 5. MediaMTX (последним — источник)
        if mediamtx:
            try:
                mediamtx.stop()
            except Exception:
                pass

        # 6. Добиваем всё, что могло остаться: ffmpeg-публикаторы MediaMTX,
        #    рекордеры, процессы от прошлых падений.
        try:
            from proc_tree import kill_leftovers
            killed = kill_leftovers(BOOTSTRAP_BIN_DIR)
            if killed:
                logger.info("Остановлено дочерних процессов: %d", len(killed))
        except Exception:
            logger.exception("Ошибка зачистки процессов при остановке")

        logger.info("Сервер остановлен")
