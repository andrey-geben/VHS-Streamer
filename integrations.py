"""Сопряжение VHS-Streamer с VHS-Viewer и VHS-Security."""

from __future__ import annotations

import os
from urllib.parse import quote

import requests
from flask import Blueprint, current_app, jsonify, render_template, request

import auth as auth_mod
from __version__ import __version__

integration_bp = Blueprint("integrations", __name__)

PARTNERS = [
    ("Viewer", "Аналитика, ALPR и события", os.environ.get("VHS_VIEWER_URL", "")),
    ("Security", "Карта объекта и тревоги", os.environ.get("VHS_SECURITY_URL", "")),
]


@integration_bp.get("/api/integration/info")
def info():
    return jsonify({
        "app": "VHS-Streamer",
        "version": __version__,
        "role": "video",
        "auth": {"header": "X-VHS-Key", "aliases": ["X-API-Token"]},
        "capabilities": ["rtsp", "archive", "onvif_events", "ptz", "motion"],
        "endpoints": {
            "streams": "/api/integration/streams",
            "info": "/api/integration/info",
        },
    })


@integration_bp.get("/api/integration/partners")
@auth_mod.login_required
def partners():
    result = []
    for name, purpose, url in PARTNERS:
        ok = None
        if url:
            try:
                response = requests.get(url.rstrip("/") + "/api/integration/info", timeout=2)
                ok = response.ok
            except requests.RequestException:
                ok = False
        result.append({"name": name, "purpose": purpose, "url": url, "online": ok})
    return jsonify({"app": "VHS-Streamer", "partners": result})




@integration_bp.get("/api/integration/streams")
@auth_mod.login_required
def stream_urls():
    """Авторизованные WebRTC-URL потоков для доверенных модулей (Security)."""
    cfg = current_app.config.get("APP_CONFIG", {}) or {}
    host = (request.host or "127.0.0.1").split(":")[0]
    # Свежий web-токен вместо read_password: не зависит от того, успел ли
    # MediaMTX синхронизировать пароль потока.
    token = auth_mod.create_mtx_token("admin")
    auth = f"admin:{quote(token)}@"
    camera_names = {c.get("id"): c.get("name") for c in cfg.get("cameras", [])}
    result = []
    for stream in cfg.get("streams", []):
        if not stream.get("enabled", True):
            continue
        sid = str(stream.get("id") or "")
        path = str(stream.get("path") or "")
        result.append({
            "id": sid,
            "camera_id": stream.get("camera_id"),
            "camera_name": camera_names.get(stream.get("camera_id"), stream.get("camera_id")),
            "path": path,
            "requires_auth": True,
            "webrtc_url": f"http://{auth}{host}:8889{path}/",
        })
    return jsonify({"streams": result, "host": host})


@integration_bp.get("/system")
@auth_mod.login_required
def system_page():
    statuses = []
    for name, purpose, url in PARTNERS:
        online = False
        if url:
            try:
                online = requests.get(url.rstrip("/") + "/api/integration/info", timeout=2).ok
            except requests.RequestException:
                online = False
        statuses.append((name, purpose, url, online))
    return render_template("system.html", partners=statuses)
