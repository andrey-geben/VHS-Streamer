"""
Проверка наличия аудиодорожки в RTSP-потоке камеры.

Используется в UI настроек, чтобы показывать галочку «Захватывать звук»
только когда звук реально есть в потоке камеры. Для аналоговых камер
не используется — там звук идёт отдельным устройством.
"""
import logging
import platform
import subprocess
import threading

import bootstrap

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"


def _ffprobe_path() -> str:
    return str(bootstrap.get_path("ffprobe.exe" if IS_WINDOWS else "ffprobe"))


def probe_audio(url: str, timeout: int = 10) -> bool:
    """
    Запускает ffprobe и ищет аудиодорожку в RTSP-потоке.
    Возвращает True, если аудио есть.
    """
    if not url:
        return False

    ffprobe = _ffprobe_path()
    cmd = [
        ffprobe,
        "-rtsp_transport", "tcp",
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0",
        "-timeout", str(timeout * 1_000_000),  # микросекунды
        url,
    ]

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 5,
            encoding="utf-8", errors="replace",
        )
        output = (r.stdout or "").strip()
        has_audio = "audio" in output.lower()
        logger.info("probe_audio %s -> %s (output=%r)",
                    url, has_audio, output[:120])
        return has_audio
    except subprocess.TimeoutExpired:
        logger.warning("probe_audio: таймаут для %s", url)
        return False
    except Exception as e:
        logger.warning("probe_audio: ошибка для %s: %s", url, e)
        return False


def probe_audio_async(url: str, callback):
    """
    Запускает проверку в фоне, по завершении вызывает callback(has_audio).
    Не блокирует HTTP-запрос.
    """
    def _run():
        result = probe_audio(url)
        try:
            callback(result)
        except Exception as e:
            logger.error("probe_audio callback error: %s", e)

    t = threading.Thread(target=_run, daemon=True, name="probe-audio")
    t.start()