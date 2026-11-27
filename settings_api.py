"""
API управления конфигурацией + страница настроек.

Все изменения пишутся в config.yaml и применяются «на лету» там,
где это возможно. Изменения, требующие перезапуска, помечаются флагом
`restart_required: true` в ответе API — UI показывает предупреждение.

Секреты (пароли пользователей, API-токен):
    - отдаются ТОЛЬКО администратору (роль 'admin');
    - обычный viewer получает их замаскированными ('***');
    - менять их можно только через /api/security/config (тоже admin-only).

Дополнительно:
    - шаблоны RTSP-путей по вендорам (Hikvision, Dahua, Reolink, Axis);
    - индивидуальный путь архива для каждой камеры.
"""
from auth import login_required, admin_required, can_access_camera
from camera import Camera
from recorder import Recorder
from event_recorder import EventRecorder
import camera_templates as ct
import copy
import logging
import os
import platform
import re
import secrets
import shutil
from pathlib import Path

import yaml
from flask import (
    Blueprint, jsonify, request, render_template, current_app, g,
)

from auth import login_required, admin_required, can_access_camera
from netdetect import build_endpoints
from recorder import Recorder
from event_recorder import EventRecorder
import camera_templates as ct

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

settings_bp = Blueprint("settings", __name__)


# ======================================================================
# Хелперы
# ======================================================================
def _state():
    return {
        "cameras": current_app.config["CAMERAS"],
        "recorders": current_app.config["RECORDERS"],
        "config": current_app.config["VHS_CONFIG"],
        "config_path": Path(current_app.config["CONFIG_PATH"]),
    }


def _is_admin() -> bool:
    return getattr(g, "user", {}).get("role") == "admin"


def _save_config(cfg: dict, path: Path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        logger.info("Конфиг сохранён: %s", path)
    except Exception as e:
        logger.error("Не удалось сохранить конфиг: %s", e)
        raise


def _archive_storage_status():
    """Проверяет, что архив указывает на подключённый внешний носитель."""
    st = _state()
    cfg = st["config"].get("archive", {})
    raw = str(cfg.get("path") or "").strip()
    base = Path(st["config_path"]).parent.resolve()
    result = {
        "configured": raw, "path": "", "available": False,
        "external": False, "writable": False, "free_gb": 0, "reason": "",
    }
    if not raw:
        result["reason"] = "Путь к внешнему архиву не задан"
        return result
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    result["path"] = str(path)
    if base == path or base in path.parents:
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


def _serialize_config(cfg: dict, is_admin: bool) -> dict:
    c = copy.deepcopy(cfg)
    if is_admin:
        return c
    # viewer: убираем камеры, к которым нет доступа
    c["cameras"] = [
        cam for cam in c.get("cameras", [])
        if can_access_camera(cam.get("id"))
    ]
    # маскируем пароли и токен
    sec = c.get("security", {})
    for u in sec.get("users", []):
        if "password" in u:
            u["password"] = "***"
    if "api_token" in sec:
        sec["api_token"] = "***"
    return c


def _normalize_analog_source(source, camera_id: str):
    """
    Приводит source аналоговой камеры к нужному типу.

    Linux:   int (индекс) или строка "/dev/videoN"
    Windows: строка с именем dshow ("HD User Facing")
             ИЛИ int (индекс).

    Возвращает (value, error_message).
    """
    if source is None or source == "":
        return None, f"Камера '{camera_id}': source не может быть пустым"

    if isinstance(source, int):
        return source, None
    if isinstance(source, str):
        s = source.strip()
        if s.isdigit():
            return int(s), None
        if s.startswith("/dev/"):
            return s, None
        if IS_WINDOWS:
            return s, None
        return None, (
            f"Камера '{camera_id}': для Linux source должен быть числом "
            f"(индекс) или путём /dev/videoN. Получено: {source!r}"
        )

    return None, f"Камера '{camera_id}': неверный тип source: {type(source).__name__}"


def _validate_camera(payload: dict) -> tuple[bool, str]:
    """Проверяет корректность данных камеры. Возвращает (ok, error_message)."""
    required = ["id", "name", "type", "source"]
    for field in required:
        if field not in payload or payload[field] in (None, ""):
            return False, f"Поле '{field}' обязательно"
    if payload["type"] not in ("ip", "analog"):
        return False, "type должен быть 'ip' или 'analog'"
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", str(payload["id"])):
        return False, "id может содержать только буквы, цифры, '_' и '-'"

    if payload["type"] == "analog":
        _, err = _normalize_analog_source(payload["source"], payload["id"])
        if err:
            return False, err

    return True, ""




def _camera_to_dict(cam: Camera) -> dict:
    return {
        "id": cam.id,
        "name": cam.name,
        "type": cam.type,
        "source": cam.source,
        "capture_index": cam.capture_index,
        "fps": cam.fps,
        "width": cam.width,
        "height": cam.height,
        "transport": cam.transport,
        "open_timeout_ms": cam.open_timeout_ms,
        "read_timeout_ms": cam.read_timeout_ms,
        "has_audio": cam.has_audio,
        "audio": cam.audio,
        "archive_path": getattr(cam, "archive_path", ""),
        "record": getattr(cam, "record", False),
        "record_mode": getattr(cam, "record_mode", "off"),
        "event_record_duration": getattr(cam, "event_record_duration", 30),
        "event_topics": getattr(cam, "event_topics", []),
        "motion_detection_enabled": getattr(
            cam, "motion_detection_enabled", True
        ),
        "motion_min_area": getattr(cam, "motion_min_area", 500),
        "connected": cam._connected,
        "last_error": cam._last_error,
    }


def _find_camera_cfg(cfg: dict, cam_id: str) -> dict | None:
    for c in cfg.get("cameras", []):
        if c.get("id") == cam_id:
            return c
    return None


def _attach_motion_detector(cam: Camera, cam_cfg: dict, replace: bool = False):
    """
    Подписать MotionDetector на камеру.
    replace=True — сначала detach (для update: пересоздать с новыми настройками).
    Если mgr не запущен (motion_detection выключен/отсутствует в config.yaml) —
    тихо пропускает.
    """
    mgr = current_app.config.get("MOTION_DETECTOR_MGR")
    if mgr is None:
        return
    try:
        if replace:
            mgr.detach_camera(cam.id)
        mgr.attach_camera(cam.id, cam_cfg)
        cam.subscribe(
            lambda frame, cid=cam.id: mgr.process_frame(cid, frame)
        )
    except Exception as e:
        logger.exception(
            "[%s] Не удалось подписать MotionDetector: %s", cam.id, e,
        )


def _detach_motion_detector(camera_id: str):
    """Отписать MotionDetector от камеры (при удалении)."""
    mgr = current_app.config.get("MOTION_DETECTOR_MGR")
    if mgr is None:
        return
    try:
        mgr.detach_camera(camera_id)
    except Exception as e:
        logger.exception(
            "[%s] Не удалось отписать MotionDetector: %s", camera_id, e,
        )


def _build_camera_cfg_from_payload(payload: dict, cam_id: str) -> tuple[dict, str]:
    """
    Собирает словарь конфига камеры из payload.
    Возвращает (cfg, error_message). При ошибке cfg пустой.
    """
    ctype = payload["type"]
    raw_source = payload["source"]

    if ctype == "analog":
        source, err = _normalize_analog_source(raw_source, cam_id)
        if err:
            return {}, err
    else:
        source = str(raw_source).strip()
        if not source:
            return {}, "source IP-камеры не может быть пустым"

    # capture_index — только для аналоговых
    capture_index = None
    if ctype == "analog":
        ci = payload.get("capture_index")
        if ci not in (None, "", "null"):
            try:
                capture_index = int(ci)
                if capture_index < 0:
                    return {}, "capture_index должен быть >= 0"
            except (TypeError, ValueError):
                return {}, "capture_index должен быть целым числом"

    # Аудио — только для IP-камер
    has_audio = None
    audio = False
    if ctype == "ip":
        ha = payload.get("has_audio")
        if ha in (True, False):
            has_audio = bool(ha)
        audio = bool(payload.get("audio", False)) and (has_audio is True)

    # Режим записи: off / always / events
    record_mode = str(payload.get("record_mode") or "").strip()
    if record_mode not in ("off", "always", "events"):
        # Поддержка старого формата: record: true/false
        if payload.get("record") is True:
            record_mode = "always"
        else:
            record_mode = "off"

    # Совместимость: record = (режим != off)
    record = (record_mode != "off")

    # Индивидуальный путь архива — только если включена запись
    archive_path = ""
    if record:
        ap = payload.get("archive_path")
        if ap:
            archive_path = str(ap).strip()

    # Параметры записи по событиям
    try:
        event_record_duration = int(
            payload.get("event_record_duration", 30)
        )
        if event_record_duration < 5:
            event_record_duration = 5
        if event_record_duration > 3600:
            event_record_duration = 3600
    except (TypeError, ValueError):
        event_record_duration = 30

    raw_topics = payload.get("event_topics")
    if isinstance(raw_topics, list):
        allowed = {"motion", "input", "tamper", "storage", "network"}
        event_topics = [t for t in raw_topics if t in allowed]
    else:
        event_topics = ["motion", "input", "tamper"]
    if not event_topics:
        event_topics = ["motion"]

    # Motion detection (OpenCV) — per-camera override
    mde_raw = payload.get("motion_detection_enabled")
    if mde_raw is None:
        motion_detection_enabled = True
    else:
        motion_detection_enabled = bool(mde_raw)

    try:
        motion_min_area = int(payload.get("motion_min_area", 500))
        if motion_min_area < 100:
            motion_min_area = 100
        if motion_min_area > 5000:
            motion_min_area = 5000
    except (TypeError, ValueError):
        motion_min_area = 500

    cfg = {
        "id": cam_id,
        "name": str(payload["name"]).strip(),
        "type": ctype,
        "source": source,
        "capture_index": capture_index,
        "fps": int(payload.get("fps", 25)),
        "width": int(payload.get("width", 640)),
        "height": int(payload.get("height", 480)),
        "record": record,
        "archive_path": archive_path,
        "record_mode": record_mode,
        "event_record_duration": event_record_duration,
        "event_topics": event_topics,
        "transport": payload.get("transport") or None,
        "open_timeout_ms": int(payload.get("open_timeout_ms", 5000)),
        "read_timeout_ms": int(payload.get("read_timeout_ms", 5000)),
                "has_audio": has_audio,
        "audio": audio,
        "motion_detection_enabled": motion_detection_enabled,
        "motion_min_area": motion_min_area,
    }
    return cfg, ""


# ======================================================================
# Страница настроек (HTML)
# ======================================================================
@settings_bp.route("/settings")
@login_required
def settings_page():
    return render_template("settings.html", user=g.user)


# ======================================================================
# API: полный конфиг (секреты — только admin)
# ======================================================================
@settings_bp.route("/api/config", methods=["GET"])
@login_required
def api_get_config():
    st = _state()
    return jsonify(_serialize_config(st["config"], _is_admin()))


# ======================================================================
# API: безопасность (только admin)
# ======================================================================
@settings_bp.route("/api/security/config", methods=["GET"])
@admin_required
def api_get_security():
    st = _state()
    sec = st["config"].get("security", {})
    return jsonify({
        "users": copy.deepcopy(sec.get("users", [])),
        "api_token": sec.get("api_token", ""),
        "auth_enabled": sec.get("auth_enabled", True),
    })


@settings_bp.route("/api/security/config", methods=["POST"])
@admin_required
def api_set_security():
    st = _state()
    payload = request.get_json(silent=True) or {}

    users = payload.get("users", [])
    if not isinstance(users, list):
        return jsonify({"error": "users должен быть списком"}), 400

    cleaned_users: list[dict] = []
    seen_names: set[str] = set()
    for i, u in enumerate(users):
        if not isinstance(u, dict):
            return jsonify({"error": f"Пользователь #{i+1} — не объект"}), 400

        username = str(u.get("username", "")).strip()
        password = str(u.get("password", ""))
        role = str(u.get("role", "viewer")).strip()

        if not username:
            return jsonify({"error": f"У пользователя #{i+1} пустой логин"}), 400
        if not password:
            return jsonify({"error": f"У пользователя '{username}' пустой пароль"}), 400
        if role not in ("admin", "viewer"):
            return jsonify({"error": f"Недопустимая роль '{role}' у '{username}'"}), 400
        if username in seen_names:
            return jsonify({"error": f"Логин '{username}' повторяется"}), 400

        seen_names.add(username)

        # Валидация поля cameras:
        #   отсутствует / null  → None  (все камеры, обратная совместимость)
        #   []                  → []    (ни одной)
        #   ["*"]               → все
        #   ["id1", "id2"]      → только эти
        raw_cameras = u.get("cameras")
        if raw_cameras is None:
            cameras_list = None
        elif isinstance(raw_cameras, list):
            cameras_list = []
            for c in raw_cameras:
                if not isinstance(c, str):
                    return jsonify({
                        "error": f"cameras у '{username}': "
                                 f"элементы должны быть строками"
                    }), 400
                c = c.strip()
                if c and c not in cameras_list:
                    cameras_list.append(c)
        else:
            return jsonify({
                "error": f"cameras у '{username}': "
                         f"должен быть массивом или null"
            }), 400

        entry = {
            "username": username,
            "password": password,
            "role": role,
        }
        if cameras_list is not None:
            entry["cameras"] = cameras_list
        cleaned_users.append(entry)

    if not cleaned_users:
        return jsonify({"error": "Список пользователей не может быть пустым"}), 400
    if not any(u["role"] == "admin" for u in cleaned_users):
        return jsonify({"error": "Должен остаться хотя бы один администратор"}), 400

    api_token = payload.get("api_token")
    warning = None
    if api_token is not None:
        api_token = str(api_token).strip()
        if not api_token:
            warning = "API-токен пуст — аутентификация по токену отключена."

    sec = st["config"].setdefault("security", {})
    sec["users"] = cleaned_users
    if api_token is not None:
        sec["api_token"] = api_token

    _save_config(st["config"], st["config_path"])
    logger.info("Настройки безопасности обновлены (пользователей: %d)",
                len(cleaned_users))

    resp = {"ok": True, "users_count": len(cleaned_users)}
    if warning:
        resp["warning"] = warning
    return jsonify(resp)


@settings_bp.route("/api/security/generate_token", methods=["POST"])
@admin_required
def api_generate_token():
    return jsonify({"token": secrets.token_urlsafe(32)})
@settings_bp.route("/api/security/generate_password", methods=["POST"])
@admin_required
def api_generate_password():
    """Генерирует случайный пароль для нового пользователя."""
    return jsonify({"password": secrets.token_urlsafe(12)})

# ======================================================================
# API: камеры
# ======================================================================
@settings_bp.route("/api/cameras", methods=["GET"])
@login_required
def api_list_cameras():
    st = _state()
    return jsonify([
        _camera_to_dict(c) for c in st["cameras"].values()
        if can_access_camera(c.id)
    ])


@settings_bp.route("/api/cameras/list", methods=["GET"])
@login_required
def api_list_cameras_alias():
    """Стабильный alias для внешних интеграций (VHS-Security)."""
    return api_list_cameras()


@settings_bp.route("/api/cameras/<camera_id>", methods=["GET"])
@login_required
def api_get_camera(camera_id: str):
    if not can_access_camera(camera_id):
        return jsonify({"error": "forbidden"}), 403
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404
    return jsonify(_camera_to_dict(cam))


@settings_bp.route("/api/cameras", methods=["POST"])
@admin_required
def api_add_camera():
    st = _state()
    payload = request.get_json(silent=True) or {}

    ok, err = _validate_camera(payload)
    if not ok:
        return jsonify({"error": err}), 400

    cam_id = payload["id"]
    if cam_id in st["cameras"]:
        return jsonify({"error": f"Камера '{cam_id}' уже существует"}), 409

    new_cfg, err = _build_camera_cfg_from_payload(payload, cam_id)
    if err:
        return jsonify({"error": err}), 400

    # Loopback URL — если камера уже под MediaMTX
    loopback_urls = current_app.config.get("LOOPBACK_URLS", {})
    if cam_id in loopback_urls:
        new_cfg["preview_source"] = loopback_urls[cam_id]

    try:
        cam = Camera(new_cfg)
        cam.start()
    except Exception as e:
        logger.exception("Ошибка создания камеры")
        return jsonify({"error": f"Не удалось создать камеру: {e}"}), 500

    st["cameras"][cam_id] = cam

    # ------------ подсказка -----------------
    # Входящий raw-путь создаём сразу: иначе превью новой камеры
    # появляется только после прохода сторожа (до 15 секунд).
    try:
        from mediamtx_manager import build_raw_path_config
        from preview import raw_path_name
        mtx = st.get("mediamtx")
        if mtx is not None:
            mtx.add_path(raw_path_name(cam_id), build_raw_path_config(cam))
            logger.info("[%s] входящий raw-путь создан сразу", cam_id)
            # сохраняем в mediamtx.yml, чтобы путь пережил перезапуск MediaMTX
            try:
                from streams_api import _sync_mediamtx_auth
                mtx_path = current_app.config.get("MEDIAMTX_CONFIG_PATH")
                if mtx_path:
                    _sync_mediamtx_auth(st["config"], mtx_path)
            except Exception as exc:
                logger.warning("[%s] путь не сохранён в mediamtx.yml: %s", cam_id, exc)
    except Exception as exc:
        logger.warning("[%s] raw-путь сразу не создан: %s", cam_id, exc)

    archive_cfg = st["config"].get("archive", {})
    mode = new_cfg.get("record_mode", "off")
    archive_on = (
            _archive_storage_status().get("available", False)
            and archive_cfg.get("record_all", True)
    )

    if archive_on and mode == "always":
        try:
            rec = Recorder(cam, archive_cfg, new_cfg)
            rec.start()
            st["recorders"][cam_id] = rec
            logger.info("[%s] Режим записи: постоянная", cam_id)
        except Exception:
            logger.exception("[%s] Ошибка создания Recorder", cam_id)
    elif archive_on and mode == "events":
        em = current_app.config.get("EVENTS_MANAGER")
        if not em:
            logger.warning(
                "[%s] Режим 'events', но EventsManager не запущен", cam_id,
            )
        else:
            try:
                rec = EventRecorder(cam, archive_cfg, new_cfg, em)
                rec.start()
                st["recorders"][cam_id] = rec
                logger.info("[%s] Режим записи: по событиям", cam_id)
            except Exception:
                logger.exception("[%s] Ошибка создания EventRecorder", cam_id)
    else:
        logger.info("[%s] Запись в архив отключена", cam_id)

        st["config"].setdefault("cameras", []).append(new_cfg)
        _save_config(st["config"], st["config_path"])

        _attach_motion_detector(cam, new_cfg, replace=False)

        return jsonify({"ok": True, "camera": _camera_to_dict(cam)}), 201

@settings_bp.route("/api/cameras/<camera_id>", methods=["PUT"])
@admin_required
def api_update_camera(camera_id: str):
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json(silent=True) or {}
    payload["id"] = camera_id
    ok, err = _validate_camera(payload)
    if not ok:
        return jsonify({"error": err}), 400

    new_cfg, err = _build_camera_cfg_from_payload(payload, camera_id)
    if err:
        return jsonify({"error": err}), 400

    # Останавливаем старый рекордер и камеру
    old_rec = st["recorders"].pop(camera_id, None)
    if old_rec:
        old_rec.stop()
    cam.stop()

    old_cfg = _find_camera_cfg(st["config"], camera_id)
    if old_cfg:
        st["config"]["cameras"].remove(old_cfg)

    # Loopback URL — если камера под MediaMTX
    loopback_urls = current_app.config.get("LOOPBACK_URLS", {})
    if camera_id in loopback_urls:
        new_cfg["preview_source"] = loopback_urls[camera_id]

    try:
        new_cam = Camera(new_cfg)
        new_cam.start()
    except Exception as e:
        return jsonify({"error": f"Не удалось создать камеру: {e}"}), 500

    st["cameras"][camera_id] = new_cam

    archive_cfg = st["config"].get("archive", {})
    archive_on = (
            _archive_storage_status().get("available", False)
            and archive_cfg.get("record_all", True)
    )
    mode = new_cfg.get("record_mode", "off")

    if archive_on and mode == "always":
        try:
            rec = Recorder(new_cam, archive_cfg, new_cfg)
            rec.start()
            st["recorders"][camera_id] = rec
            logger.info("[%s] Режим записи: постоянная", camera_id)
        except Exception:
            logger.exception("[%s] Ошибка создания Recorder", camera_id)
    elif archive_on and mode == "events":
        em = current_app.config.get("EVENTS_MANAGER")
        if not em:
            logger.warning(
                "[%s] Режим 'events', но EventsManager не запущен",
                camera_id,
            )
        else:
            try:
                rec = EventRecorder(new_cam, archive_cfg, new_cfg, em)
                rec.start()
                st["recorders"][camera_id] = rec
                logger.info("[%s] Режим записи: по событиям", camera_id)
            except Exception:
                logger.exception(
                    "[%s] Ошибка создания EventRecorder", camera_id,
                )
    else:
        logger.info("[%s] Запись в архив отключена", camera_id)

    st["config"].setdefault("cameras", []).append(new_cfg)
    _save_config(st["config"], st["config_path"])

    # replace=True: detach старый детектор + attach новый (с настройками)
    _attach_motion_detector(new_cam, new_cfg, replace=True)

    return jsonify({"ok": True, "camera": _camera_to_dict(new_cam)})


@settings_bp.route("/api/cameras/<camera_id>", methods=["DELETE"])
@admin_required
def api_delete_camera(camera_id: str):
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404

    rec = st["recorders"].pop(camera_id, None)
    if rec:
        rec.stop()
    cam.stop()

    del st["cameras"][camera_id]

    old_cfg = _find_camera_cfg(st["config"], camera_id)
    if old_cfg:
        st["config"]["cameras"].remove(old_cfg)
        _save_config(st["config"], st["config_path"])

    _detach_motion_detector(camera_id)

    return jsonify({"ok": True})


@settings_bp.route("/api/cameras/<camera_id>/restart", methods=["POST"])
@admin_required
def api_restart_camera(camera_id: str):
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404
    cam.stop()
    cam.start()
    return jsonify({"ok": True})


# ======================================================================
# API: обнаружение локальных устройств захвата
# (веб-камеры, USB/PCI-карты — не IP, не RTSP)
# ======================================================================
@settings_bp.route("/api/devices/local", methods=["GET"])
@login_required
def api_list_local_devices():
    """
    Обнаруживает локальные устройства захвата видео на машине сервера.
    Windows: DirectShow через FFmpeg -list_devices.
    Linux:   /dev/video* (+ v4l2-ctl для имени, если установлен).
    macOS:   пустой список (не поддерживается в задаче №1).

    Импорт device_discovery — внутри функции, чтобы ошибка импорта
    (например, отсутствует FFmpeg) не валила весь settings_bp.
    """
    try:
        from device_discovery import discover_local_devices
        devices = discover_local_devices()
    except Exception as e:
        logger.exception("Ошибка обнаружения локальных устройств")
        return jsonify({
            "error": f"Не удалось обнаружить устройства: {e}",
            "devices": [],
        }), 500

    logger.info("Обнаружение локальных устройств: найдено %d", len(devices))
    return jsonify({"devices": devices})



# ======================================================================
# API: проверка наличия аудио в потоке IP-камеры
# ======================================================================
@settings_bp.route("/api/cameras/<camera_id>/probe_audio", methods=["POST"])
@admin_required
def api_probe_audio(camera_id: str):
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404
    if cam.type != "ip":
        return jsonify({"error": "Проверка звука доступна только для IP-камер"}), 400

    from audio_probe import probe_audio

    logger.info("[%s] Проверяю наличие аудио в потоке...", camera_id)
    has_audio = probe_audio(cam.source)
    logger.info("[%s] Результат проверки аудио: has_audio=%s",
                camera_id, has_audio)

    cam.has_audio = has_audio
    if not has_audio:
        cam.audio = False

    cfg = _find_camera_cfg(st["config"], camera_id)
    if cfg:
        cfg["has_audio"] = has_audio
        if not has_audio:
            cfg["audio"] = False
        _save_config(st["config"], st["config_path"])

    return jsonify({
        "ok": True,
        "has_audio": has_audio,
        "audio": cam.audio,
    })


# ======================================================================
# API: архив
# ======================================================================
@settings_bp.route("/api/archive/config", methods=["GET"])
@login_required
def api_get_archive():
    st = _state()
    data = dict(st["config"].get("archive", {}))
    data["storage"] = _archive_storage_status()
    return jsonify(data)


@settings_bp.route("/api/archive/config", methods=["POST"])
@admin_required
def api_set_archive():
    st = _state()
    payload = request.get_json(silent=True) or {}

    archive = st["config"].setdefault("archive", {})
    for field in ("path", "segment_minutes", "max_size_gb",
                  "max_age_days", "cleanup_interval_sec",
                  "codec", "extension", "enabled", "record_all"):
        if field in payload:
            archive[field] = payload[field]

    _save_config(st["config"], st["config_path"])
    storage = _archive_storage_status()

    return jsonify({
        "ok": True,
        "restart_required": "path" in payload,
        "archive": archive,
        "storage": storage,
    })


# ======================================================================
# API: дублирование MJPEG на отдельный порт
# ======================================================================
@settings_bp.route("/api/duplicate/config", methods=["GET"])
@login_required
def api_get_duplicate():
    st = _state()
    return jsonify(st["config"].get("duplicate_stream", {}))


@settings_bp.route("/api/duplicate/config", methods=["POST"])
@admin_required
def api_set_duplicate():
    st = _state()
    payload = request.get_json(silent=True) or {}

    dup = st["config"].setdefault("duplicate_stream", {})
    for field in ("enabled", "port", "host", "require_auth"):
        if field in payload:
            dup[field] = payload[field]

    _save_config(st["config"], st["config_path"])

    return jsonify({
        "ok": True,
        "restart_required": True,
        "duplicate_stream": dup,
    })


# ======================================================================
# API: рекордеры (start/stop отдельной камеры)
# ======================================================================
@settings_bp.route("/api/recorders/<camera_id>/start", methods=["POST"])
@admin_required
def api_start_recorder(camera_id: str):
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "camera not found"}), 404
    if camera_id in st["recorders"]:
        return jsonify({"ok": True, "message": "уже запущен"})

    archive_cfg = st["config"].get("archive", {})
    cam_cfg = _find_camera_cfg(st["config"], camera_id) or {}
    rec = Recorder(cam, archive_cfg, cam_cfg)
    rec.start()
    st["recorders"][camera_id] = rec
    return jsonify({"ok": True})


@settings_bp.route("/api/recorders/<camera_id>/stop", methods=["POST"])
@admin_required
def api_stop_recorder(camera_id: str):
    st = _state()
    rec = st["recorders"].pop(camera_id, None)
    if not rec:
        return jsonify({"error": "not running"}), 404
    rec.stop()
    return jsonify({"ok": True})


# ======================================================================
# API: шаблоны вендоров IP-камер
# ======================================================================
@settings_bp.route("/api/camera_templates/vendors", methods=["GET"])
@login_required
def api_camera_vendors():
    """Список поддерживаемых вендоров с потоками."""
    try:
        return jsonify(ct.list_vendors_for_api())
    except Exception as e:
        logger.exception("Ошибка загрузки шаблонов")
        return jsonify({"error": str(e)}), 500


@settings_bp.route("/api/camera_templates/detect", methods=["POST"])
@login_required
def api_camera_detect_vendor():
    """Определяет вендора по RTSP-URL."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL обязателен"}), 400

    key = ct.detect_vendor(url)
    return jsonify({
        "vendor": key,
        "label": ct.get_vendor_label(key),
        "streams": ct.get_vendor_streams(key),
    })


@settings_bp.route("/api/camera_templates/autofill", methods=["POST"])
@admin_required
def api_camera_autofill():
    """
    Генерирует RTSP-URL по вендору, хосту, порту и выбранному потоку.
    Принимает: vendor, host, port, stream_id, user, password.
    """
    data = request.get_json(silent=True) or {}
    vendor = (data.get("vendor") or "").strip()
    host = (data.get("host") or "").strip()
    if not vendor or not host:
        return jsonify({"error": "Поля vendor и host обязательны"}), 400

    try:
        port = int(data.get("port") or 554)
    except (TypeError, ValueError):
        port = 554

    stream_id = (data.get("stream_id") or "").strip()
    user = (data.get("user") or "").strip()
    password = data.get("password") or ""

    url = ct.autofill_rtsp(vendor, host, port, stream_id, user, password)
    if not url:
        return jsonify({"error": "Не удалось сгенерировать URL"}), 400

    return jsonify({"url": url})



# ======================================================================
# API: обнаружение камер (ONVIF WS-Discovery + Hikvision SADP)
# ======================================================================
def _merge_discovery_results(onvif_list: list, sadp_list: list) -> list:
    """
    Объединяет результаты ONVIF WS-Discovery и SADP по IP.

    Приоритет полей:
        - ONVIF: endpoint, name (scopes), hardware, xaddrs, scopes
        - SADP:  mac, serial, model, firmware, http_port, command_port

    Возвращает единый список. Каждое устройство:
        {
          "ip", "endpoint", "name", "hardware", "scopes", "xaddrs",
          "mac", "serial", "model", "firmware",
          "http_port", "command_port", "dhcp", "activated",
          "source": "onvif" | "sadp" | "onvif+sadp",
        }
    """
    by_ip: dict[str, dict] = {}

    # 1. Сначала — ONVIF-устройства
    for d in onvif_list or []:
        ip = d.get("ip")
        if not ip:
            continue
        by_ip[ip] = {
            "ip": ip,
            "endpoint": d.get("endpoint"),
            "name": d.get("name") or "",
            "hardware": d.get("hardware") or "",
            "scopes": d.get("scopes") or [],
            "xaddrs": d.get("xaddrs") or "",
            "mac": "",
            "serial": "",
            "model": "",
            "firmware": "",
            "http_port": None,
            "command_port": None,
            "dhcp": None,
            "activated": None,
            "source": "onvif",
        }

    # 2. Затем — SADP-устройства (дополняют или добавляют)
    for d in sadp_list or []:
        ip = d.get("ip")
        if not ip:
            continue
        if ip in by_ip:
            # Найдено обоими — дополняем
            by_ip[ip].update({
                "mac": d.get("mac") or by_ip[ip].get("mac", ""),
                "serial": d.get("serial") or by_ip[ip].get("serial", ""),
                "model": d.get("model") or by_ip[ip].get("model", ""),
                "firmware": d.get("firmware") or by_ip[ip].get("firmware", ""),
                "http_port": d.get("http_port"),
                "command_port": d.get("command_port"),
                "dhcp": d.get("dhcp"),
                "activated": d.get("activated"),
                "source": "onvif+sadp",
            })
            # Если у ONVIF не было имени — берём модель из SADP
            if not by_ip[ip].get("name") and d.get("model"):
                by_ip[ip]["name"] = d["model"]
        else:
            # Только SADP — Hikvision с выключенным ONVIF или без него
            http_port = d.get("http_port") or 80
            if http_port == 80:
                endpoint_guess = f"http://{ip}/onvif/device_service"
            else:
                endpoint_guess = (
                    f"http://{ip}:{http_port}/onvif/device_service"
                )
            by_ip[ip] = {
                "ip": ip,
                "endpoint": endpoint_guess,  # best guess
                "name": d.get("model") or "",
                "hardware": "",
                "scopes": [],
                "xaddrs": "",
                "mac": d.get("mac") or "",
                "serial": d.get("serial") or "",
                "model": d.get("model") or "",
                "firmware": d.get("firmware") or "",
                "http_port": d.get("http_port"),
                "command_port": d.get("command_port"),
                "dhcp": d.get("dhcp"),
                "activated": d.get("activated"),
                "source": "sadp",
            }

    return list(by_ip.values())


def _merge_extra_discovery(devices, extra):
    """Дополняет результаты ONVIF/SADP найденными SSDP/Dahua по IP."""
    by_ip = {d.get("ip"): dict(d) for d in devices if d.get("ip")}
    for item in extra or []:
        ip = item.get("ip")
        if not ip:
            continue
        if ip not in by_ip:
            by_ip[ip] = {
                "ip": ip, "endpoint": item.get("endpoint") or "",
                "name": item.get("name") or "", "hardware": "",
                "scopes": [], "xaddrs": "", "mac": item.get("mac") or "",
                "serial": item.get("serial") or "", "model": item.get("model") or "",
                "firmware": item.get("firmware") or "",
                "http_port": item.get("http_port"), "command_port": None,
                "dhcp": None, "activated": item.get("activated"),
                "source": item.get("source") or "extra",
            }
            continue
        current = by_ip[ip]
        for key in ("endpoint", "name", "model", "serial", "firmware", "mac",
                    "http_port", "activated"):
            if item.get(key) and not current.get(key):
                current[key] = item[key]
        source = current.get("source") or ""
        extra_source = item.get("source") or ""
        if extra_source and extra_source not in source.split("+"):
            current["source"] = (source + "+" + extra_source).strip("+")
    return list(by_ip.values())


@settings_bp.route("/api/onvif/discover", methods=["POST"])
@admin_required
def api_onvif_discover():
    """
    Параллельное сканирование локальной сети:
        * ONVIF WS-Discovery (multicast UDP 239.255.255.250:3702)
        * SADP — Hikvision (multicast UDP 239.255.255.250:37020)

    Возвращает объединённый список. Поле `source` у каждого устройства:
        * "onvif"      — найдено только через ONVIF
        * "sadp"       — найдено только через SADP (Hikvision)
        * "onvif+sadp" — найдено обоими (совпал IP)

    Время: ~timeout (параллельно, а не 2×timeout).
    """
    import concurrent.futures as _cf

    payload = request.get_json(silent=True) or {}
    timeout = float(payload.get("timeout", 4.0))
    timeout = max(1.0, min(timeout, 15.0))
    methods = payload.get("methods") or []
    if isinstance(methods, str):
        methods = [methods]
    methods = {str(m).lower() for m in methods}

    # --- Ленивая загрузка модулей с защитой от отсутствия ---
    onvif_fn = None
    sadp_fn = None

    try:
        from onvif_client import discover_devices as _discover_onvif
        onvif_fn = _discover_onvif
    except Exception as e:
        logger.exception("ONVIF discovery недоступен: %s", e)

    try:
        from sadp_discovery import discover_sadp_devices as _discover_sadp
        sadp_fn = _discover_sadp
    except Exception as e:
        logger.exception("SADP discovery недоступен: %s", e)

    def _safe_run(fn):
        if fn is None:
            return []
        try:
            return fn(timeout=timeout)
        except Exception as e:
            logger.exception("Discovery: %s упал: %s", fn, e)
            return []

    onvif_devices: list = []
    sadp_devices: list = []

    with _cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_onvif = ex.submit(_safe_run, onvif_fn)
        f_sadp = ex.submit(_safe_run, sadp_fn)
        onvif_devices = f_onvif.result()
        sadp_devices = f_sadp.result()

    merged = _merge_discovery_results(onvif_devices, sadp_devices)

    extra = []
    extra_fns = {}
    if "ssdp" in methods:
        try:
            from network_discovery import discover_ssdp
            extra_fns["ssdp"] = discover_ssdp
        except Exception as exc:
            logger.exception("SSDP discovery недоступен: %s", exc)
    if "dahua" in methods:
        try:
            from network_discovery import discover_dahua
            extra_fns["dahua"] = discover_dahua
        except Exception as exc:
            logger.exception("Dahua discovery недоступен: %s", exc)
    if extra_fns:
        with _cf.ThreadPoolExecutor(max_workers=len(extra_fns)) as ex:
            futures = {
                ex.submit(_safe_run, fn): name
                for name, fn in extra_fns.items()
            }
            for future in _cf.as_completed(futures):
                try:
                    extra.extend(future.result() or [])
                except Exception as exc:
                    logger.exception("Discovery %s: %s", futures[future], exc)
    merged = _merge_extra_discovery(merged, extra)

    if "isapi" in methods and merged:
        try:
            from network_discovery import enrich_with_isapi
            merged = enrich_with_isapi(merged, timeout=min(timeout, 3.0))
        except Exception as exc:
            logger.exception("ISAPI probe недоступен: %s", exc)

    def _count(source):
        return sum(1 for d in merged if source in (d.get("source") or "").split("+"))

    return jsonify({
        "ok": True,
        "count": len(merged),
        "by_source": {
            "onvif": _count("onvif"),
            "sadp": _count("sadp"),
            "ssdp": _count("ssdp"),
            "dahua": _count("dahua"),
            "isapi": _count("isapi"),
        },
        "devices": merged,
    })


@settings_bp.route("/api/onvif/probe", methods=["POST"])
@admin_required
def api_onvif_probe():
    """
    Подключается к ONVIF-камере и получает список профилей с RTSP-адресами.
    """
    payload = request.get_json(silent=True) or {}
    endpoint = (payload.get("endpoint") or "").strip()
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not endpoint:
        return jsonify({"error": "Не указан endpoint"}), 400

    try:
        from onvif_client import get_rtsp_urls
        result = get_rtsp_urls(endpoint, username, password)
    except Exception as e:
        logger.exception("Ошибка ONVIF probe")
        return jsonify({"error": f"Ошибка подключения: {e}"}), 500

    if not result.get("ok"):
        return jsonify({"error": result.get("error") or "Не удалось получить профили"}), 400

    return jsonify(result)


@settings_bp.route("/api/onvif/add_camera", methods=["POST"])
@admin_required
def api_onvif_add_camera():
    """
    Создаёт камеру на основе данных ONVIF.
    """
    st = _state()
    payload = request.get_json(silent=True) or {}

    cam_id = (payload.get("id") or "").strip()
    name = (payload.get("name") or "").strip()
    rtsp_url = (payload.get("rtsp_url") or "").strip()

    if not cam_id or not name or not rtsp_url:
        return jsonify({"error": "Поля id, name и rtsp_url обязательны"}), 400

    if cam_id in st["cameras"]:
        return jsonify({"error": f"Камера '{cam_id}' уже существует"}), 409

    if not re.fullmatch(r"[A-Za-z0-9_\-]+", cam_id):
        return jsonify({"error": "id может содержать только буквы, цифры, '_' и '-'"}), 400

    # Режим записи: off / always / events.
    # Логика 1-в-1 как в api_add_camera (см. _build_camera_cfg_from_payload).
    record_mode = str(payload.get("record_mode") or "").strip()
    if record_mode not in ("off", "always", "events"):
        record_mode = "always" if payload.get("record") is True else "off"

    try:
        event_record_duration = int(payload.get("event_record_duration", 30))
        if event_record_duration < 5:
            event_record_duration = 5
        if event_record_duration > 3600:
            event_record_duration = 3600
    except (TypeError, ValueError):
        event_record_duration = 30

    raw_topics = payload.get("event_topics")
    if isinstance(raw_topics, list):
        allowed = {"motion", "input", "tamper", "storage", "network"}
        event_topics = [t for t in raw_topics if t in allowed]
    else:
        event_topics = ["motion", "input", "tamper"]
    if not event_topics:
        event_topics = ["motion"]

    new_cfg = {
        "id": cam_id,
        "name": name,
        "type": "ip",
        "source": rtsp_url,
        "capture_index": None,
        "fps": int(payload.get("fps", 25)),
        "width": int(payload.get("width", 1280)),
        "height": int(payload.get("height", 720)),
        # record = (record_mode != "off") — согласовано с record_mode
        "record": (record_mode != "off"),
        "archive_path": "",
        "record_mode": record_mode,
        "event_record_duration": event_record_duration,
        "event_topics": event_topics,
        "transport": "tcp",
        "open_timeout_ms": 5000,
        "read_timeout_ms": 5000,
        "has_audio": None,
        "audio": False,
    }

    loopback_urls = current_app.config.get("LOOPBACK_URLS", {})
    if cam_id in loopback_urls:
        new_cfg["preview_source"] = loopback_urls[cam_id]

    try:
        cam = Camera(new_cfg)
        cam.start()
    except Exception as e:
        logger.exception("Ошибка создания ONVIF-камеры")
        return jsonify({"error": f"Не удалось создать камеру: {e}"}), 500

    st["cameras"][cam_id] = cam

    archive_cfg = st["config"].get("archive", {})
    mode = new_cfg.get("record_mode", "off")
    archive_on = (
            _archive_storage_status().get("available", False)
            and archive_cfg.get("record_all", True)
    )

    if archive_on and mode == "always":
        try:
            rec = Recorder(cam, archive_cfg, new_cfg)
            rec.start()
            st["recorders"][cam_id] = rec
        except Exception:
            logger.exception("Ошибка создания Recorder")
    elif archive_on and mode == "events":
        em = current_app.config.get("EVENTS_MANAGER")
        if not em:
            logger.warning(
                "Режим 'events' для %s, но EventsManager не запущен", cam_id,
            )
        else:
            try:
                rec = EventRecorder(cam, archive_cfg, new_cfg, em)
                rec.start()
                st["recorders"][cam_id] = rec
            except Exception:
                logger.exception("Ошибка создания EventRecorder")

    st["config"].setdefault("cameras", []).append(new_cfg)
    _save_config(st["config"], st["config_path"])

    _attach_motion_detector(cam, new_cfg, replace=False)

    logger.info("ONVIF: добавлена камера %s (%s)", cam_id, rtsp_url)

    return jsonify({"ok": True, "camera": _camera_to_dict(cam)}), 201


# ======================================================================
# API: PTZ — управление поворотами/зумом
# ======================================================================
@settings_bp.route("/api/cameras/<camera_id>/ptz/info", methods=["GET"])
@login_required
def api_ptz_info(camera_id: str):
    """
    Проверяет поддержку PTZ и возвращает конфигурации + пресеты.
    """
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404
    if cam.type != "ip":
        return jsonify({
            "ok": True,
            "has_ptz": False,
            "error": "PTZ доступен только для IP-камер",
        })

    # Логин/пароль для ONVIF — из URL камеры, если они там есть
    from onvif_client import get_ptz_info
    # Если функция parse_rtsp_auth не найдена — берём логин/пароль из source

    # Извлекаем endpoint камеры из RTSP-URL
    # RTSP-URL вида: rtsp://user:pass@host:port/path
    # ONVIF endpoint: http://host/onvif/device_service
    rtsp_url = cam.source
    parsed = _parse_rtsp_for_onvif(rtsp_url)
    if not parsed:
        return jsonify({
            "ok": False,
            "has_ptz": False,
            "error": "Не удалось разобрать RTSP-URL камеры",
        }), 400

    endpoint = _ptz_endpoint_for(camera_id, cam, parsed)
    logger.info("[%s] PTZ info: %s", camera_id, endpoint)

    try:
        result = get_ptz_info(
            endpoint,
            parsed.get("user", ""),
            parsed.get("password", ""),
        )
    except Exception as e:
        logger.exception("PTZ info error")
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify(result)


@settings_bp.route("/api/cameras/<camera_id>/ptz/move", methods=["POST"])
@admin_required
def api_ptz_move(camera_id: str):
    """
    Движение PTZ.
    Тело: {"pan": 0.5, "tilt": 0.0, "zoom": 0.0, "duration": 0.5}
    """
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404
    if cam.type != "ip":
        return jsonify({"error": "PTZ только для IP"}), 400

    payload = request.get_json(silent=True) or {}
    pan = float(payload.get("pan", 0.0))
    tilt = float(payload.get("tilt", 0.0))
    zoom = float(payload.get("zoom", 0.0))
    duration = float(payload.get("duration", 0.5))
    profile_token = payload.get("profile_token")

    if not profile_token:
        return jsonify({"error": "Не указан profile_token"}), 400

    parsed = _parse_rtsp_for_onvif(cam.source)
    if not parsed:
        return jsonify({"error": "Не удалось разобрать RTSP-URL"}), 400

    endpoint = _ptz_endpoint_for(camera_id, cam, parsed)

    from onvif_client import ptz_move
    ok = ptz_move(
        endpoint,
        parsed.get("user", ""),
        parsed.get("password", ""),
        profile_token,
        pan=pan, tilt=tilt, zoom=zoom,
        duration=duration,
    )
    return jsonify({"ok": ok})


@settings_bp.route("/api/cameras/<camera_id>/ptz/stop", methods=["POST"])
@admin_required
def api_ptz_stop(camera_id: str):
    """Остановить движение."""
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json(silent=True) or {}
    profile_token = payload.get("profile_token")
    if not profile_token:
        return jsonify({"error": "Не указан profile_token"}), 400

    parsed = _parse_rtsp_for_onvif(cam.source)
    if not parsed:
        return jsonify({"error": "Не удалось разобрать RTSP-URL"}), 400

    endpoint = _ptz_endpoint_for(camera_id, cam, parsed)

    from onvif_client import ptz_stop
    ok = ptz_stop(
        endpoint,
        parsed.get("user", ""),
        parsed.get("password", ""),
        profile_token,
    )
    return jsonify({"ok": ok})


@settings_bp.route("/api/cameras/<camera_id>/ptz/home", methods=["POST"])
@admin_required
def api_ptz_home(camera_id: str):
    """Вернуть в исходное положение."""
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json(silent=True) or {}
    profile_token = payload.get("profile_token")
    if not profile_token:
        return jsonify({"error": "Не указан profile_token"}), 400

    parsed = _parse_rtsp_for_onvif(cam.source)
    if not parsed:
        return jsonify({"error": "Не удалось разобрать RTSP-URL"}), 400

    endpoint = _ptz_endpoint_for(camera_id, cam, parsed)

    from onvif_client import ptz_home
    ok = ptz_home(
        endpoint,
        parsed.get("user", ""),
        parsed.get("password", ""),
        profile_token,
    )
    return jsonify({"ok": ok})


@settings_bp.route("/api/cameras/<camera_id>/ptz/preset/goto", methods=["POST"])
@admin_required
def api_ptz_preset_goto(camera_id: str):
    """Перейти к пресету."""
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json(silent=True) or {}
    profile_token = payload.get("profile_token")
    preset_token = payload.get("preset_token")
    if not profile_token or not preset_token:
        return jsonify({"error": "Нужны profile_token и preset_token"}), 400

    parsed = _parse_rtsp_for_onvif(cam.source)
    if not parsed:
        return jsonify({"error": "Не удалось разобрать RTSP-URL"}), 400

    endpoint = _ptz_endpoint_for(camera_id, cam, parsed)

    from onvif_client import ptz_goto_preset
    ok = ptz_goto_preset(
        endpoint,
        parsed.get("user", ""),
        parsed.get("password", ""),
        profile_token,
        preset_token,
    )
    return jsonify({"ok": ok})


@settings_bp.route("/api/cameras/<camera_id>/ptz/preset/set", methods=["POST"])
@admin_required
def api_ptz_preset_set(camera_id: str):
    """Создать пресет из текущего положения."""
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json(silent=True) or {}
    profile_token = payload.get("profile_token")
    name = (payload.get("name") or "").strip()
    if not profile_token or not name:
        return jsonify({"error": "Нужны profile_token и name"}), 400

    parsed = _parse_rtsp_for_onvif(cam.source)
    if not parsed:
        return jsonify({"error": "Не удалось разобрать RTSP-URL"}), 400

    endpoint = _ptz_endpoint_for(camera_id, cam, parsed)

    from onvif_client import ptz_set_preset
    result = ptz_set_preset(
        endpoint,
        parsed.get("user", ""),
        parsed.get("password", ""),
        profile_token,
        name,
    )
    return jsonify({"ok": result is not None, "preset": result})


@settings_bp.route("/api/cameras/<camera_id>/ptz/preset/remove", methods=["POST"])
@admin_required
def api_ptz_preset_remove(camera_id: str):
    """Удалить пресет."""
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json(silent=True) or {}
    profile_token = payload.get("profile_token")
    preset_token = payload.get("preset_token")
    if not profile_token or not preset_token:
        return jsonify({"error": "Нужны profile_token и preset_token"}), 400

    parsed = _parse_rtsp_for_onvif(cam.source)
    if not parsed:
        return jsonify({"error": "Не удалось разобрать RTSP-URL"}), 400

    endpoint = _ptz_endpoint_for(camera_id, cam, parsed)

    from onvif_client import ptz_remove_preset
    ok = ptz_remove_preset(
        endpoint,
        parsed.get("user", ""),
        parsed.get("password", ""),
        profile_token,
        preset_token,
    )
    return jsonify({"ok": ok})


# ======================================================================
# Вспомогательное: разбор RTSP-URL для ONVIF
# ======================================================================
def _ptz_endpoint_for(cam_id: str, cam, parsed: dict) -> str:
    """Рабочий ONVIF-endpoint для PTZ: кэш проверки -> конфиг -> перебор портов.

    Раньше move/stop/home всегда шли на порт 80, хотя capabilities могла
    найти ONVIF на 8899 — команды уходили в никуда, а API возвращал 200.
    """
    cached = (_ptz_cache.get("data") or {}).get(cam_id) or {}
    ep = cached.get("endpoint")
    if ep and cached.get("has_ptz"):
        return ep

    try:
        raw = next((c for c in _state()["config"].get("cameras", [])
                    if c.get("id") == cam_id), {})
    except Exception:
        raw = {}

    forced = (raw.get("ptz_endpoint") or "").strip()
    if forced:
        return forced

    port = int(raw.get("onvif_port") or 0)
    if port:
        return f"http://{parsed['host']}:{port}/onvif/device_service"

    from onvif_client import get_ptz_info
    for p_ in (80, 8000, 2020, 8899):
        cand = f"http://{parsed['host']}{'' if p_ == 80 else f':{p_}'}/onvif/device_service"
        try:
            if get_ptz_info(cand, parsed.get("user", ""), parsed.get("password", "")).get("has_ptz"):
                return cand
        except Exception:
            continue
    return f"http://{parsed['host']}/onvif/device_service"


def _parse_rtsp_for_onvif(rtsp_url: str):
    """
    Разбирает RTSP-URL вида rtsp://user:pass@host:port/path
    и возвращает {host, port, user, password} или None.

    Fallback для нестандартных URL (iSpy/AgentDVR), где креды
    лежат внутри path, а не перед host:
        rtsp://192.168.1.88:554/user=andrew_password=nS6VfUet_channel=0_...
    В этом случае вытаскиваем user/password регуляркой из path.
    """
    if not rtsp_url:
        return None
    m = re.match(
        r"^rtsp://"
        r"(?:(?P<user>[^:@/]+)(?::(?P<pass>[^@/]*))?@)?"
        r"(?P<host>[^:/]+)"
        r"(?::(?P<port>\d+))?"
        r"(?P<path>/.*)?$",
        rtsp_url.strip(),
        re.IGNORECASE,
    )
    if not m:
        return None

    user = m.group("user") or ""
    password = m.group("pass") or ""
    path = m.group("path") or ""

    # Fallback: iSpy-формат "user=..._password=..." внутри path.
    # Сработает, только если в URL не было user:pass@ перед host.
    if not user and not password and path:
        m2 = re.search(r"user=([^_&]+)_password=([^_&]+)", path)
        if m2:
            user = m2.group(1)
            password = m2.group(2)
            logger.info(
                "ONVIF: креды извлечены из iSpy-path: user='%s'", user,
            )

    return {
        "user": user,
        "password": password,
        "host": m.group("host") or "",
        "port": int(m.group("port")) if m.group("port") else 554,
        "path": path,
    }


# ======================================================================
# API: PTZ — capabilities для всех камер (для главной страницы)
# ======================================================================
# Кэш результатов: проверка PTZ занимает время (несколько SOAP-запросов
# на камеру), поэтому кэшируем и перепроверяем раз в 5 минут.
_ptz_cache = {
    "data": {},          # {camera_id: {"has_ptz": bool, "profile_token": str|None}}
    "ts": 0.0,           # timestamp последней проверки
}
_PTZ_CACHE_TTL = 300     # секунд


@settings_bp.route("/api/ptz/capabilities", methods=["GET"])
@login_required
def api_ptz_capabilities():
    """
    Возвращает для всех IP-камер информацию о поддержке PTZ.
    Формат: {"cameras": {"ip_1": {"has_ptz": true, "profile_token": "..."}, ...}}

    Кэшируется на 5 минут, чтобы не дёргать ONVIF при каждой загрузке
    главной страницы.
    """
    import time as _time

    st = _state()
    cameras = st["cameras"]

    # Если кэш свежий — возвращаем из него
    now = _time.time()
    if (now - _ptz_cache["ts"] < _PTZ_CACHE_TTL
            and _ptz_cache["data"]):
        return jsonify({"cameras": _ptz_cache["data"]})

    # Перепроверяем
    from onvif_client import get_ptz_info

    result = {}
    for cam_id, cam in cameras.items():
        # PTZ — это ONVIF поверх той же камеры: тип записи не важен,
        # важно, что источник RTSP (у аналоговых его нет).
        if not str(getattr(cam, "source", "")).startswith("rtsp://"):
            result[cam_id] = {"has_ptz": False, "profile_token": None}
            continue

        parsed = _parse_rtsp_for_onvif(cam.source)
        if not parsed:
            result[cam_id] = {"has_ptz": False, "profile_token": None}
            continue

        # ------------ подсказка -----------------
        # У камер ONVIF живёт на разных портах (80, 8000, 2020, 8899),
        # поэтому пробуем по очереди и запоминаем причину отказа,
        # чтобы её было видно в логе и в ответе API.
        raw = next((c for c in st["config"].get("cameras", [])
                    if c.get("id") == cam_id), {})
        forced = (raw.get("ptz_endpoint") or "").strip()
        _onvif_port = int(raw.get("onvif_port") or 0)

        if forced:
            entries = [forced]
        elif _onvif_port:
            entries = [f"http://{parsed['host']}:{_onvif_port}/onvif/device_service"]
        else:
            host = parsed["host"]
            entries = [f"http://{host}{'' if p == 80 else f':{p}'}/onvif/device_service"
                       for p in (80, 8000, 2020, 8899)]

        found, last_error = None, None
        for ep in entries:
            try:
                info = get_ptz_info(ep, parsed.get("user", ""),
                                    parsed.get("password", ""))
            except Exception as exc:
                last_error = f"{ep}: {exc}"
                continue
            if info.get("has_ptz"):
                found = (ep, info)
                break
            if info.get("error"):
                last_error = f"{ep}: {info['error']}"

        if found:
            ep, info = found
            result[cam_id] = {
                "has_ptz": True,
                "profile_token": info.get("profile_token"),
                "endpoint": ep,
            }
            logger.info("[%s] PTZ доступен: %s", cam_id, ep)
        else:
            result[cam_id] = {
                "has_ptz": False,
                "profile_token": None,
                "reason": last_error or "камера не ответила на ONVIF",
            }
            logger.warning("[%s] PTZ недоступен: %s", cam_id,
                           last_error or "нет ответа ONVIF")

    _ptz_cache["data"] = result
    _ptz_cache["ts"] = now

    return jsonify({"cameras": result})


# ======================================================================
# API: ONVIF Events — быстрая проверка
# ======================================================================
@settings_bp.route("/api/cameras/<camera_id>/events/test", methods=["POST"])
@admin_required
def api_events_test(camera_id: str):
    """
    Быстрая проверка: создаёт подписку, слушает pull_timeout секунд,
    возвращает список полученных событий.
    """
    st = _state()
    cam = st["cameras"].get(camera_id)
    if not cam:
        return jsonify({"error": "not found"}), 404
    if cam.type != "ip":
        return jsonify({"error": "События доступны только для IP-камер"}), 400

    payload = request.get_json(silent=True) or {}
    try:
        pull_timeout = int(payload.get("timeout", 5))
        pull_timeout = max(2, min(pull_timeout, 30))
    except (TypeError, ValueError):
        pull_timeout = 5

    parsed = _parse_rtsp_for_onvif(cam.source)
    if not parsed:
        return jsonify({"error": "Не удалось разобрать RTSP-URL камеры"}), 400

    endpoint = _ptz_endpoint_for(camera_id, cam, parsed)
    logger.info("[%s] Events test: %s, таймаут %s сек",
                camera_id, endpoint, pull_timeout)

    try:
        from onvif_events import test_events
        result = test_events(
            endpoint,
            parsed.get("user", ""),
            parsed.get("password", ""),
            pull_timeout=pull_timeout,
        )
    except Exception as e:
        logger.exception("Events test error")
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify(result)


# ======================================================================
# API: журнал ONVIF-событий
# ======================================================================
@settings_bp.route("/api/events", methods=["GET"])
@login_required
def api_events_list():
    """
    Список событий с фильтрами.
    Query:
        camera_id — фильтр по камере
        severity  — info | warning | alert
        since_ts  — Unix timestamp, только новее
        limit     — макс. число записей (по умолчанию 200)
    """
    em = current_app.config.get("EVENTS_MANAGER")
    if not em:
        return jsonify({"error": "Менеджер событий не запущен", "events": []}), 503

    camera_id = request.args.get("camera_id") or None
    severity = request.args.get("severity") or None

    try:
        since_ts = request.args.get("since_ts")
        since_ts = float(since_ts) if since_ts else None
    except (TypeError, ValueError):
        since_ts = None

    try:
        limit = int(request.args.get("limit", 200))
        limit = max(1, min(limit, 1000))
    except (TypeError, ValueError):
        limit = 200

    # Проверка доступа, если фильтр по камере задан явно
    if camera_id and not can_access_camera(camera_id):
        return jsonify({"error": "forbidden", "events": []}), 403

    try:
        events = em.db.query(
            camera_id=camera_id,
            since_ts=since_ts,
            severity=severity,
            limit=limit,
        )
    except Exception as e:
        logger.exception("Ошибка чтения событий")
        return jsonify({"error": str(e), "events": []}), 500

    # Если фильтр не задан — оставляем только события разрешённых камер
    if not camera_id:
        events = [
            ev for ev in events
            if can_access_camera(ev.get("camera_id"))
        ]

    return jsonify({
        "ok": True,
        "count": len(events),
        "events": events,
    })


@settings_bp.route("/api/events/stats", methods=["GET"])
@login_required
def api_events_stats():
    """Сводка по журналу событий."""
    em = current_app.config.get("EVENTS_MANAGER")
    if not em:
        return jsonify({"error": "Менеджер событий не запущен"}), 503

    try:
        stats = em.db.stats()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "stats": stats})


@settings_bp.route("/api/events/manager", methods=["GET"])
@login_required
def api_events_manager():
    """Статус менеджера событий."""
    em = current_app.config.get("EVENTS_MANAGER")
    if not em:
        return jsonify({
            "ok": True,
            "running": False,
            "message": "Менеджер не запущен",
        })
    return jsonify({"ok": True, **em.get_status()})


@settings_bp.route("/api/events/cleanup", methods=["POST"])
@admin_required
def api_events_cleanup():
    """Принудительная чистка журнала событий."""
    em = current_app.config.get("EVENTS_MANAGER")
    if not em:
        return jsonify({"error": "Менеджер не запущен"}), 503

    payload = request.get_json(silent=True) or {}
    try:
        max_age_days = int(payload.get("max_age_days", 30))
        max_records = int(payload.get("max_records", 200000))
    except (TypeError, ValueError):
        return jsonify({"error": "Неверные параметры"}), 400

    try:
        result = em.db.cleanup(max_age_days=max_age_days,
                                max_records=max_records)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "result": result})