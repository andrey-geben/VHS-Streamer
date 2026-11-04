"""
Обнаружение локальных устройств захвата видео.
Windows: DirectShow через FFmpeg -list_devices
Linux:   /dev/video* + опционально v4l2-ctl
"""
import glob
import logging
import platform
import re
import subprocess

import bootstrap

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

FFMPEG_BIN_NAME = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
FFMPEG_PATH = str(bootstrap.get_path(FFMPEG_BIN_NAME))

# ----------------------------------------------------------------------
# Два формата вывода FFmpeg -list_devices, которые надо поддержать:
#
# (A) Новый — FFmpeg 6.x / 7.x / 8.x / 9.x:
#     [in#0 @ 000001ff] "HD User Facing" (video)
#     [in#0 @ 000001ff]   Alternative name "@device_pnp_\\?\usb#..."
#     [in#0 @ 000001ff] "Набор микрофонов (Realtek(R) Audio)" (audio)
#
#     Заголовков "DirectShow video devices" нет.
#     Тип определяется по суффиксу (video) / (audio).
#
# (B) Старый — FFmpeg 4.x / 5.x:
#     [dshow @ 000001ff] DirectShow video devices (some info)
#     [dshow @ 000001ff]  "HD User Facing"
#     [dshow @ 000001ff]  Alternative name "@device_pnp_\\?\usb#..."
#     [dshow @ 000001ff] DirectShow audio devices
#     [dshow @ 000001ff]  "Микрофон"
#
#     Разделы отделены заголовками; тип определяется по секции.
# ----------------------------------------------------------------------
_NEW_FORMAT_RE = re.compile(
    r'^\s*\[[^\]]*\]\s+"([^"]+)"\s+\((video|audio)\)\s*$'
)
_OLD_FORMAT_NAME_RE = re.compile(
    r'^\s*\[[^\]]*\]\s+"([^"]+)"\s*$'
)


def _discover_windows() -> list[dict]:
    """
    Возвращает список устройств:
        {name, capture_index, type='analog', transport='dshow', id_suggested}
    Только видеоустройства; аудио отбрасывается.
    """
    try:
        r = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
        )
    except FileNotFoundError:
        logger.error("FFmpeg не найден: %s", FFMPEG_PATH)
        return []
    except subprocess.TimeoutExpired:
        logger.warning("FFmpeg -list_devices не ответил за 15 сек")
        return []
    except Exception as e:
        logger.warning("FFmpeg -list_devices упал: %s", e)
        return []

    devices: list[dict] = []
    in_video_legacy = False  # флаг активен только для старого формата

    for line in r.stderr.splitlines():
        # 1) "Alternative name ..." — служебная строка, всегда пропускаем
        if "Alternative name" in line:
            continue

        # 2) Заголовки старого формата переключают секцию
        if "DirectShow video devices" in line:
            in_video_legacy = True
            continue
        if "DirectShow audio devices" in line:
            in_video_legacy = False
            continue

        # 3) Новый формат: "Имя" (video) / "Имя" (audio)
        m = _NEW_FORMAT_RE.match(line)
        if m:
            if m.group(2) == "video":
                devices.append({
                    "name": m.group(1),
                    "type": "analog",
                    "transport": "dshow",
                })
            # audio — тихо игнорируем, в задаче №1 нужны только видео
            continue

        # 4) Старый формат: имя внутри секции "DirectShow video devices"
        if in_video_legacy:
            m2 = _OLD_FORMAT_NAME_RE.match(line)
            if m2:
                devices.append({
                    "name": m2.group(1),
                    "type": "analog",
                    "transport": "dshow",
                })

    # capture_index 0..N-1 в порядке появления (DirectShow так и работает)
    for i, d in enumerate(devices):
        d["capture_index"] = i
        # предлагаемое id: usb_hd_user_facing
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", d["name"]).strip("_").lower()
        d["id_suggested"] = ("usb_" + slug)[:32]

    if not devices:
        logger.info(
            "DirectShow: видеоустройства не найдены (stderr FFmpeg: %d строк)",
            len(r.stderr.splitlines()),
        )

    return devices


def _discover_linux() -> list[dict]:
    devices = []
    for path in sorted(glob.glob("/dev/video*")):
        m = re.search(r"(\d+)$", path)
        if not m:
            continue
        idx = int(m.group(1))
        name = path
        # попробуем получить человекочитаемое имя через v4l2-ctl (если установлен)
        try:
            r = subprocess.run(
                ["v4l2-ctl", "-d", path, "--info"],
                capture_output=True, text=True, timeout=3,
            )
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("Card type"):
                    name = line.split(":", 1)[1].strip() or path
                    break
        except Exception:
            pass
        devices.append({
            "name": name,
            "type": "analog",
            "transport": "v4l2",
            "capture_index": idx,
            "source": path,
            "id_suggested": f"v4l2_{idx}",
        })
    return devices


def discover_local_devices() -> list[dict]:
    """
    Возвращает список локальных устройств захвата видео.
    Windows — DirectShow через FFmpeg.
    Linux — /dev/video* + v4l2-ctl (опционально).
    macOS — пустой список с предупреждением (в задаче №1 не поддерживается).
    """
    if IS_WINDOWS:
        return _discover_windows()
    if IS_LINUX:
        return _discover_linux()
    logger.warning("Обнаружение локальных устройств не реализовано для %s",
                   platform.system())
    return []


# ======================================================================
# Проверка модуля вручную (не для production)
# ======================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    print(f"Платформа: {platform.system()} {platform.machine()}")
    print(f"FFmpeg:    {FFMPEG_PATH}")
    print()

    devs = discover_local_devices()
    if not devs:
        print("Устройства не найдены. Это нормально, если камер нет или")
        print("FFmpeg нет прав на перечисление устройств.")
        print("\nЕсли камера точно есть — запустите _diag_dshow.py,")
        print("он покажет сырой stderr FFmpeg.")
    else:
        print(f"Найдено устройств: {len(devs)}")
        for d in devs:
            print(f"  - name={d['name']!r}")
            print(f"    type={d.get('type')!r} transport={d.get('transport')!r}")
            print(f"    capture_index={d.get('capture_index')!r} "
                  f"id_suggested={d.get('id_suggested')!r}")
            if d.get("source"):
                print(f"    source={d['source']!r}")