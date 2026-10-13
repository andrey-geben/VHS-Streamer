# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec для VHS-Streamer.

Собираем в режиме --onedir (папка), не --onefile:
    * .exe стартует мгновенно (не нужно распаковывать каждый раз);
    * можно обновлять отдельные файлы без пересборки;
    * удобнее отладка.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Корень проекта
ROOT = Path(SPECPATH).resolve()

# --- Собираем данные, которые должны попасть внутрь .exe ---
datas = [
    # Шаблоны (только для чтения, внутрь _MEIPASS)
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "static"), "static"),
]

# Скрытые импорты — PyInstaller не всегда видит их сам
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = [
    "cv2",
    "yaml",
    "requests",
    "flask",
    "werkzeug",
    "jinja2",
    "PIL",
    "numpy",
    "numpy._core",
    "numpy._core._exceptions",
    "numpy._core._multiarray_umath",
]
hiddenimports += collect_submodules("numpy")

# Пытаемся подключить imageio-ffmpeg, если используется
try:
    hiddenimports += collect_submodules("imageio_ffmpeg")
except Exception:
    pass

# ONNX Runtime (если понадобится в будущем)
try:
    hiddenimports += collect_submodules("onnxruntime")
except Exception:
    pass

datas = datas + collect_data_files("numpy")

a = Analysis(
    ["app.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Не тащим лишнее
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
    upx=False,          # UPX часто ломает OpenCV DLL
    console=True,       # True — видно консоль с логами
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
     icon=str(ROOT / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VHS-Streamer",
)