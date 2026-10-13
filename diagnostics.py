"""
Модуль диагностики VHS-Streamer.

Собирает всё важное в один ZIP-архив:
    * system.txt        — ОС, Python, CPU, порты, бинарники;
    * config.sanitized.yaml — конфиг без паролей и токенов;
    * mediamtx.json     — пути и статусы из API MediaMTX;
    * app_state.json    — состояние камер, рекордеров, потоков;
    * ffmpeg_devices.txt — список устройств захвата;
    * app.log           — последние строки лога.

Использование:
    python diagnostics.py                        # создать diagnostics_YYYYMMDD_HHMMSS.zip
    python diagnostics.py -o report.zip          # в свой файл
    python diagnostics.py --no-zip               # вывести в консоль

В составе Flask: GET /api/diagnostics/download (только для admin).
"""
import argparse
import io
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Blueprint, send_file, current_app

from auth import admin_required
import bootstrap

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Пути
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
LOG_FILE = BASE_DIR / "logs" / "vhs-streamer.log"
CONFIG_FILE = BASE_DIR / "config.yaml"

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

# Сколько последних строк лога включать
LOG_TAIL_LINES = 2000

diagnostics_bp = Blueprint("diagnostics", __name__)


# ======================================================================
# Хелперы
# ======================================================================
def _human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _safe(fn, name: str, default: str = ""):
    """Безопасный вызов: ловит любые исключения."""
    try:
        return fn()
    except Exception as e:
        logger.warning("Ошибка при сборе '%s': %s", name, e)
        return f"<ошибка сбора '{name}': {e}>"


# ======================================================================
# Сбор системной информации
# ======================================================================
def collect_system_info() -> str:
    lines = []
    lines.append("=" * 72)
    lines.append("СИСТЕМНАЯ ИНФОРМАЦИЯ")
    lines.append("=" * 72)
    lines.append(f"Собрано:            {datetime.now().isoformat()}")
    lines.append(f"ОС:                 {platform.system()} {platform.release()}")
    lines.append(f"Версия ОС:          {platform.version()}")
    lines.append(f"Архитектура:        {platform.machine()}")
    lines.append(f"Узел:               {platform.node()}")
    lines.append(f"Python:             {sys.version.split()[0]}")
    lines.append(f"Python-путь:        {sys.executable}")
    lines.append(f"CPU (логических):   {os.cpu_count()}")
    lines.append(f"Текущая папка:      {os.getcwd()}")
    lines.append(f"Папка проекта:      {BASE_DIR}")
    try:
        lines.append(f"Имя хоста:          {socket.gethostname()}")
        lines.append(f"IP (локальный):     {socket.gethostbyname(socket.gethostname())}")
    except Exception:
        pass
    return "\n".join(lines)


def collect_binaries() -> str:
    lines = ["", "=" * 72, "БИНАРНИКИ (bin/)", "=" * 72]
    if IS_WINDOWS:
        names = ["mediamtx.exe", "ffmpeg.exe", "ffprobe.exe"]
    else:
        names = ["mediamtx", "ffmpeg", "ffprobe"]
    for name in names:
        p = bootstrap.get_path(name)
        if p.exists():
            st = p.stat()
            mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(
                f"✓ {name:15s} {_human_size(st.st_size):>10s}   изменён: {mtime}"
            )
        else:
            lines.append(f"✗ {name:15s} ОТСУТСТВУЕТ")
    return "\n".join(lines)


def collect_ports() -> str:
    lines = ["", "=" * 72, "СЛУШАЮЩИЕ ПОРТЫ (интересующие)", "=" * 72]
    ports = ["8000", "8080", "8554", "9997"]
    try:
        if IS_WINDOWS:
            cmd = ["netstat", "-ano"]
        else:
            cmd = ["netstat", "-tlnp"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        for line in out.splitlines():
            if any(f":{p}" in line for p in ports):
                lines.append(line.rstrip())
        if len(lines) == 3:
            lines.append("(ничего не найдено)")
    except Exception as e:
        lines.append(f"Ошибка: {e}")
    return "\n".join(lines)


# ======================================================================
# Конфиг без секретов
# ======================================================================
def collect_config_sanitized() -> str:
    if not CONFIG_FILE.exists():
        return f"# config.yaml не найден: {CONFIG_FILE}"
    try:
        import yaml
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        # Маскируем пароли и токены
        sec = cfg.get("security", {})
        for u in sec.get("users", []):
            if "password" in u:
                u["password"] = "***"
        if "api_token" in sec:
            sec["api_token"] = "***"
        srv = cfg.get("server", {})
        if "secret_key" in srv:
            srv["secret_key"] = "***"
        return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    except Exception as e:
        return f"# Ошибка чтения config.yaml: {e}"


# ======================================================================
# MediaMTX
# ======================================================================
def collect_mediamtx() -> dict:
    result = {
        "api_url": "http://127.0.0.1:9997",
        "api_reachable": False,
        "paths_config": None,
        "paths_runtime": None,
        "error": None,
    }
    try:
        import requests
        try:
            r = requests.get(
                "http://127.0.0.1:9997/v3/config/paths/list", timeout=3
            )
            if r.status_code == 200:
                result["api_reachable"] = True
                result["paths_config"] = r.json()
        except Exception as e:
            result["error"] = f"config/paths/list: {e}"

        try:
            r = requests.get(
                "http://127.0.0.1:9997/v3/paths/list", timeout=3
            )
            if r.status_code == 200:
                result["paths_runtime"] = r.json()
        except Exception as e:
            if not result["error"]:
                result["error"] = f"paths/list: {e}"
    except ImportError:
        result["error"] = "Модуль requests не установлен"
    return result


# ======================================================================
# FFmpeg — устройства захвата
# ======================================================================
def collect_ffmpeg_devices() -> str:
    bin_name = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    ffmpeg = bootstrap.get_path(bin_name)
    if not ffmpeg.exists():
        return f"FFmpeg не найден: {ffmpeg}"

    if IS_WINDOWS:
        cmd = [str(ffmpeg), "-hide_banner", "-list_devices", "true",
               "-f", "dshow", "-i", "dummy"]
    else:
        cmd = [str(ffmpeg), "-hide_banner", "-f", "v4l2",
               "-list_formats", "all", "-i", "/dev/video0"]

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
        return (r.stdout or "") + "\n" + (r.stderr or "")
    except Exception as e:
        return f"Ошибка запуска FFmpeg: {e}"


# ======================================================================
# Состояние приложения (камеры, рекордеры, потоки)
# ======================================================================
def _collect_app_state(cfg: dict) -> dict:
    state = {"cameras": [], "recorders": [], "streams": []}
    try:
        cameras = cfg.get("CAMERAS", {}) or {}
        recorders = cfg.get("RECORDERS", {}) or {}
        vhs_cfg = cfg.get("VHS_CONFIG", {}) or {}

        for cam_id, cam in cameras.items():
            try:
                state["cameras"].append({
                    "id": cam.id,
                    "name": cam.name,
                    "type": cam.type,
                    "source": str(cam.source),
                    "fps": cam.fps,
                    "width": cam.width,
                    "height": cam.height,
                    "connected": bool(cam._connected),
                    "last_error": cam._last_error,
                })
            except Exception as e:
                state["cameras"].append({"id": cam_id, "error": str(e)})

        for cam_id, rec in recorders.items():
            try:
                state["recorders"].append(rec.get_status())
            except Exception as e:
                state["recorders"].append({"camera_id": cam_id, "error": str(e)})

        state["streams"] = vhs_cfg.get("streams", []) or []
    except Exception as e:
        state["error"] = str(e)
    return state


# ======================================================================
# Лог
# ======================================================================
def collect_log_tail(max_lines: int = LOG_TAIL_LINES) -> str:
    if not LOG_FILE.exists():
        return f"Лог-файл не найден: {LOG_FILE}"
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        tail = lines[-max_lines:]
        header = (
            f"# Всего строк в логе: {total}\n"
            f"# Показаны последние: {len(tail)}\n"
            f"{'=' * 72}\n"
        )
        return header + "".join(tail)
    except Exception as e:
        return f"Ошибка чтения лога: {e}"


# ======================================================================
# Сбор всего в один dict
# ======================================================================
def collect_all(app_config: dict | None = None) -> dict:
    """Возвращает {имя_файла: содержимое_или_объект}."""
    files = {}

    # 1. Система + бинарники + порты в один файл
    system = _safe(collect_system_info, "system_info")
    system += _safe(collect_binaries, "binaries")
    system += _safe(collect_ports, "ports")
    files["system.txt"] = system

    # 2. Конфиг
    files["config.sanitized.yaml"] = _safe(
        collect_config_sanitized, "config"
    )

    # 3. MediaMTX
    mtx = _safe(collect_mediamtx, "mediamtx")
    if isinstance(mtx, dict):
        files["mediamtx.json"] = json.dumps(mtx, indent=2, ensure_ascii=False)
    else:
        files["mediamtx.json"] = json.dumps({"error": str(mtx)})

    # 4. Устройства FFmpeg
    files["ffmpeg_devices.txt"] = _safe(
        collect_ffmpeg_devices, "ffmpeg_devices"
    )

    # 5. Состояние приложения
    if app_config:
        state = _collect_app_state(app_config)
    else:
        state = {"note": "Собрано из CLI, состояние приложения недоступно"}
    files["app_state.json"] = json.dumps(state, indent=2, ensure_ascii=False)

    # 6. Лог
    files["app.log"] = _safe(collect_log_tail, "log_tail")

    # 7. Мета-файл
    files["README.txt"] = (
        "Диагностический отчёт VHS-Streamer\n"
        f"Собран: {datetime.now().isoformat()}\n\n"
        "Файлы:\n"
        "  system.txt              — ОС, Python, порты, бинарники\n"
        "  config.sanitized.yaml   — конфиг (пароли и токены замаскированы)\n"
        "  mediamtx.json           — пути и статусы MediaMTX\n"
        "  app_state.json          — камеры, рекордеры, потоки\n"
        "  ffmpeg_devices.txt      — устройства захвата (dshow / v4l2)\n"
        "  app.log                 — последние строки лога\n"
    )
    return files


# ======================================================================
# ZIP
# ======================================================================
def build_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            if not isinstance(content, str):
                content = json.dumps(content, indent=2, ensure_ascii=False)
            z.writestr(name, content)
    return buf.getvalue()


# ======================================================================
# Flask-маршрут
# ======================================================================
@diagnostics_bp.route("/api/diagnostics/download", methods=["GET"])
@admin_required
def download_diagnostics():
    files = collect_all(current_app.config)
    data = build_zip(files)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        io.BytesIO(data),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"vhs-diagnostics_{ts}.zip",
    )


# ======================================================================
# CLI
# ======================================================================
def _cli():
    parser = argparse.ArgumentParser(
        description="Сбор диагностики VHS-Streamer"
    )
    parser.add_argument("-o", "--output", help="куда сохранить zip-файл")
    parser.add_argument("--no-zip", action="store_true",
                        help="вывести содержимое в консоль, без zip")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    files = collect_all(None)

    if args.no_zip:
        for name, content in files.items():
            print(f"\n{'=' * 72}\nФАЙЛ: {name}\n{'=' * 72}")
            if not isinstance(content, str):
                content = json.dumps(content, indent=2, ensure_ascii=False)
            print(content)
        return

    data = build_zip(files)
    if args.output:
        out = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = BASE_DIR / f"vhs-diagnostics_{ts}.zip"
    out.write_bytes(data)
    print(f"Готово: {out}")
    print(f"Размер: {_human_size(len(data))}")


if __name__ == "__main__":
    _cli()