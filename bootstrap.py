"""
Bootstrap — скачивание и раскладка внешних бинарников (MediaMTX, FFmpeg).

Запускается автоматически из app.py при первом старте, если бинарников
нет в папке bin/. Также можно запустить вручную:

    python bootstrap.py            # установить всё, что не установлено
    python bootstrap.py --force    # переустановить поверх
    python bootstrap.py --check    # только проверить, ничего не скачивать

Используется только стандартная библиотека Python.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Определение платформы
# ----------------------------------------------------------------------
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

_MACHINE = platform.machine().lower()
if _MACHINE in ("x86_64", "amd64"):
    ARCH = "amd64"
elif _MACHINE in ("aarch64", "arm64"):
    ARCH = "arm64"
else:
    ARCH = _MACHINE

# ----------------------------------------------------------------------
# Пути и источники
# ----------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.resolve()
BIN_DIR = PROJECT_ROOT / "bin"
MARKER_FILE = BIN_DIR / ".installed.json"

MEDIAMTX_RELEASES_API = (
    "https://api.github.com/repos/bluenviron/mediamtx/releases/latest"
)

FFMPEG_WINDOWS_URL = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
)
FFMPEG_LINUX_URL = {
    "amd64": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    "arm64": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz",
}

# Имя файла бинарника под текущую ОС
MEDIAMTX_BIN = "mediamtx.exe" if IS_WINDOWS else "mediamtx"
FFMPEG_BIN = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
FFPROBE_BIN = "ffprobe.exe" if IS_WINDOWS else "ffprobe"

# HTTP-заголовки — GitHub API требует User-Agent
HTTP_HEADERS = {"User-Agent": "VHS-Streamer-bootstrap/1.0"}


# ======================================================================
# Вспомогательные функции
# ======================================================================
def ensure_bin_dir() -> bool:
    """
    Создаёт папку bin/, если её нет.
    Возвращает True, если папка существует и доступна для записи.
    Бросает RuntimeError с понятным сообщением при проблемах с правами.
    """
    try:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise RuntimeError(
            f"Нет прав на создание папки '{BIN_DIR}'. "
            f"Запустите от имени администратора или перенесите проект "
            f"в пользовательскую директорию (например, C:\\Users\\<вы>\\...)."
        ) from e

    # Проверим, что можем писать
    test_file = BIN_DIR / ".write_test"
    try:
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
    except Exception as e:
        raise RuntimeError(
            f"Папка '{BIN_DIR}' существует, но недоступна для записи: {e}"
        ) from e

    return True


def _load_marker() -> dict:
    if not MARKER_FILE.exists():
        return {}
    try:
        return json.loads(MARKER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_marker(data: dict):
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    MARKER_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _log(msg: str, level: str = "info"):
    """Логирует и в logger, и в консоль — для наглядности при первом запуске."""
    getattr(logger, level, logger.info)(msg)
    print(msg, file=sys.stderr, flush=True)


def _human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _make_executable(path: Path):
    """chmod +x на Unix."""
    if IS_WINDOWS:
        return
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ======================================================================
# Скачивание с прогрессом
# ======================================================================
def _download(url: str, dest: Path, description: str = "", retries: int = 3):
    """
    Скачивает URL в dest с индикацией прогресса.
    При ошибке сети повторяет до `retries` раз.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers=HTTP_HEADERS)
            with urlopen(req, timeout=30) as r:
                total = int(r.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 64 * 1024
                last_percent = -1
                start_time = time.time()

                with open(dest, "wb") as f:
                    while True:
                        chunk = r.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)

                        if total > 0:
                            percent = int(downloaded * 100 / total)
                            if percent != last_percent and percent % 2 == 0:
                                speed = downloaded / max(time.time() - start_time, 0.1)
                                msg = (
                                    f"  {description}: {percent}% "
                                    f"({_human_size(downloaded)} / "
                                    f"{_human_size(total)}, "
                                    f"{_human_size(speed)}/с)"
                                )
                                print(msg, file=sys.stderr, end="\r", flush=True)
                                last_percent = percent

                print(" " * 120, file=sys.stderr, end="\r", flush=True)
                print(
                    f"  {description}: скачано "
                    f"({_human_size(dest.stat().st_size)})",
                    file=sys.stderr, flush=True,
                )
                return
        except (URLError, HTTPError, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < retries:
                wait = 2 * attempt
                _log(f"  {description}: попытка {attempt} не удалась ({e}), "
                     f"повтор через {wait} с", "warning")
                time.sleep(wait)
            else:
                break

    raise RuntimeError(f"Не удалось скачать {url}: {last_exc}")


# ======================================================================
# Распаковка архивов
# ======================================================================
def _extract_archive(archive: Path, dest_dir: Path):
    """Распаковывает .zip или .tar.xz / .tar.gz в dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()

    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest_dir)
    elif name.endswith((".tar.xz", ".tar.gz", ".tgz", ".txz")):
        with tarfile.open(archive) as t:
            t.extractall(dest_dir)
    else:
        raise ValueError(f"Неизвестный формат архива: {archive.name}")


def _find_file_recursive(root: Path, filename: str) -> Path | None:
    """Ищет файл с указанным именем на любой глубине внутри root."""
    for p in root.rglob(filename):
        if p.is_file():
            return p
    return None


# ======================================================================
# Установка MediaMTX
# ======================================================================
def _mediamtx_download_url() -> tuple[str, str]:
    """
    Возвращает (url, version) для последней версии MediaMTX.
    Использует GitHub API.
    """
    _log("MediaMTX: запрос последней версии...")
    req = Request(MEDIAMTX_RELEASES_API, headers=HTTP_HEADERS)
    with urlopen(req, timeout=15) as r:
        data = json.load(r)

    tag = data["tag_name"]                     # например "v1.11.3"
    version = tag.lstrip("v")
    assets = data.get("assets", [])

    # Определяем имя нужного архива по ОС и архитектуре
    if IS_WINDOWS:
        pattern = f"windows_{ARCH}.zip"
    else:
        # На Linux у MediaMTX для ARM64 суффикс "arm64v8"
        arch_for_linux = ARCH if ARCH != "arm64" else "arm64v8"
        pattern = f"linux_{arch_for_linux}.tar.gz"

    for asset in assets:
        name = asset["name"].lower()
        if name.endswith(pattern.lower()):
            return asset["browser_download_url"], version

    raise RuntimeError(
        f"Не найдено сборки MediaMTX для платформы: "
        f"{'windows' if IS_WINDOWS else 'linux'}_{ARCH}"
    )


def install_mediamtx(force: bool = False) -> bool:
    """
    Устанавливает MediaMTX в bin/. Возвращает True, если что-то сделал.
    """
    target = BIN_DIR / MEDIAMTX_BIN
    if target.exists() and not force:
        _log(f"MediaMTX: уже установлен ({target})")
        return False

    url, version = _mediamtx_download_url()
    _log(f"MediaMTX: версия {version}, скачиваю...")

    with tempfile.TemporaryDirectory(prefix="vhs_mediamtx_") as tmp:
        tmp_path = Path(tmp)
        archive_name = url.split("/")[-1]
        archive = tmp_path / archive_name

        _download(url, archive, description="MediaMTX")
        _log("MediaMTX: распаковка...")
        _extract_archive(archive, tmp_path / "extracted")

        # Ищем бинарник на любой глубине
        found = _find_file_recursive(tmp_path / "extracted", MEDIAMTX_BIN)
        if not found:
            raise RuntimeError(
                f"В архиве не найден {MEDIAMTX_BIN}. "
                f"Проверьте сборку вручную."
            )

        BIN_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(found, target)
        _make_executable(target)
        _log(f"MediaMTX: установлен -> {target}")

    marker = _load_marker()
    marker["mediamtx"] = {"version": version, "installed_at": time.time()}
    _save_marker(marker)
    return True


# ======================================================================
# Установка FFmpeg
# ======================================================================
def _ffmpeg_download_url() -> str:
    if IS_WINDOWS:
        return FFMPEG_WINDOWS_URL
    url = FFMPEG_LINUX_URL.get(ARCH)
    if not url:
        raise RuntimeError(
            f"Нет сборки FFmpeg для Linux-{ARCH}. "
            f"Поддерживается: amd64, arm64."
        )
    return url


def install_ffmpeg(force: bool = False) -> bool:
    """
    Устанавливает FFmpeg + ffprobe в bin/. Возвращает True, если что-то сделал.
    """
    target_ffmpeg = BIN_DIR / FFMPEG_BIN
    target_ffprobe = BIN_DIR / FFPROBE_BIN

    if target_ffmpeg.exists() and target_ffprobe.exists() and not force:
        _log(f"FFmpeg: уже установлен ({target_ffmpeg})")
        return False

    url = _ffmpeg_download_url()
    _log(f"FFmpeg: скачиваю {url.split('/')[-1]}...")

    with tempfile.TemporaryDirectory(prefix="vhs_ffmpeg_") as tmp:
        tmp_path = Path(tmp)
        archive_name = url.split("/")[-1]
        archive = tmp_path / archive_name

        _download(url, archive, description="FFmpeg")
        _log("FFmpeg: распаковка...")
        _extract_archive(archive, tmp_path / "extracted")

        found_ffmpeg = _find_file_recursive(tmp_path / "extracted", FFMPEG_BIN)
        found_ffprobe = _find_file_recursive(tmp_path / "extracted", FFPROBE_BIN)

        if not found_ffmpeg:
            raise RuntimeError(f"В архиве не найден {FFMPEG_BIN}")

        BIN_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(found_ffmpeg, target_ffmpeg)
        _make_executable(target_ffmpeg)
        _log(f"FFmpeg: установлен -> {target_ffmpeg}")

        if found_ffprobe:
            shutil.copy2(found_ffprobe, target_ffprobe)
            _make_executable(target_ffprobe)
            _log(f"FFprobe: установлен -> {target_ffprobe}")

    marker = _load_marker()
    marker["ffmpeg"] = {"source": url, "installed_at": time.time()}
    _save_marker(marker)
    return True


# ======================================================================
# Публичные функции
# ======================================================================
def ensure_all(force: bool = False) -> dict:
    """
    Проверяет и при необходимости устанавливает все компоненты.
    Возвращает словарь: {'mediamtx': bool, 'ffmpeg': bool}
    (True = было установлено/обновлено сейчас).
    """
    result = {"mediamtx": False, "ffmpeg": False}

    # Создаём папку bin/ и проверяем права
    try:
        ensure_bin_dir()
    except RuntimeError as e:
        _log(f"Ошибка подготовки папки bin/: {e}", "error")
        return result

    try:
        result["mediamtx"] = install_mediamtx(force=force)
    except Exception as e:
        _log(f"MediaMTX: ошибка установки — {e}", "error")

    try:
        result["ffmpeg"] = install_ffmpeg(force=force)
    except Exception as e:
        _log(f"FFmpeg: ошибка установки — {e}", "error")

    return result


def get_path(binary_name: str) -> Path:
    """Возвращает путь к бинарнику в bin/ (не проверяет существование)."""
    return BIN_DIR / binary_name


def is_ready() -> bool:
    """True, если все компоненты на месте."""
    return (
        (BIN_DIR / MEDIAMTX_BIN).exists()
        and (BIN_DIR / FFMPEG_BIN).exists()
    )


# ======================================================================
# CLI
# ======================================================================
def _cli():
    parser = argparse.ArgumentParser(
        description="Скачивание компонентов VHS-Streamer"
    )
    parser.add_argument("--force", action="store_true",
                        help="переустановить поверх существующих")
    parser.add_argument("--check", action="store_true",
                        help="только проверить, ничего не скачивать")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    print(f"Платформа: {platform.system()} / {ARCH}")
    print(f"Целевая папка: {BIN_DIR}")
    print()

    if args.check:
        ok = is_ready()
        print("Статус:")
        print(f"  MediaMTX: {'✓' if (BIN_DIR / MEDIAMTX_BIN).exists() else '✗'}")
        print(f"  FFmpeg:   {'✓' if (BIN_DIR / FFMPEG_BIN).exists() else '✗'}")
        sys.exit(0 if ok else 1)

    result = ensure_all(force=args.force)
    print()
    print("Итог:")
    print(f"  MediaMTX: {'установлен' if result['mediamtx'] else 'не требовался'}")
    print(f"  FFmpeg:   {'установлен' if result['ffmpeg'] else 'не требовался'}")
    sys.exit(0 if is_ready() else 1)


if __name__ == "__main__":
    _cli()