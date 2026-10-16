"""
Шаблоны RTSP-путей и определение вендора IP-камер.

Работает без ONVIF — только по строке RTSP-URL. Помогает:
    * определить вендора камеры,
    * сгенерировать правильный RTSP-URL по IP + вендор + логин/пароль.

Поддерживаются: Hikvision, Dahua, Reolink, Axis.
"""
import logging
import re
from urllib.parse import quote

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Шаблоны вендоров
# ----------------------------------------------------------------------
VENDORS = {
    "hikvision": {
        "label": "Hikvision",
        "default_port": 554,
        "markers": [
            "/Streaming/Channels/",
            "/ISAPI/",
            "/hikvision",
        ],
        "streams": [
            {"id": "main", "label": "Основной", "path": "/Streaming/Channels/101"},
            {"id": "sub",  "label": "Субпоток",  "path": "/Streaming/Channels/102"},
            {"id": "third","label": "Третий",    "path": "/Streaming/Channels/103"},
        ],
    },
    "dahua": {
        "label": "Dahua / Amcrest",
        "default_port": 554,
        "markers": [
            "/cam/realmonitor",
        ],
        "streams": [
            {"id": "main", "label": "Основной", "path": "/cam/realmonitor?channel=1&subtype=0"},
            {"id": "sub",  "label": "Субпоток",  "path": "/cam/realmonitor?channel=1&subtype=1"},
        ],
    },
    "reolink": {
        "label": "Reolink",
        "default_port": 554,
        "markers": [
            "/h264Preview_",
            "/Preview_",
            "/reolink",
        ],
        "streams": [
            {"id": "main", "label": "Основной", "path": "/h264Preview_01_main"},
            {"id": "sub",  "label": "Субпоток",  "path": "/h264Preview_01_sub"},
        ],
    },
    "axis": {
        "label": "Axis",
        "default_port": 554,
        "markers": [
            "/axis-media/",
            "/axis/",
        ],
        "streams": [
            {"id": "main", "label": "Основной", "path": "/axis-media/media.amp"},
            {"id": "profile2", "label": "Профиль 2", "path": "/axis-media/media.amp?streamprofile=Quality"},
        ],
    },
    "unknown": {
        "label": "Неизвестный",
        "default_port": 554,
        "markers": [],
        "streams": [],
    },
}


# ----------------------------------------------------------------------
# Определение вендора по RTSP-URL
# ----------------------------------------------------------------------
def detect_vendor(rtsp_url: str) -> str:
    """
    Возвращает ключ вендора по RTSP-URL или 'unknown'.
    """
    if not rtsp_url:
        return "unknown"

    u = rtsp_url.lower()
    for key, v in VENDORS.items():
        if key == "unknown":
            continue
        for marker in v["markers"]:
            if marker.lower() in u:
                return key
    return "unknown"


def get_vendor_label(vendor_key: str) -> str:
    v = VENDORS.get(vendor_key) or VENDORS["unknown"]
    return v["label"]


def get_vendor_streams(vendor_key: str) -> list[dict]:
    v = VENDORS.get(vendor_key) or VENDORS["unknown"]
    return list(v["streams"])


# ----------------------------------------------------------------------
# Парсинг и сборка RTSP-URL
# ----------------------------------------------------------------------
_RTSP_RE = re.compile(
    r"^rtsp://"
    r"(?:(?P<user>[^:@/]+)(?::(?P<pass>[^@/]*))?@)?"
    r"(?P<host>[^:/]+)"
    r"(?::(?P<port>\d+))?"
    r"(?P<path>/.*)?$",
    re.IGNORECASE,
)


def parse_rtsp(rtsp_url: str) -> dict | None:
    """
    Разбирает RTSP-URL на составляющие.
    Возвращает dict или None при ошибке.
    """
    if not rtsp_url:
        return None
    m = _RTSP_RE.match(rtsp_url.strip())
    if not m:
        return None
    return {
        "user": m.group("user") or "",
        "password": m.group("pass") or "",
        "host": m.group("host") or "",
        "port": int(m.group("port")) if m.group("port") else 554,
        "path": m.group("path") or "",
    }


def build_rtsp_url(host: str,
                   port: int = 554,
                   path: str = "",
                   user: str = "",
                   password: str = "") -> str:
    """
    Собирает RTSP-URL.
    Логин и пароль кодируются для спецсимволов (@, :, / и т.п.).
    """
    if not host:
        return ""
    host = host.strip()
    # Убираем протокол, если пользователь случайно вставил
    host = re.sub(r"^rtsps?://", "", host, flags=re.IGNORECASE)
    # Убираем возможный слэш в конце
    host = host.rstrip("/")

    # Если в host уже есть логин — не дублируем
    if "@" in host:
        base = f"rtsp://{host}"
    else:
        auth = ""
        if user:
            auth = quote(user, safe="")
            if password:
                auth += ":" + quote(password, safe="")
            auth += "@"
        base = f"rtsp://{auth}{host}"

    # Порт добавляем, если не 554
    if port and port != 554 and ":" not in base.split("@")[-1]:
        base += f":{port}"

    if path and not path.startswith("/"):
        path = "/" + path

    return base + (path or "")


# ----------------------------------------------------------------------
# Автогенерация для UI
# ----------------------------------------------------------------------
def autofill_rtsp(vendor_key: str,
                  host: str,
                  port: int,
                  stream_id: str,
                  user: str = "",
                  password: str = "") -> str:
    """
    Генерирует RTSP-URL по вендору, хосту и выбранному потоку.
    """
    v = VENDORS.get(vendor_key)
    if not v:
        return ""
    streams = v["streams"]
    if not streams:
        return ""

    # Ищем поток по id
    chosen = None
    for s in streams:
        if s["id"] == stream_id:
            chosen = s
            break
    if not chosen:
        chosen = streams[0]

    return build_rtsp_url(
        host=host,
        port=port or v["default_port"],
        path=chosen["path"],
        user=user,
        password=password,
    )


def list_vendors_for_api() -> list[dict]:
    """Готовит данные для UI."""
    return [
        {
            "key": k,
            "label": v["label"],
            "default_port": v["default_port"],
            "streams": v["streams"],
        }
        for k, v in VENDORS.items()
    ]