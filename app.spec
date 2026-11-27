# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec для VHS-Streamer.

Сборка в режиме --onedir (папка):
    * .exe стартует мгновенно;
    * можно обновлять отдельные файлы без пересборки;
    * удобнее отладка.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
    collect_dynamic_libs,
)

# Корень проекта
ROOT = Path(SPECPATH).resolve()

# --- Шаблоны и статика (внутрь _MEIPASS) ---
datas = [
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "static"), "static"),
]

# --- NumPy: собираем всё, что PyInstaller может пропустить ---
try:
    numpy_datas = collect_data_files("numpy")
    numpy_binaries = collect_dynamic_libs("numpy")
    numpy_hiddenimports = collect_submodules("numpy")
    datas += numpy_datas
except Exception:
    numpy_datas = []
    numpy_binaries = []
    numpy_hiddenimports = []

# --- OpenCV: те же проблемы ---
try:
    opencv_datas = collect_data_files("cv2")
    opencv_binaries = collect_dynamic_libs("cv2")
    opencv_hiddenimports = collect_submodules("cv2")
    datas += opencv_datas
except Exception:
    opencv_datas = []
    opencv_binaries = []
    opencv_hiddenimports = []

# --- psutil: C-extension, подхватываем подмодули на всякий случай ---
try:
    psutil_hiddenimports = collect_submodules("psutil")
except Exception:
    psutil_hiddenimports = []

# --- Скрытые импорты ---
hiddenimports = [
    "numpy",
    "numpy._core",
    "numpy._core._exceptions",
    "numpy._core._multiarray_umath",
    "numpy._core._multiarray_tests",
    "numpy._core._dtype_ctypes",
    "cv2",
    "yaml",
    "requests",
    "flask",
    "werkzeug",
    "jinja2",
    "PIL",
    "psutil",

    # --- Модули проекта ---
    "helper",
    "__version__",
    "audio_probe",
    "preview",
    "transcode",
    "mediamtx_manager",
    "diagnostics",
    "onvif_client",
    "onvif_events",
    "onvif_server",
    "camera_templates",
    "netdetect",
    "events_manager",
    "event_recorder",
    "recorder",
    "camera",
    "duplicator",
    "auth",
    "settings_api",
    "streams_api",
    "bootstrap",

    # --- Модули, появившиеся после релиза v1.0.0 ---
    # Импортируются лениво или из новых фич. Добавляем явно для надёжности.
    "device_discovery",   # задача №1 — обнаружение локальных устройств
    "motion_detector",    # motion detection по OpenCV
    "sadp_discovery",     # SADP (Hikvision) discovery
    "network_discovery",  # SSDP/UPnP, Dahua, ISAPI
    "system_monitor",     # системный мониторинг (CPU/RAM/диски/GPU/сеть)
]
hiddenimports += numpy_hiddenimports
hiddenimports += opencv_hiddenimports
hiddenimports += psutil_hiddenimports

binaries = numpy_binaries + opencv_binaries


a = Analysis(
    ["app.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "notebook",
        "IPython",
        "jupyter",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VHS-Streamer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ROOT / "icon.ico"),
    version=str(ROOT / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="VHS-Streamer",
)