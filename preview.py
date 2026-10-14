"""
Хелперы для loopback-схемы.

Логика:
    Все камеры (IP и аналоговые) публикуются в MediaMTX как raw-пути.
    OpenCV (Camera), рекордер и RTSP-клиенты читают из MediaMTX, а не
    из физической камеры. Это решает проблему эксклюзивного захвата
    на Windows и снижает нагрузку на камеру.
"""
RTSP_HOST = "127.0.0.1"
RTSP_PORT = 8554


def raw_path_name(camera_id: str) -> str:
    """Имя raw-пути в MediaMTX для камеры."""
    return f"__raw_{camera_id}"


def get_loopback_url(camera_id: str) -> str:
    """RTSP URL для чтения raw-потока из MediaMTX."""
    return f"rtsp://{RTSP_HOST}:{RTSP_PORT}/{raw_path_name(camera_id)}"