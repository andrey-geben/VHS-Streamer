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
# Начиная с numpy 2.3.0 некоторые подмодули не подхватываются автоматически.
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
    "helper",
    "__version__",
    "audio_probe",
    "preview",
    "transcode",
    "mediamtx_manager",
    "diagnostics",
]
hiddenimports += numpy_hiddenimports
hiddenimports += opencv_hiddenimports

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