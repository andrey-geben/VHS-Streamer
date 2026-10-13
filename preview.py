"""
Loopback для аналоговых камер.

Проблема: на Windows dshow камера — эксклюзивный ресурс.
Одновременно открыть её в OpenCV (для MJPEG) и в FFmpeg (для RTSP) нельзя.

Решение: FFmpeg (через MediaMTX) — единственный, кто читает физическую
камеру. OpenCV (Camera) читает RTSP-поток из MediaMTX.
"""
RTSP_HOST = "127.0.0.1"
RTSP_PORT = 8554


def raw_path_name(camera_id: str) -> str:
    """Имя raw-пути в MediaMTX для камеры."""
    return f"__raw_{camera_id}"


def needs_loopback(camera_id: str, camera_type: str, streams_cfg: list) -> bool:
    """True, если для аналоговой камеры нужен loopback."""
    if camera_type != "analog":
        return False
    if not streams_cfg:
        return False
    return any(
        s.get("camera_id") == camera_id and s.get("enabled", True)
        for s in streams_cfg
    )


def get_loopback_url(camera_id: str) -> str:
    """RTSP URL для чтения raw-потока из MediaMTX."""
    return f"rtsp://{RTSP_HOST}:{RTSP_PORT}/{raw_path_name(camera_id)}"