"""
Управление MediaMTX: запуск процесса, добавление/удаление потоков
через HTTP API, чтение статуса, генерация FFmpeg-команд.
"""
import platform
import subprocess
import threading
import time
import logging
from pathlib import Path

import requests

import bootstrap

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

MEDIAMTX_API = "http://127.0.0.1:9997"
RTSP_PORT = 8554

FFMPEG_BIN_NAME = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
FFMPEG_PATH = str(bootstrap.get_path(FFMPEG_BIN_NAME)).replace("\\", "/")


# ----------------------------------------------------------------------
# Генераторы команд
# ----------------------------------------------------------------------
def _quote(s: str) -> str:
    s = str(s)
    if " " in s and not (s.startswith('"') and s.endswith('"')):
        return f'"{s}"'
    return s


def build_raw_publisher_cmd(camera) -> str:
    """FFmpeg-команда для чтения физической камеры и публикации в raw-путь."""
    # --- Вход ---
    if camera.type == "analog":
        if IS_LINUX:
            try:
                idx = int(camera.source)
                input_args = f"-f v4l2 -i /dev/video{idx}"
            except (TypeError, ValueError):
                input_args = f"-f v4l2 -i {camera.source}"
        else:
            name = camera.source
            try:
                int(name)
                logger.warning(
                    "[%s] На Windows для аналоговой камеры нужно указывать "
                    "ИМЯ устройства dshow, а не индекс. Сейчас: %r.",
                    camera.id, name,
                )
            except (TypeError, ValueError):
                pass
            input_args = f"-f dshow -i video={_quote(name)}"
    else:
        input_args = f"-rtsp_transport tcp -i {_quote(camera.source)}"

    # --- Кодек: пробуем аппаратный, иначе libx264 ---
    try:
        from transcode import get_loopback_codec
        codec = get_loopback_codec()
    except Exception:
        codec = "libx264"

    logger.info("[%s] Loopback-кодек: %s", camera.id, codec)

    # --- Параметры энкодера ---
    if codec == "h264_nvenc":
        encode = (
            "-c:v h264_nvenc -preset p4 -tune ll "
            "-profile:v main -pix_fmt yuv420p "
            "-b:v 1500k -maxrate 1500k -bufsize 3000k "
            "-g 25 -bf 0"
        )
    elif codec == "h264_qsv":
        encode = (
            "-c:v h264_qsv -preset veryfast -look_ahead 0 "
            "-profile:v baseline -level 3.1 "
            "-pix_fmt nv12 "
            "-b:v 1500k -maxrate 1500k -bufsize 3000k "
            "-g 25 -bf 0"
        )
    elif codec == "h264_amf":
        encode = (
            "-c:v h264_amf -quality speed -usage lowlatency "
            "-profile:v main -pix_fmt yuv420p "
            "-b:v 1500k -maxrate 1500k "
            "-g 25"
        )
    else:
        # libx264 (CPU)
        encode = (
            "-c:v libx264 -preset veryfast "
            "-profile:v main -pix_fmt yuv420p "
            "-b:v 1500k -maxrate 1500k -bufsize 3000k "
            "-g 25 -bf 0 -sc_threshold 0"
        )

    # --- RTSP-выход ---
    # -muxdelay 0 -muxpreload 0: без буферизации при мультиплексировании
    # (уменьшает задержку в VLC с ~0.3 до ~0.1 сек)
    rtsp_out = (
        f"-f rtsp -rtsp_transport tcp "
        f"-pkt_size 800 "
        f"-muxdelay 0 -muxpreload 0 "
        f"-bsf:v h264_mp4toannexb "
        f"rtsp://localhost:{RTSP_PORT}/$MTX_PATH"
    )

    return (
        f'"{FFMPEG_PATH}" -nostdin -loglevel warning '
        f"{input_args} "
        f"{encode} -an "
        f"{rtsp_out}"
    )


def build_transcode_cmd(stream_cfg: dict) -> str:
    """
    FFmpeg-команда для транскодирования из raw-потока в пользовательский.
    Поддерживает libx264 (CPU) и GPU-кодеки: NVENC, QSV, AMF.
    """
    codec = stream_cfg.get("codec", "libx264")
    bitrate = stream_cfg.get("bitrate", "1000k")
    scale = stream_cfg.get("scale")
    fps = stream_cfg.get("fps")

    input_args = (
        f"-rtsp_transport tcp "
        f"-i rtsp://localhost:{RTSP_PORT}/{stream_cfg['_raw_path']}"
    )

    # Видеофильтры (масштаб, fps)
    vf = []
    if scale:
        vf.append(f"scale={scale}")
    if fps:
        vf.append(f"fps={fps}")
    vf_arg = f"-vf \"{','.join(vf)}\" " if vf else ""

    # Параметры под конкретный кодек
    if codec == "libx264":
        encode = (
            f"-c:v libx264 -preset ultrafast -tune zerolatency "
            f"-profile:v baseline -pix_fmt yuv420p "
            f"-b:v {bitrate} -maxrate {bitrate} -bufsize 2M "
            f"-g 25 -bf 0"
        )
    elif codec == "h264_qsv":
        encode = (
            f"-c:v h264_qsv -preset veryfast -look_ahead 0 "
            f"-profile:v baseline -level 3.1 -pix_fmt nv12 "
            f"-b:v {bitrate} -maxrate {bitrate} -bufsize 2M "
            f"-g 25 -bf 0"
        )
    elif codec == "h264_nvenc":
        encode = (
            f"-c:v h264_nvenc -preset p1 -tune ll "
            f"-profile:v baseline -pix_fmt yuv420p "
            f"-b:v {bitrate} -maxrate {bitrate} -bufsize 2M "
            f"-g 25 -bf 0"
        )
    elif codec == "h264_amf":
        encode = (
            f"-c:v h264_amf -quality speed -usage lowlatency "
            f"-profile:v main -pix_fmt yuv420p "
            f"-b:v {bitrate} -maxrate {bitrate} "
            f"-g 25"
        )
    else:
        encode = (
            f"-c:v {codec} -pix_fmt yuv420p "
            f"-b:v {bitrate} -g 25"
        )

    rtsp_out = (
        f"-f rtsp -rtsp_transport tcp -pkt_size 800 "
        f"-muxdelay 0 -muxpreload 0 "
        f"-bsf:v h264_mp4toannexb "
        f"rtsp://localhost:{RTSP_PORT}/$MTX_PATH"
    )

    return (
        f'"{FFMPEG_PATH}" -nostdin -loglevel warning '
        f"{input_args} "
        f"{vf_arg}{encode} -an "
        f"{rtsp_out}"
    )


# ----------------------------------------------------------------------
# Менеджер
# ----------------------------------------------------------------------
class MediaMTXManager:
    def __init__(self, binary_path: Path, config_path: Path):
        self.binary = Path(binary_path)
        self.config = Path(config_path)
        self.process: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None

    def start(self, timeout: float = 15.0):
        if self.process and self.process.poll() is None:
            logger.warning("MediaMTX уже запущен (PID=%s)", self.process.pid)
            return

        if not self.binary.exists():
            raise FileNotFoundError(f"MediaMTX не найден: {self.binary}")

        logger.info("Запускаю MediaMTX: %s", self.binary)
        self.process = subprocess.Popen(
            [str(self.binary), str(self.config)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            name="mediamtx-stdout",
            daemon=True,
        )
        self._stdout_thread.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"MediaMTX упал при старте (код {self.process.returncode})"
                )
            try:
                r = requests.get(
                    f"{MEDIAMTX_API}/v3/config/global/get", timeout=1
                )
                if r.status_code == 200:
                    logger.info("MediaMTX готов (PID=%s)", self.process.pid)
                    return
            except requests.ConnectionError:
                pass
            time.sleep(0.3)

        raise RuntimeError(f"MediaMTX не поднял API за {timeout} секунд")

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                low = line.lower()
                if any(k in low for k in
                       ("error", "fail", "fatal", "panic", "cannot",
                        "unable", "invalid")):
                    logger.error("[mediamtx] %s", line)
                elif "warn" in low:
                    logger.warning("[mediamtx] %s", line)
                else:
                    logger.debug("[mediamtx] %s", line)
        except Exception as e:
            logger.debug("Поток чтения stdout MediaMTX остановлен: %s", e)

    def stop(self):
        if not self.process:
            return
        logger.info("Останавливаю MediaMTX")
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.process = None

    def add_path(self, name: str, config: dict):
        self.remove_path(name, silent=True)
        r = requests.post(
            f"{MEDIAMTX_API}/v3/config/paths/add/{name}",
            json=config, timeout=5,
        )
        if r.status_code >= 400:
            logger.error("Ошибка добавления пути %s: %s", name, r.text)
            r.raise_for_status()
        logger.info("MediaMTX: путь добавлен — %s", name)

    def remove_path(self, name: str, silent: bool = False):
        r = requests.delete(
            f"{MEDIAMTX_API}/v3/config/paths/delete/{name}", timeout=5,
        )
        if r.status_code >= 400:
            if not silent:
                logger.warning("Не удалось удалить путь %s: %s", name, r.text)
            return
        logger.info("MediaMTX: путь удалён — %s", name)

    def list_paths(self) -> list[dict]:
        try:
            r = requests.get(
                f"{MEDIAMTX_API}/v3/config/paths/list", timeout=5
            )
            r.raise_for_status()
            return r.json().get("items", []) or []
        except Exception as e:
            logger.warning("Не удалось получить список путей: %s", e)
            return []

    def get_path_status(self, name: str) -> dict | None:
        try:
            r = requests.get(
                f"{MEDIAMTX_API}/v3/paths/get/{name}", timeout=5
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception:
            return None


# ----------------------------------------------------------------------
# Конфиги путей
# ----------------------------------------------------------------------
def build_raw_path_config(camera) -> dict:
    """
    Конфиг raw-пути для MediaMTX.
    - IP-камера: source = rtsp://... (MediaMTX читает напрямую).
    - Аналоговая: source = publisher + runOnInit с FFmpeg.
    """
    if camera.type == "ip":
        return {
            "source": camera.source,
            "sourceOnDemand": True,
            "sourceOnDemandCloseAfter": "10s",
            "rtspTransport": "tcp",
        }
    cmd = build_raw_publisher_cmd(camera)
    return {
        "source": "publisher",
        "runOnInit": cmd,
        "runOnInitRestart": True,
    }


def build_stream_path_config(stream_cfg: dict, raw_path: str) -> dict:
    """
    Конфиг пользовательского потока.
    copy=true — source = rtsp://localhost:8554/<raw>
    copy=false — runOnInit с FFmpeg-транскодированием.
    """
    if stream_cfg.get("copy", True):
        return {
            "source": f"rtsp://localhost:{RTSP_PORT}/{raw_path}",
            "sourceOnDemand": True,
            "sourceOnDemandCloseAfter": "10s",
            "rtspTransport": "tcp",
        }
    cfg_for_cmd = dict(stream_cfg)
    cfg_for_cmd["_raw_path"] = raw_path
    cmd = build_transcode_cmd(cfg_for_cmd)
    return {
        "source": "publisher",
        "runOnInit": cmd,
        "runOnInitRestart": True,
    }