"""
API управления потоками и страница «Потоки».

Логика:
    * для каждой камеры автоматически создаётся raw-путь в MediaMTX;
    * пользовательские потоки добавляются/удаляются через UI;
    * на одну камеру — не более MAX_STREAMS_PER_CAMERA потоков;
    * оценка нагрузки CPU/RAM — эвристическая, показывается в UI;
    * каждый поток возвращает готовые RTSP- и WebRTC-ссылки
      для каждого сетевого интерфейса (гибрид: автоопределение
      системы + ручные описания из config.yaml).
"""
import base64
import hashlib
import logging
import secrets
from pathlib import Path

import requests
import yaml

from flask import (
    Blueprint, jsonify, request, render_template, current_app, g,
)

from auth import login_required, admin_required
from netdetect import build_endpoints

logger = logging.getLogger(__name__)

streams_bp = Blueprint("streams", __name__)

# Максимум потоков на одну камеру
MAX_STREAMS_PER_CAMERA = 3


# ----------------------------------------------------------------------
# Хелперы
# ----------------------------------------------------------------------
def _state():
    return {
        "cameras": current_app.config["CAMERAS"],
        "config": current_app.config["VHS_CONFIG"],
        "config_path": Path(current_app.config["CONFIG_PATH"]),
        "mediamtx": current_app.config.get("MEDIAMTX"),
        "mediamtx_helpers": current_app.config.get("MEDIAMTX_HELPERS"),
    }


def _save_config(cfg: dict, path: Path):
    import yaml
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def _hash_password_sha256(password: str) -> str:
    """
    Возвращает хеш пароля в формате MediaMTX:
        "sha256:<base64(sha256(password))>"
    Используется в authInternalUsers[].pass.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return "sha256:" + base64.b64encode(digest).decode("ascii")


def _generate_password(length: int = 12) -> str:
    """
    Генерирует случайный пароль для read_password потока.
    Используется в api_add_stream, если пользователь не задал пароль явно.
    """
    return secrets.token_urlsafe(length)


def _sync_mediamtx_auth(config: dict, mediamtx_config_path: Path) -> int:
    """
    Синхронизирует authInternalUsers в mediamtx.yml с потоками из config.

    Правила (ТЗ, задача №2, п. 2.2b):
    - vhs_<id> создаётся для каждого enabled-потока с непустым read_password;
    - ips = allowed_ips (если пусто — ключ ips не добавляется = любой IP);
    - permissions = [{action: read, path: "/<path>"}];
    - удаляются все старые vhs_* пользователи;
    - гарантируется наличие пользователя с action: api (иначе Flask
      потеряет доступ к API MediaMTX);
    - self-heal: если итог пуст — ключ authInternalUsers удаляется целиком
      (дефолтное поведение MediaMTX, API открыт);
    - сохраняет динамические пути (source=publisher), добавленные через API,
      чтобы hot-reload не удалил их (иначе Camera/FFmpeg получат 400/404).

    Возвращает количество добавленных vhs_* пользователей.
    """
    if not mediamtx_config_path:
        return 0
    mtx_path = Path(mediamtx_config_path)
    if not mtx_path.exists():
        logger.warning(
            "mediamtx.yml не найден (%s), синхронизация auth пропущена",
            mtx_path,
        )
        return 0

    try:
        with open(mtx_path, "r", encoding="utf-8") as f:
            mtx_cfg = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error("Не удалось прочитать mediamtx.yml: %s", e)
        return 0

    # --- Защита от пустого файла ---
    # Если файл пустой (yaml.safe_load дал {} или None), записываем
    # базовую конфигурацию MediaMTX, иначе MediaMTX не стартует
    # (нет apiAddress, rtspAddress) — а мы в цикле будем перезаписывать
    # пустоту пустотой.
    if not mtx_cfg or not isinstance(mtx_cfg, dict):
        mtx_cfg = {}

    if not mtx_cfg.get("apiAddress") and not mtx_cfg.get("rtspAddress"):
        BASE_MTX_CONFIG = {
            "logLevel": "info",
            "rtspAddress": ":8554",
            "rtspTransports": ["tcp"],
            "api": True,
            "apiAddress": "127.0.0.1:9997",
            "webrtc": True,
            "webrtcAddress": ":8889",
            "rtmp": False,
            "hls": False,
            "srt": False,
            "playback": False,
            "paths": {},
        }
        mtx_cfg.update(BASE_MTX_CONFIG)
        logger.info(
            "mediamtx.yml: пустой/отсутствуют базовые ключи — "
            "восстановлена базовая конфигурация MediaMTX"
        )

    existing_users = mtx_cfg.get("authInternalUsers") or []

    # 1) Убираем старых vhs_*
    kept_users = []
    for u in existing_users:
        if not isinstance(u, dict):
            continue
        if str(u.get("user", "")).startswith("vhs_"):
            continue
        kept_users.append(u)

    # 2) Собираем новых vhs_* из config
    new_vhs_users = []
    for s in config.get("streams", []):
        if not s.get("enabled", True):
            continue
        password = (s.get("read_password") or "").strip()
        if not password:
            continue
        stream_id = s.get("id", "")
        path = s.get("path", "")
        if not stream_id or not path:
            continue
        user_entry = {
            "user": f"vhs_{stream_id}",
            "pass": _hash_password_sha256(password),
            "permissions": [{"action": "read", "path": path}],
        }
        allowed_ips = s.get("allowed_ips") or []
        if allowed_ips:
            user_entry["ips"] = list(allowed_ips)
        new_vhs_users.append(user_entry)

    merged_users = kept_users + new_vhs_users

    # 3) Self-heal: если итог пуст — ключ убираем целиком
    if not merged_users:
        mtx_cfg.pop("authInternalUsers", None)
        logger.info(
            "mediamtx.yml: authInternalUsers пуст — ключ удалён "
            "(дефолтное поведение MediaMTX)"
        )
    else:
        # 4) Гарантируем наличие пользователя с action: api
        has_api_user = False
        for u in merged_users:
            for perm in u.get("permissions", []):
                if isinstance(perm, dict) and perm.get("action") == "api":
                    has_api_user = True
                    break
            if has_api_user:
                break

        if not has_api_user:
            merged_users.append({
                "user": "any",
                "ips": ["127.0.0.1", "::1"],
                "permissions": [
                    {"action": "api"},
                    {"action": "publish"},
                    {"action": "read"},
                    {"action": "playback"},
                ],
            })
            logger.info(
                "mediamtx.yml: добавлен пользователь 'any' с action: api "
                "(защита от закрытия API MediaMTX)"
            )

        mtx_cfg["authInternalUsers"] = merged_users

    # --- ВАЖНО: сохраняем динамические пути, добавленные через API ---
    # MediaMTX при hot-reload перечитывает файл и УДАЛЯЕТ все пути,
    # которых нет в секции paths. Если в файле "paths: {}" —
    # все raw-пути и пользовательские потоки будут потеряны,
    # и Camera/FFmpeg получат 400/404. Снимаем снимок через API
    # и записываем в файл, чтобы сохранить их.
    try:
        r = requests.get(
            "http://127.0.0.1:9997/v3/config/paths/list",
            timeout=5,
        )
        if r.status_code == 200:
            items = r.json().get("items", []) or []
            if items:
                mtx_cfg["paths"] = {
                    item["name"]: item for item in items
                    if isinstance(item, dict) and item.get("name")
                }
                logger.info(
                    "mediamtx.yml: сохранено %d динамических путей",
                    len(mtx_cfg["paths"]),
                )
    except Exception as e:
        logger.warning(
            "mediamtx.yml: не удалось сохранить динамические пути: %s", e,
        )

    try:
        with open(mtx_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(mtx_cfg, f, allow_unicode=True, sort_keys=False)
    except Exception as e:
        logger.error("Не удалось записать mediamtx.yml: %s", e)
        return 0

    logger.info(
        "mediamtx.yml: синхронизировано %d пользователей vhs_* (всего: %d)",
        len(new_vhs_users), len(merged_users),
    )
    return len(new_vhs_users)


def _maybe_sync_auth(config: dict):
    """
    Вызывает _sync_mediamtx_auth, если в Flask config задан
    MEDIAMTX_CONFIG_PATH. Иначе — тихо пропускает.
    """
    mtx_path = current_app.config.get("MEDIAMTX_CONFIG_PATH")
    if not mtx_path:
        return
    try:
        _sync_mediamtx_auth(config, Path(mtx_path))
    except Exception as e:
        logger.exception("Ошибка синхронизации mediamtx.yml: %s", e)

def _raw_path(camera_id: str) -> str:
    """Имя raw-пути для камеры."""
    return f"__raw_{camera_id}"


def _count_streams_for_camera(cfg: dict, camera_id: str) -> int:
    """Сколько потоков уже привязано к камере."""
    return sum(
        1 for s in cfg.get("streams", [])
        if s.get("camera_id") == camera_id
    )


def _next_stream_index(cfg: dict, camera_id: str) -> int:
    """
    Возвращает первый свободный индекс для потоков камеры.
    Формат id: {camera_id}_s{N}, где N — 1..MAX.
    """
    used = set()
    prefix = f"{camera_id}_s"
    for s in cfg.get("streams", []):
        if s.get("camera_id") != camera_id:
            continue
        sid = s.get("id", "")
        if sid.startswith(prefix):
            try:
                used.add(int(sid[len(prefix):]))
            except ValueError:
                pass
    for i in range(1, MAX_STREAMS_PER_CAMERA + 1):
        if i not in used:
            return i
    return MAX_STREAMS_PER_CAMERA + 1


# ----------------------------------------------------------------------
# Оценка нагрузки (эвристика)
# ----------------------------------------------------------------------
def _estimate_load(stream_cfg: dict, camera) -> dict:
    if stream_cfg.get("copy", True):
        return {"cpu_percent_of_core": 2, "ram_mb": 20, "level": "low"}

    codec = stream_cfg.get("codec", "libx264")
    scale = stream_cfg.get("scale", "") or ""
    fps = int(stream_cfg.get("fps", camera.fps if camera else 25))

    target_h = 480
    try:
        w = int(scale.split(":")[0])
        target_h = int(w * 9 / 16)
    except Exception:
        pass

    if codec == "libx264":
        base = target_h * fps / 25 * 0.08
        cpu = min(int(base), 200)
    elif codec in ("h264_qsv", "h264_nvenc", "h264_amf", "nvv4l2h264enc"):
        cpu = 5
    else:
        cpu = 50

    ram = 50 + target_h // 4

    if cpu < 20:
        level = "low"
    elif cpu < 60:
        level = "medium"
    elif cpu < 120:
        level = "high"
    else:
        level = "critical"

    return {"cpu_percent_of_core": cpu, "ram_mb": ram, "level": level}


def _total_estimated_load(cfg: dict, cameras: dict) -> dict:
    total_cpu = 0
    total_ram = 0
    analog_count = 0

    for stream in cfg.get("streams", []):
        if not stream.get("enabled", True):
            continue
        cam = cameras.get(stream.get("camera_id"))
        if not cam:
            continue
        est = _estimate_load(stream, cam)
        total_cpu += est["cpu_percent_of_core"]
        total_ram += est["ram_mb"]

    for cam in cameras.values():
        if cam.type == "analog":
            analog_count += 1
            total_cpu += 60
            total_ram += 80

    return {
        "cpu_percent_of_core": total_cpu,
        "cpu_cores_required": round(total_cpu / 100, 1),
        "ram_mb": total_ram,
        "analog_encoders": analog_count,
    }


# ----------------------------------------------------------------------
# Работа с MediaMTX
# ----------------------------------------------------------------------
def _apply_stream_to_mediamtx(stream_cfg: dict, cameras: dict, mediamtx, helpers):
    cam = cameras.get(stream_cfg["camera_id"])
    if not cam:
        raise ValueError(f"Камера '{stream_cfg['camera_id']}' не найдена")

    raw = _raw_path(cam.id)
    path_cfg = helpers["build_stream_path_config"](stream_cfg, raw)
    name = stream_cfg["path"].lstrip("/")
    mediamtx.add_path(name, path_cfg)


def _ensure_raw_path(camera, mediamtx, helpers):
    raw = _raw_path(camera.id)
    # --- дорабатываем проект. suppression guard ---
    if getattr(camera, "raw_suppressed", False):
        logger.info(
            "[%s] raw-путь подавлен администратором, автосоздание пропущено",
            camera.id,
        )
        return None
    # --- дорабатываем проект. конец вставки ---
    try:
        status = mediamtx.get_path_status(raw)
        if status and status.get("ready"):
            logger.debug("raw-путь %s уже готов, не пересоздаём", raw)
            return
    except Exception:
        pass

    cfg = helpers["build_raw_path_config"](camera)
    mediamtx.add_path(raw, cfg)


# ----------------------------------------------------------------------
# Страница
# ----------------------------------------------------------------------
@streams_bp.route("/streams")
@login_required
def streams_page():
    return render_template("streams.html", user=g.user)


# ----------------------------------------------------------------------
# API: список потоков (с готовыми RTSP и WebRTC URL)
# ----------------------------------------------------------------------
@streams_bp.route("/api/streams", methods=["GET"])
@login_required
def api_list_streams():
    st = _state()
    cfg = st["config"]
    cameras = st["cameras"]
    mediamtx = st["mediamtx"]

    rtsp_port = cfg.get("mediamtx", {}).get("rtsp_port", 8554)
    webrtc_port = cfg.get("mediamtx", {}).get("webrtc_port", 8889)

    # Список сетевых endpoints — гибрид:
    # автоопределение системы + ручные описания из config.yaml
    endpoints = build_endpoints(cfg)

    result = []
    for s in cfg.get("streams", []):
        cam = cameras.get(s.get("camera_id"))
        est = _estimate_load(s, cam) if cam else None

        name = s["path"].lstrip("/")
        status = mediamtx.get_path_status(name) if mediamtx else None

        # RTSP-ссылки для копирования.
        # Если у потока задан read_password — вставляем username в URL
        # (без пароля: VLC/клиент запросит его вручную).
        # Пароль в таблицу НЕ отдаём (has_read_password — маскировка).
        has_pw = bool(s.get("read_password"))
        auth_prefix = f"vhs_{s['id']}@" if has_pw else ""
        urls = [
            {
                "url": f"rtsp://{auth_prefix}{ep['host']}:{rtsp_port}{s['path']}",
                "label": ep["label"],
                "host": ep["host"],
                "requires_auth": has_pw,
            }
            for ep in endpoints
        ]

        # WebRTC-ссылки для встраивания в браузер
        webrtc_urls = [
            {
                "url": f"http://{ep['host']}:{webrtc_port}{s['path']}/",
                "label": ep["label"],
                "host": ep["host"],
            }
            for ep in endpoints
        ]

        # Маскируем read_password — не отдаём plaintext в GET /api/streams.
        # has_read_password: true/false — достаточно для значка 🔒 в UI.
        out = {**s}
        out["has_read_password"] = bool(out.pop("read_password", None))
        out["camera_name"] = cam.name if cam else "—"
        out["estimate"] = est
        out["urls"] = urls
        out["webrtc_urls"] = webrtc_urls
        out["runtime"] = {
            "ready": bool(status and status.get("ready")),
            "readers": (status or {}).get("readers", []) or [],
            "bytes_received": (status or {}).get("bytesReceived", 0),
        }
        result.append(out)

    totals = _total_estimated_load(cfg, cameras)
    return jsonify({
        "streams": result,
        "totals": totals,
        "rtsp_port": rtsp_port,
        "webrtc_port": webrtc_port,
        "endpoints": endpoints,
        "max_per_camera": MAX_STREAMS_PER_CAMERA,
    })


# ----------------------------------------------------------------------
# API: статистика по камерам (для выпадающего списка)
# ----------------------------------------------------------------------
@streams_bp.route("/api/streams/stats", methods=["GET"])
@login_required
def api_streams_stats():
    st = _state()
    cfg = st["config"]
    cameras = st["cameras"]
    result = []
    for cam_id, cam in cameras.items():
        count = _count_streams_for_camera(cfg, cam_id)
        result.append({
            "camera_id": cam_id,
            "camera_name": cam.name,
            "streams_count": count,
            "max_streams": MAX_STREAMS_PER_CAMERA,
            "can_add": count < MAX_STREAMS_PER_CAMERA,
        })
    return jsonify(result)


# ----------------------------------------------------------------------
# API: добавить поток
# ----------------------------------------------------------------------
@streams_bp.route("/api/streams", methods=["POST"])
@admin_required
def api_add_stream():
    st = _state()

    if not st["mediamtx"]:
        return jsonify({"error": "MediaMTX не запущен"}), 503

    payload = request.get_json(silent=True) or {}

    camera_id = payload.get("camera_id")
    if not camera_id:
        return jsonify({"error": "Поле 'camera_id' обязательно"}), 400
    if camera_id not in st["cameras"]:
        return jsonify({"error": "Камера не найдена"}), 404

    existing_count = _count_streams_for_camera(st["config"], camera_id)
    if existing_count >= MAX_STREAMS_PER_CAMERA:
        return jsonify({
            "error": (
                f"Для камеры '{camera_id}' уже создано "
                f"{MAX_STREAMS_PER_CAMERA} потока. Больше нельзя."
            )
        }), 409

    if not payload.get("id") or not payload.get("path"):
        n = _next_stream_index(st["config"], camera_id)
        payload["id"] = f"{camera_id}_s{n}"
        payload["path"] = f"/{camera_id}_s{n}"

    if not payload["path"].startswith("/"):
        return jsonify({"error": "path должен начинаться с '/'"}), 400

    existing = st["config"].setdefault("streams", [])
    if any(s.get("id") == payload["id"] for s in existing):
        return jsonify({"error": "Поток с таким id уже существует"}), 409
    if any(s.get("path") == payload["path"] for s in existing):
        return jsonify({"error": "Поток с таким path уже существует"}), 409

    transcode = bool(payload.get("transcode", False))

    # read_password: использовать явно заданный или сгенерировать автоматически.
    # generated_password != None → вернём plaintext пользователю ОДИН раз
    # в ответе на POST (потом увидеть нельзя, только сменить).
    explicit_password = (payload.get("read_password") or "").strip()
    if explicit_password:
        read_password = explicit_password
        generated_password = None
    else:
        read_password = _generate_password()
        generated_password = read_password

    new_stream = {
        "id": payload["id"],
        "camera_id": payload["camera_id"],
        "path": payload["path"],
        "copy": not transcode,
        "enabled": bool(payload.get("enabled", True)),
        "read_password": read_password,
    }

    if transcode:
        try:
            from transcode import get_recommended_codec
            default_codec = get_recommended_codec()
        except Exception:
            default_codec = "libx264"

        new_stream["codec"] = payload.get("codec", default_codec)
        new_stream["bitrate"] = payload.get("bitrate", "600k")
        new_stream["scale"] = payload.get("scale", "640:-2")
        new_stream["fps"] = int(payload.get("fps", 15))

    try:
        cam = st["cameras"][payload["camera_id"]]
        _ensure_raw_path(cam, st["mediamtx"], st["mediamtx_helpers"])
        _apply_stream_to_mediamtx(
            new_stream, st["cameras"],
            st["mediamtx"], st["mediamtx_helpers"],
        )
    except Exception as e:
        logger.exception("Ошибка применения потока в MediaMTX")
        return jsonify({"error": f"MediaMTX: {e}"}), 500

    existing.append(new_stream)
    _save_config(st["config"], st["config_path"])
    _maybe_sync_auth(st["config"])

    # stream: маскируем read_password — не отдаём plaintext в ответе.
    safe_stream = dict(new_stream)
    safe_stream["has_read_password"] = bool(
        safe_stream.pop("read_password", None)
    )

    resp = {"ok": True, "stream": safe_stream}
    if generated_password is not None:
        resp["generated_password"] = generated_password

        # Готовые RTSP-URL с паролем — для копирования в модалке
        # «Поток создан». Показываются ОДИН РАЗ (как и сам пароль).
        try:
            _eps = build_endpoints(st["config"])
            _port = st["config"].get("mediamtx", {}).get("rtsp_port", 8554)
            resp["generated_urls"] = [
                {
                    "url": (
                        f"rtsp://vhs_{new_stream['id']}:{generated_password}"
                        f"@{ep['host']}:{_port}{new_stream['path']}"
                    ),
                    "label": ep["label"],
                    "host": ep["host"],
                }
                for ep in _eps
            ]
        except Exception as e:
            logger.warning(
                "Не удалось сгенерировать full URL: %s", e,
            )

    return jsonify(resp), 201


# ----------------------------------------------------------------------
# API: удалить поток
# ----------------------------------------------------------------------
@streams_bp.route("/api/streams/<stream_id>", methods=["DELETE"])
@admin_required
def api_delete_stream(stream_id: str):
    st = _state()
    streams = st["config"].setdefault("streams", [])
    stream = next((s for s in streams if s.get("id") == stream_id), None)
    if not stream:
        return jsonify({"error": "not found"}), 404

    if st["mediamtx"]:
        try:
            st["mediamtx"].remove_path(stream["path"].lstrip("/"))
        except Exception as e:
            logger.warning("Не удалось удалить путь из MediaMTX: %s", e)

    streams.remove(stream)
    _save_config(st["config"], st["config_path"])
    _maybe_sync_auth(st["config"])
    return jsonify({"ok": True})


# ----------------------------------------------------------------------
# API: включить/выключить поток
# ----------------------------------------------------------------------
@streams_bp.route("/api/streams/<stream_id>/toggle", methods=["POST"])
@admin_required
def api_toggle_stream(stream_id: str):
    st = _state()
    stream = next(
        (s for s in st["config"].get("streams", []) if s.get("id") == stream_id),
        None,
    )
    if not stream:
        return jsonify({"error": "not found"}), 404

    stream["enabled"] = not stream.get("enabled", True)
    name = stream["path"].lstrip("/")

    if st["mediamtx"]:
        try:
            if stream["enabled"]:
                cam = st["cameras"].get(stream["camera_id"])
                if cam:
                    _ensure_raw_path(
                        cam, st["mediamtx"], st["mediamtx_helpers"],
                    )
                _apply_stream_to_mediamtx(
                    stream, st["cameras"],
                    st["mediamtx"], st["mediamtx_helpers"],
                )
            else:
                st["mediamtx"].remove_path(name)
        except Exception as e:
            return jsonify({"error": f"MediaMTX: {e}"}), 500

    _save_config(st["config"], st["config_path"])
    _maybe_sync_auth(st["config"])
    return jsonify({"ok": True, "enabled": stream["enabled"]})


# ----------------------------------------------------------------------
# API: обновить поток (path / allowed_ips / read_password)
# ----------------------------------------------------------------------
@streams_bp.route("/api/streams/<stream_id>", methods=["PATCH"])
@admin_required
def api_update_stream(stream_id: str):
    """
    Обновляет path, allowed_ips и/или read_password существующего потока.

    - path: строка, начинается с '/', не '/', не '/__raw_*', без пробелов,
            не занят другим потоком (иначе 409).
            При смене — remove_path(старый) + add_path(новый).
    - allowed_ips: массив строк (IP или CIDR), пустые игнорируются.
    - read_password: строка; пустая = пароль не менять.

    При ошибке MediaMTX — откат config и возврат 500.
    """
    st = _state()
    streams = st["config"].get("streams", [])
    stream = next((s for s in streams if s.get("id") == stream_id), None)
    if not stream:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json(silent=True) or {}

    old_path = stream.get("path", "")
    new_path = old_path

    if "path" in payload:
        candidate = str(payload.get("path") or "").strip()
        if not candidate:
            return jsonify({"error": "path не может быть пустым"}), 400
        if not candidate.startswith("/"):
            return jsonify({
                "error": "path должен начинаться с '/'"
            }), 400
        if candidate == "/":
            return jsonify({"error": "path не может быть '/'"}), 400
        if candidate.startswith("/__raw_"):
            return jsonify({
                "error": "path не может начинаться с '/__raw_'"
            }), 400
        if any(ch.isspace() for ch in candidate):
            return jsonify({
                "error": "path не может содержать пробелы"
            }), 400
        if candidate != old_path:
            occupied = any(
                (s is not stream) and (s.get("path") == candidate)
                for s in streams
            )
            if occupied:
                return jsonify({
                    "error": f"path '{candidate}' уже занят другим потоком"
                }), 409
            new_path = candidate

    new_ips = None
    if "allowed_ips" in payload:
        raw = payload.get("allowed_ips")
        if not isinstance(raw, list):
            return jsonify({
                "error": "allowed_ips должен быть массивом строк"
            }), 400
        cleaned_ips = []
        for item in raw:
            if not isinstance(item, str):
                return jsonify({
                    "error": "allowed_ips: элементы должны быть строками"
                }), 400
            s = item.strip()
            if s:
                cleaned_ips.append(s)
        new_ips = cleaned_ips

    new_password = None
    password_changed = False
    if "read_password" in payload:
        pw = payload.get("read_password")
        if not isinstance(pw, str):
            return jsonify({
                "error": "read_password должен быть строкой"
            }), 400
        if pw != "":
            new_password = pw
            password_changed = True

    # --- Запоминаем старое состояние для отката ---
    backup = dict(stream)

    # --- Применяем изменения в config (in-memory) ---
    if new_path != old_path:
        stream["path"] = new_path
    if new_ips is not None:
        stream["allowed_ips"] = new_ips
    if password_changed:
        stream["read_password"] = new_password

    # --- MediaMTX: пересоздаём путь при смене path ---
    if st["mediamtx"] and new_path != old_path:
        old_name = old_path.lstrip("/")
        try:
            st["mediamtx"].remove_path(old_name)
        except Exception as e:
            logger.warning(
                "Не удалось удалить старый путь %s: %s", old_name, e
            )
        try:
            cam = st["cameras"].get(stream.get("camera_id"))
            if cam:
                _ensure_raw_path(
                    cam, st["mediamtx"], st["mediamtx_helpers"],
                )
            _apply_stream_to_mediamtx(
                stream, st["cameras"],
                st["mediamtx"], st["mediamtx_helpers"],
            )
        except Exception as e:
            stream.clear()
            stream.update(backup)
            try:
                _apply_stream_to_mediamtx(
                    stream, st["cameras"],
                    st["mediamtx"], st["mediamtx_helpers"],
                )
            except Exception:
                pass
            logger.exception("MediaMTX: ошибка при смене path: %s", e)
            return jsonify({"error": f"MediaMTX: {e}"}), 500

    try:
        _save_config(st["config"], st["config_path"])
    except Exception as e:
        stream.clear()
        stream.update(backup)
        logger.exception("Не удалось сохранить config.yaml: %s", e)
        return jsonify({"error": f"config: {e}"}), 500

    _maybe_sync_auth(st["config"])

    # Маскируем read_password — не отдаём plaintext в ответе на PATCH.
    safe_stream = dict(stream)
    safe_stream["has_read_password"] = bool(
        safe_stream.pop("read_password", None)
    )
    return jsonify({"ok": True, "stream": safe_stream})


# ----------------------------------------------------------------------
# API: список сетевых интерфейсов (из системы + конфига)
# ----------------------------------------------------------------------
@streams_bp.route("/api/network/interfaces", methods=["GET"])
@login_required
def api_list_interfaces():
    st = _state()
    return jsonify(build_endpoints(st["config"]))


# ----------------------------------------------------------------------
# API: доступные кодеки и пресеты для транскодирования
# ----------------------------------------------------------------------
@streams_bp.route("/api/transcode/codecs", methods=["GET"])
@login_required
def api_transcode_codecs():
    try:
        from transcode import list_codecs_for_api
        return jsonify(list_codecs_for_api())
    except Exception as e:
        logger.exception("Ошибка определения кодеков")
        return jsonify({
            "available": [{"name": "libx264", "label": "libx264 (CPU)", "type": "cpu"}],
            "recommended": "libx264",
            "presets": [],
            "error": str(e),
        })


# ----------------------------------------------------------------------
# Публичный API для клиентов (VHS-Viewer и др.) — без авторизации
# ----------------------------------------------------------------------
@streams_bp.route("/api/public/streams", methods=["GET"])
def api_public_streams():
    """
    Публичный список потоков для клиентов-просмотрщиков.
    Не требует авторизации. Отдаёт только WebRTC-ссылки.
    """
    st = _state()
    cfg = st["config"]
    cameras = st["cameras"]

    rtsp_port = cfg.get("mediamtx", {}).get("rtsp_port", 8554)
    webrtc_port = cfg.get("mediamtx", {}).get("webrtc_port", 8889)

    endpoints = build_endpoints(cfg)
    # Для публичного API убираем пометку «(на сервере)»
    if endpoints:
        endpoints[0] = {"host": "127.0.0.1", "label": "Локально"}

    server_name = cfg.get("server", {}).get("name", "VHS-Streamer")

    result = []
    for s in cfg.get("streams", []):
        if not s.get("enabled", True):
            continue
        cam = cameras.get(s.get("camera_id"))
        cam_name = cam.name if cam else "—"

        webrtc_urls = [
            {
                "url": f"http://{ep['host']}:{webrtc_port}{s['path']}/",
                "label": ep["label"],
                "host": ep["host"],
            }
            for ep in endpoints
        ]

        result.append({
            "id": s["id"],
            "path": s["path"],
            "camera_name": cam_name,
            "name": f"{cam_name} / {s['id']}",
            "webrtc_urls": webrtc_urls,
        })

    return jsonify({
        "server_name": server_name,
        "rtsp_port": rtsp_port,
        "webrtc_port": webrtc_port,
        "count": len(result),
        "streams": result,
    })