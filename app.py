"""
VHS-Streamer — главный сервер.
"""
import logging
import platform
import sys
import time
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

import yaml
from flask import (
    Flask, render_template, Response, abort,
    request, redirect, url_for, jsonify, g, session, send_from_directory,
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
from duplicator import StreamDuplicator
import auth as auth_mod
from settings_api import settings_bp
from streams_api import streams_bp
from diagnostics import diagnostics_bp
from mediamtx_manager import MediaMTXManager
import mediamtx_manager as _mtx_helpers
from preview import get_loopback_url, raw_path_name


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

for cam_cfg in cameras_cfg:
    try:
        cfg = dict(cam_cfg)
        if cam_cfg["id"] in loopback_urls:
            cfg["preview_source"] = loopback_urls[cam_cfg["id"]]

        cam = Camera(cfg)
        cameras[cam.id] = cam
        cam.start()

        record_enabled = (
            archive_cfg.get("enabled", False)
            and archive_cfg.get("record_all", True)
            and cam_cfg.get("record", True)
        )
        if record_enabled:
            rec = Recorder(cam, archive_cfg, cam_cfg)
            rec.start()
            recorders[cam.id] = rec
    except Exception as e:
        logger.exception("Не удалось инициализировать камеру %s: %s",
                         cam_cfg.get("id"), e)


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
app.config["MEDIAMTX_HELPERS"] = {
    "build_raw_path_config": _mtx_helpers.build_raw_path_config,
    "build_stream_path_config": _mtx_helpers.build_stream_path_config,
}
app.config["LOOPBACK_URLS"] = loopback_urls

app.register_blueprint(settings_bp)
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
    cam_statuses = [c.get_status() for c in cameras.values()]
    rec_statuses = {cid: r.get_status() for cid, r in recorders.items()}

    total_archive_bytes = 0
    for r in recorders.values():
        try:
            total_archive_bytes += r.get_status()["archive_size_mb"] * 1024 * 1024
        except Exception:
            pass

    sysinfo = {
        "cameras_total": len(cameras),
        "cameras_online": sum(1 for s in cam_statuses if s["connected"]),
        "recorders": len(recorders),
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
        cameras=cameras.values(),
        rec_statuses=rec_statuses,
        sysinfo=sysinfo,
        user=g.user,
    )


# ======================================================================
#  MJPEG
# ======================================================================
@app.route("/video_feed/<camera_id>")
@auth_mod.login_required
def video_feed(camera_id: str):
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
    root = archive_root() / camera_id / date
    if not root.is_dir():
        abort(404)
    return send_from_directory(root, filename, as_attachment=False, conditional=True)


# ======================================================================
#  API: статус
# ======================================================================
@app.route("/api/status")
@auth_mod.login_required
def api_status():
    return jsonify({
        "app": {
            "name": APP_NAME,
            "version": __version__,
            "author": __author__,
        },
        "cameras": [c.get_status() for c in cameras.values()],
        "recorders": {cid: r.get_status() for cid, r in recorders.items()},
        "mediamtx": {
            "running": mediamtx is not None,
            "port": mediamtx_cfg.get("rtsp_port", 8554),
            "loopback_cameras": list(loopback_urls.keys()),
        },
    })


# ======================================================================
#  Запуск
# ======================================================================
if __name__ == "__main__":
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
    logger.info("Архив: %s", archive_root())
    if loopback_urls:
        logger.info("Loopback-камеры: %s", list(loopback_urls.keys()))

    try:
        app.run(host=host, port=port, threaded=True, debug=False)
    finally:
        logger.info("Останавливаю компоненты...")
        for rec in recorders.values():
            try:
                rec.stop()
            except Exception:
                pass
        for cam in cameras.values():
            try:
                cam.stop()
            except Exception:
                pass
        if duplicator:
            try:
                duplicator.stop()
            except Exception:
                pass
        if mediamtx:
            try:
                mediamtx.stop()
            except Exception:
                pass
        logger.info("Сервер остановлен")