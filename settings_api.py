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
import copy
import logging
import platform
import re
import secrets
from pathlib import Path

import yaml
from flask import (
    Blueprint, jsonify, request, render_template, current_app, g,
)

from auth import login_required, admin_required
from camera import Camera
from recorder import Recorder
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


def _serialize_config(cfg: dict, is_admin: bool) -> dict:
    c = copy.deepcopy(cfg)
    if is_admin:
        return c
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
        "connected": cam._connected,
        "last_error": cam._last_error,
    }


def _find_camera_cfg(cfg: dict, cam_id: str) -> dict | None:
    for c in cfg.get("cameras", []):
        if c.get("id") == cam_id:
            return c
    return None


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

    # Запись в архив — по умолчанию выключена
    record = bool(payload.get("record", False))

    # Индивидуальный путь архива — только если включена запись
    archive_path = ""
    if record:
        ap = payload.get("archive_path")
        if ap:
            archive_path = str(ap).strip()

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
        "transport": payload.get("transport") or None,
        "open_timeout_ms": int(payload.get("open_timeout_ms", 5000)),
        "read_timeout_ms": int(payload.get("read_timeout_ms", 5000)),
        "has_audio": has_audio,
        "audio": audio,
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
        cleaned_users.append({
            "username": username,
            "password": password,
            "role": role,
        })

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
    return jsonify([_camera_to_dict(c) for c in st["cameras"].values()])


@settings_bp.route("/api/cameras/<camera_id>", methods=["GET"])
@login_required
def api_get_camera(camera_id: str):
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

    archive_cfg = st["config"].get("archive", {})
    if (new_cfg.get("record", False)
            and archive_cfg.get("enabled", False)
            and archive_cfg.get("record_all", True)):
        try:
            rec = Recorder(cam, archive_cfg, new_cfg)
            rec.start()
            st["recorders"][cam_id] = rec
        except Exception:
            logger.exception("Ошибка создания рекордера")

    st["config"].setdefault("cameras", []).append(new_cfg)
    _save_config(st["config"], st["config_path"])

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
    if (new_cfg.get("record", False)
            and archive_cfg.get("enabled", False)
            and archive_cfg.get("record_all", True)):
        try:
            rec = Recorder(new_cam, archive_cfg, new_cfg)
            rec.start()
            st["recorders"][camera_id] = rec
        except Exception:
            logger.exception("Ошибка создания рекордера")

    st["config"].setdefault("cameras", []).append(new_cfg)
    _save_config(st["config"], st["config_path"])

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
    return jsonify(st["config"].get("archive", {}))


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

    return jsonify({
        "ok": True,
        "restart_required": "path" in payload,
        "archive": archive,
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