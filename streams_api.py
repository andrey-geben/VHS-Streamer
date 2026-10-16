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
import logging
from pathlib import Path

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

        # RTSP-ссылки для копирования
        urls = [
            {
                "url": f"rtsp://{ep['host']}:{rtsp_port}{s['path']}",
                "label": ep["label"],
                "host": ep["host"],
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

        result.append({
            **s,
            "camera_name": cam.name if cam else "—",
            "estimate": est,
            "urls": urls,
            "webrtc_urls": webrtc_urls,
            "runtime": {
                "ready": bool(status and status.get("ready")),
                "readers": (status or {}).get("readers", []) or [],
                "bytes_received": (status or {}).get("bytesReceived", 0),
            },
        })

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
    new_stream = {
        "id": payload["id"],
        "camera_id": payload["camera_id"],
        "path": payload["path"],
        "copy": not transcode,
        "enabled": bool(payload.get("enabled", True)),
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
    return jsonify({"ok": True, "stream": new_stream}), 201


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
    return jsonify({"ok": True, "enabled": stream["enabled"]})


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