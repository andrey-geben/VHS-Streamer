"""
Определение сетевых интерфейсов системы.

Используется для формирования списка RTSP-адресов в UI — по одному
на каждый реальный сетевой интерфейс машины.

Гибридный режим:
    * auto_detect: true  — список берётся из системы, конфиг даёт
                           только описания (матч по имени или IP);
    * auto_detect: false — список берётся только из config.yaml.

Отсекаются:
    * loopback (lo, lo0, Loopback)
    * link-local адреса (127.x, 169.254.x)
    * docker/veth/bridge
    * виртуальные адаптеры (VMware, Hyper-V, VirtualBox)
    * VPN-туннели (WireGuard, OpenVPN, PPPoE, Happ, utun и др.)
"""
import logging
import socket

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Фильтры по умолчанию — мусорные интерфейсы
# ----------------------------------------------------------------------
# Префиксы имён — если имя интерфейса НАЧИНАЕТСЯ с одного из них
DEFAULT_EXCLUDE_PREFIXES = (
    "lo",           # loopback (Linux/macOS)
    "lo0",
    "docker",       # docker0, docker_gwbridge
    "veth",         # veth-пары контейнеров
    "br-",          # docker bridge
    "vmnet",        # VMware
    "virbr",        # libvirt
    "utun",         # macOS VPN
    "vEthernet",    # Hyper-V (любой)
    "Bluetooth",
    "Loopback",
    "happ",         # Happ VPN
    "wg",           # WireGuard
    "ppp",          # PPPoE
    "tap",          # OpenVPN tap
    "tun",          # OpenVPN tun
    "VPN",
    "VirtualBox",
    "VMware",
    "Hyper-V",
)

# Маркеры в любой части имени — если встречается где угодно
DEFAULT_EXCLUDE_MARKERS = (
    "-tun",
    "-tap",
    "-vpn",
    "vpn-",
    "_tun",
    "_tap",
    "_vpn",
    " vpn ",
)


# ----------------------------------------------------------------------
# Вспомогательные
# ----------------------------------------------------------------------
def _is_excluded(name: str, exclude_list: list) -> bool:
    """True, если интерфейс надо скрыть из списка."""
    if not name:
        return True

    lname = name.lower()

    # Явный exclude из конфига — приоритет, ищем по подстроке
    for pattern in (exclude_list or []):
        if not pattern:
            continue
        p = pattern.lower()
        if lname == p or lname.startswith(p) or p in lname:
            return True

    # Стандартные префиксы
    for pref in DEFAULT_EXCLUDE_PREFIXES:
        if lname.startswith(pref.lower()):
            return True

    # Маркеры в любой части имени
    for marker in DEFAULT_EXCLUDE_MARKERS:
        if marker.lower() in lname:
            return True

    return False


# ----------------------------------------------------------------------
# Получение интерфейсов
# ----------------------------------------------------------------------
def get_ipv4_interfaces() -> list[dict]:
    """
    Возвращает список сетевых интерфейсов с IPv4-адресами.
    Формат: [{"name": "...", "ip": "...", "netmask": "..."}, ...]
    """
    try:
        import psutil
    except ImportError:
        logger.warning(
            "psutil не установлен — автоопределение интерфейсов недоступно. "
            "Установите: pip install psutil"
        )
        return _fallback_interfaces()

    result = []
    try:
        stats = psutil.net_if_addrs()
    except Exception as e:
        logger.error("psutil.net_if_addrs() failed: %s", e)
        return _fallback_interfaces()

    for name, addrs in stats.items():
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            ip = addr.address
            if not ip:
                continue
            # Исключаем loopback и link-local
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            result.append({
                "name": name,
                "ip": ip,
                "netmask": addr.netmask or "",
            })

    # Сортируем для стабильности: сначала по имени
    result.sort(key=lambda x: x["name"].lower())
    return result


def _fallback_interfaces() -> list[dict]:
    """Резервный вариант без psutil."""
    try:
        hostname = socket.gethostname()
        _, _, ips = socket.gethostbyname_ex(hostname)
        return [
            {"name": f"iface{i}", "ip": ip, "netmask": ""}
            for i, ip in enumerate(ips)
            if not ip.startswith(("127.", "169.254."))
        ]
    except Exception:
        return []


# ----------------------------------------------------------------------
# Построение списка endpoints для API
# ----------------------------------------------------------------------
def build_endpoints(cfg: dict) -> list[dict]:
    """
    Формирует список сетевых endpoints для UI.

    Возвращает:
        [
          {"host": "127.0.0.1", "label": "Локально (на сервере)"},
          {"host": "192.168.1.101", "label": "Сеть охраны труда", "name": "Ethernet"},
          ...
        ]
    """
    net_cfg = cfg.get("network", {}) or {}
    auto_detect = bool(net_cfg.get("auto_detect", True))
    exclude = net_cfg.get("exclude", []) or []
    manual = net_cfg.get("interfaces", []) or []

    # Индексы ручных настроек: по имени и по IP
    manual_by_name = {}
    manual_by_ip = {}
    for m in manual:
        if not isinstance(m, dict):
            continue
        n = (m.get("name") or "").strip()
        ip = (m.get("ip") or "").strip()
        if n:
            manual_by_name[n.lower()] = m
        if ip:
            manual_by_ip[ip] = m

    endpoints = [{"host": "127.0.0.1", "label": "Локально (на сервере)"}]
    seen_ips = {"127.0.0.1"}

    if auto_detect:
        # Основной источник — система
        for iface in get_ipv4_interfaces():
            name = iface["name"]
            ip = iface["ip"]

            if _is_excluded(name, exclude):
                continue
            if ip in seen_ips:
                continue

            seen_ips.add(ip)

            # Описание ищем сначала по имени, потом по IP
            m = manual_by_name.get(name.lower()) or manual_by_ip.get(ip)
            if m:
                label = (m.get("description") or "").strip()
                if not label:
                    label = (m.get("name") or name).strip()
            else:
                label = name

            endpoints.append({
                "host": ip,
                "label": label,
                "name": name,
            })

        # В авторежиме ручные IP из конфига, которых нет в системе,
        # НЕ добавляем — доверяем системе.
    else:
        # Ручной режим — только конфиг
        for m in manual:
            if not isinstance(m, dict):
                continue
            ip = (m.get("ip") or "").strip()
            if not ip or ip in seen_ips:
                continue
            seen_ips.add(ip)
            endpoints.append({
                "host": ip,
                "label": (m.get("description") or m.get("name") or ip).strip(),
                "name": m.get("name"),
            })

    return endpoints