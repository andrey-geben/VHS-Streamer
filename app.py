"""
VHS-Streamer — главный сервер.
"""
import logging
import platform
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
    logger.error("MediaMTX/FFmpeg не установлены. Запустите: python bootstrap.py")
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


config = load_config()
server_cfg = config.get("server", {})
security_cfg = config.get("security", {})
archive_cfg = config.get("archive", {})
dup_cfg = config.get("duplicate_stream", {})
mediamtx_cfg = config.get("mediamtx", {})
cameras_cfg = config.get("cameras", [])
streams_cfg = config.get("streams", [])


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

        # Архив включён глобально?
        archive_on = (
            archive_cfg.get("enabled", False)
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
app = Flask(
    __name__,
    template_folder=str(RES_DIR / "templates"),
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

app.register_blueprint(settings_bp)
app.register_blueprint(onvif_bp)
app.register_blueprint(streams_bp)
app.register_blueprint(diagnostics_bp)


# ======================================================================
#  Хелперы для шаблонов
# ======================================================================
def archive_root() -> Path:
    return Path(config.get("archive", {}).get("path", "./archive")).resolve()


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
#  Аутентификация
# ======================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if auth_mod.do_login(u, p):
            return redirect(request.args.get("next") or url_for("index"))
        error = "Неверный логин или пароль"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    auth_mod.do_logout()
    return redirect(url_for("login"))


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
        info={
            "version": __version__,
            "author": __author__,
            "copyright": __copyright__,
            "license": __license__,
            "url": __url__,
        },
    )


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

    return render_template(
        "index.html",
        cameras=visible_cams,
        rec_statuses=rec_statuses,
        sysinfo=sysinfo,
        user=g.user,
        # A.4: креды для MediaMTX (WHEP из iframe)
        mtx_user=(g.user.get("username") if g.user else None),
        mtx_token=auth_mod.get_mtx_token(),
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
        logger.info(
            "mediamtx_auth DENY: no creds (ip=%r protocol=%r path=%r)",
            ip, protocol, path,
        )
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
    archive_cfg = config.get("archive", {})
    archive_on = (
        archive_cfg.get("enabled", False)
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
    port = int(server_cfg.get("port", 8000))
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

        logger.info("Сервер остановлен")