"""Зачистка дочерних процессов VHS-Streamer.

Зачем: ffmpeg/ffprobe/mediamtx запускаются десятками (камеры, рекордеры,
runOnInit-публикаторы MediaMTX). При аварийном завершении или падении
MediaMTX они остаются висеть и продолжают держать камеры и порты.

Модуль умеет:
  * kill_tree(pid)     — погасить процесс вместе со всеми потомками;
  * kill_leftovers()   — погасить процессы, запущенные из нашей папки bin/
                         (osдаже если родитель давно умер).
"""

from __future__ import annotations

import logging
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)


def _exe_under(proc: psutil.Process, folder: Path) -> bool:
    try:
        exe = proc.exe() or ""
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if not exe:
        return False
    try:
        return Path(exe).resolve().is_relative_to(folder.resolve())
    except (ValueError, OSError):
        return str(Path(exe)).lower().startswith(str(folder).lower())


def kill_tree(pid: int, timeout: float = 3.0) -> int:
    """Гасит процесс и всех его потомков. Возвращает число погашенных."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return 0

    procs = []
    try:
        procs = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    procs.append(root)

    killed = 0
    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for p in procs:
        try:
            if not p.is_running():
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            killed += 1
    return killed


def kill_leftovers(bin_dir: Path | str, keep_pids: set[int] | None = None) -> list[int]:
    """Гасит процессы, запущенные из папки bin/ (ffmpeg, ffprobe, mediamtx)."""
    keep = set(keep_pids or ())
    bin_dir = Path(bin_dir)
    if not bin_dir.exists():
        return []

    found: list[int] = []
    for proc in psutil.process_iter(["pid"]):
        try:
            pid = proc.info["pid"]
            if pid in keep or pid == psutil.Process().pid:
                continue
            if _exe_under(proc, bin_dir):
                found.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    for pid in found:
        try:
            kill_tree(pid)
        except Exception:
            continue

    if found:
        logger.warning("Зачищено зависших процессов из %s: %d", bin_dir, len(found))
    return found


def count_bin_processes(bin_dir: Path | str) -> int:
    """Сколько процессов сейчас запущено из папки bin/ (для диагностики)."""
    bin_dir = Path(bin_dir)
    if not bin_dir.exists():
        return 0
    n = 0
    for proc in psutil.process_iter(["pid"]):
        try:
            if _exe_under(proc, bin_dir):
                n += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return n
