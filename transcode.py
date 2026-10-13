"""
Определение РАБОТАЮЩИХ H264-энкодеров FFmpeg.
Не доверяем списку -encoders: FFmpeg собран с поддержкой многих кодеков,
но конкретный кодек может не работать без GPU/драйвера.
Поэтому каждый кандидат проверяем реальным запуском на тестовом паттерне.
"""
import logging
import platform
import subprocess
import threading
from functools import lru_cache

import bootstrap

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"

KNOWN_ENCODERS = {
    "libx264":    {"label": "libx264 (CPU)",          "type": "cpu"},
    "h264_qsv":   {"label": "Intel Quick Sync (GPU)", "type": "gpu"},
    "h264_nvenc": {"label": "NVIDIA NVENC (GPU)",     "type": "gpu"},
    "h264_amf":   {"label": "AMD AMF (GPU)",          "type": "gpu"},
}

# Приоритет: сначала GPU (по возрастанию задержки), потом CPU
RECOMMEND_ORDER = ["h264_nvenc", "h264_qsv", "h264_amf", "libx264"]

PRESETS = [
    {"id": "mobile_low", "label": "Мобильный (эконом)", "scale": "426:-2", "bitrate": "300k",  "fps": 12},
    {"id": "mobile",     "label": "Мобильный",          "scale": "640:-2", "bitrate": "600k",  "fps": 15},
    {"id": "web",        "label": "Веб / удалённый",    "scale": "854:-2", "bitrate": "1200k", "fps": 20},
    {"id": "full",       "label": "Полное качество",    "scale": "",       "bitrate": "3000k", "fps": 25},
]


def _ffmpeg_path() -> str:
    return str(bootstrap.get_path("ffmpeg.exe" if IS_WINDOWS else "ffmpeg"))


def _test_codec(codec: str) -> bool:
    """
    Реально ли работает кодек? Пробуем закодировать 1 кадр 64x64 из
    синтетического источника. Если кодек не может открыться — FFmpeg
    вернёт ненулевой код возврата.
    """
    cmd = [
        _ffmpeg_path(),
        "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=64x64:rate=1",
        "-t", "1",
        "-c:v", codec,
        "-f", "null", "-",
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()
            logger.info("Кодек %s НЕ работает: %s",
                        codec, err[-1] if err else "unknown error")
            return False
        logger.info("Кодек %s — OK", codec)
        return True
    except subprocess.TimeoutExpired:
        logger.warning("Кодек %s: таймаут проверки", codec)
        return False
    except Exception as e:
        logger.warning("Кодек %s: ошибка проверки: %s", codec, e)
        return False


@lru_cache(maxsize=1)
def detect_encoders() -> dict:
    """
    Возвращает только РАБОТАЮЩИЕ кодеки.
    Каждый проверяется реальным запуском FFmpeg (параллельно).
    """
    results = {}
    lock = threading.Lock()

    def check(name, info):
        if _test_codec(name):
            with lock:
                results[name] = dict(info)

    threads = []
    for name, info in KNOWN_ENCODERS.items():
        t = threading.Thread(target=check, args=(name, info), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=10)

    logger.info("Рабочие кодеки: %s", list(results.keys()))
    return results


def get_recommended_codec() -> str:
    """Лучший доступный кодек для текущего железа."""
    encoders = detect_encoders()
    for codec in RECOMMEND_ORDER:
        if codec in encoders:
            return codec
    return "libx264"


def get_loopback_codec() -> str:
    """
    Кодек для raw/loopback-потока.
    Использует ту же логику, что и транскодирование: первый рабочий из списка.
    Пользователь может переопределить через config: mediamtx.loopback_codec.
    """
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).parent / "config.yaml"
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            forced = (cfg.get("mediamtx", {}) or {}).get("loopback_codec")
            if forced and forced != "auto":
                encoders = detect_encoders()
                if forced in encoders:
                    logger.info("Loopback-кодек задан в конфиге: %s", forced)
                    return forced
                else:
                    logger.warning(
                        "В конфиге указан кодек %r, но он не работает. "
                        "Использую автоопределение.", forced,
                    )
    except Exception as e:
        logger.debug("Не удалось прочитать loopback_codec из конфига: %s", e)

    return get_recommended_codec()


def list_codecs_for_api() -> dict:
    """Готовит данные для UI."""
    encoders = detect_encoders()
    return {
        "available": [
            {"name": name, "label": info["label"], "type": info["type"]}
            for name, info in encoders.items()
        ],
        "recommended": get_recommended_codec(),
        "presets": PRESETS,
    }