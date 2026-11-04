"""
SADP (Hikvision) discovery — UDP multicast, поиск Hikvision-камер.

Протокол:
    - Probe отправляется на 239.255.255.250:37020 (UDP multicast)
    - Формат Probe: XML с <Uuid> и <Types>inquiry</Types>
    - Ответ ProbeMatch: XML с полями IPv4Address, MAC, DeviceDescription,
      DeviceSN, CommandPort, HttpPort, SoftwareVersion, DHCP и др.

ВАЖНО: SADP — приватный протокол Hikvision. Найдёт только Hikvision
(и некоторые совместимые OEM). Для Dahua/Axis/Reolink — только ONVIF.

Зависимости: socket, xml.etree, uuid — всё стандартное.
"""
import logging
import socket
import time
import uuid
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# Multicast-адрес и порт SADP (37020, не 3702!)
SADP_MULTICAST = ("239.255.255.250", 37020)

SADP_PROBE_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<Probe>
    <Uuid>{uuid}</Uuid>
    <Types>inquiry</Types>
</Probe>"""


def discover_sadp_devices(timeout: float = 4.0) -> list[dict]:
    """
    Ищет Hikvision-устройства в локальной сети через SADP.

    Возвращает список словарей:
        {
            "ip": "10.1.1.251",
            "mac": "c0-56-e3-fe-42-92",
            "model": "DS-2CD2432F-IW",
            "serial": "DS-2CD2432F-IW20150126...",
            "device_type": "38930",
            "command_port": 8000,
            "http_port": 80,
            "firmware": "V5.2.5build 141201",
            "dhcp": false,
            "activated": true,     # None если поле отсутствует
            "source": "sadp",
        }

    Пустой список — нормально, если Hikvision-камер нет в broadcast-домене.
    """
    probe_uuid = str(uuid.uuid4())
    probe_xml = SADP_PROBE_TEMPLATE.format(uuid=probe_uuid)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)

    try:
        try:
            sock.sendto(probe_xml.encode("utf-8"), SADP_MULTICAST)
        except OSError as e:
            logger.error("SADP: не удалось отправить probe: %s", e)
            return []

        logger.info("SADP: probe отправлен, ждём ответы %.1f сек", timeout)

        devices: dict[str, dict] = {}
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            except OSError as e:
                logger.debug("SADP: ошибка приёма: %s", e)
                continue

            try:
                text = data.decode("utf-8", errors="replace")
                device = _parse_probe_match(text, addr[0])
                if device and device["ip"] not in devices:
                    devices[device["ip"]] = device
                    logger.info(
                        "SADP: найдена камера %s (%s, %s)",
                        device["ip"],
                        device.get("model") or "?",
                        device.get("mac") or "?",
                    )
            except Exception as e:
                logger.debug("SADP: ошибка парсинга ответа от %s: %s",
                             addr[0], e)

        logger.info("SADP: всего найдено %d устройств", len(devices))
        return list(devices.values())

    finally:
        sock.close()


def _parse_probe_match(xml_text: str, sender_ip: str) -> dict | None:
    """
    Разбирает SADP ProbeMatch. Возвращает dict или None.

    Толерантен к namespace (некоторые прошивки отдают XML с xmlns).
    """
    if "ProbeMatch" not in xml_text:
        return None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug("SADP: XML не распарсился: %s", e)
        return None

    # Собираем все поля в плоский dict по local-name (без namespace).
    # Первое вхождение каждого тега — берём его.
    fields: dict[str, str] = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        text = (elem.text or "").strip()
        if text and tag not in fields:
            fields[tag] = text

    ip = fields.get("IPv4Address") or sender_ip
    if not ip:
        return None

    def _int(name: str, default: int | None = None) -> int | None:
        v = fields.get(name)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    dhcp_raw = (fields.get("DHCP") or "").lower()
    dhcp = dhcp_raw in ("true", "1", "yes")

    activated = None
    if "Activated" in fields:
        activated = fields["Activated"].lower() == "true"

    return {
        "ip": ip,
        "mac": fields.get("MAC", ""),
        "model": fields.get("DeviceDescription", ""),
        "serial": fields.get("DeviceSN", ""),
        "device_type": fields.get("DeviceType", ""),
        "command_port": _int("CommandPort", 8000),
        "http_port": _int("HttpPort", 80),
        "firmware": fields.get("SoftwareVersion", ""),
        "dhcp": dhcp,
        "activated": activated,
        "source": "sadp",
    }