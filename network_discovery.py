"""Лёгкие методы обнаружения IP-камер без внешних зависимостей.

SSDP/UPnP, Dahua DHDiscover и ISAPI-проба Hikvision.
Используются стандартная библиотека и уже установленный psutil.
"""
import concurrent.futures
import http.client
import ipaddress
import json
import logging
import re
import socket
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
DAHUA_PORT = 37810
ISAPI_PORTS = (80, 8000)


def broadcast_addresses():
    """IPv4 broadcast-адреса интерфейсов + глобальный broadcast."""
    result = {"255.255.255.255"}
    try:
        from netdetect import get_ipv4_interfaces
        for iface in get_ipv4_interfaces():
            ip = iface.get("ip") or ""
            mask = iface.get("netmask") or ""
            if not ip or not mask:
                continue
            try:
                result.add(str(ipaddress.IPv4Network(f"{ip}/{mask}", strict=False).broadcast_address))
            except ValueError:
                pass
    except Exception as exc:
        logger.debug("broadcast interfaces: %s", exc)
    return sorted(result)


def _parse_headers(text):
    headers = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return headers


def _xml_text(root, name):
    for node in root.iter():
        if node.tag.split("}")[-1] == name and node.text:
            return node.text.strip()
    return ""


def _ssdp_description(location, timeout=2.0):
    try:
        req = urllib.request.Request(location, headers={"User-Agent": "VHS-Streamer/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read(256 * 1024)
        root = ET.fromstring(data)
        return {
            "name": _xml_text(root, "friendlyName"),
            "manufacturer": _xml_text(root, "manufacturer"),
            "model": _xml_text(root, "modelName"),
            "serial": _xml_text(root, "serialNumber"),
            "device_type": _xml_text(root, "deviceType"),
        }
    except Exception as exc:
        logger.debug("SSDP description %s: %s", location, exc)
        return {}


def _looks_like_camera(text):
    text = (text or "").lower()
    markers = (
        "camera", "ipcam", "ip cam", "nvr", "dvr", "onvif",
        "hikvision", "dahua", "axis", "reolink", "amcrest",
        "uniview", "tapo", "ezviz", "vivotek", "foscam",
    )
    return any(marker in text for marker in markers)


def discover_ssdp(timeout=4.0):
    """SSDP M-SEARCH; возвращает список устройств с location/name/model."""
    devices = []
    seen = set()
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: ssdp:all\r\n\r\n"
    ).encode("ascii")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)
    try:
        for target in [SSDP_ADDR] + broadcast_addresses():
            try:
                sock.sendto(message, (target, SSDP_PORT))
            except OSError:
                pass
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            text = data.decode("utf-8", errors="replace")
            if not text.upper().startswith("HTTP/"):
                continue
            headers = _parse_headers(text)
            location = headers.get("location", "")
            ip = addr[0]
            if ip in seen:
                continue
            seen.add(ip)
            info = _ssdp_description(location) if location else {}
            marker_text = " ".join((
                info.get("name", ""), info.get("manufacturer", ""),
                info.get("model", ""), info.get("device_type", ""),
                headers.get("server", ""), headers.get("st", ""),
            ))
            if not _looks_like_camera(marker_text):
                logger.debug("SSDP пропущен (не камера): %s %s", ip, marker_text[:120])
                continue
            devices.append({
                "ip": ip,
                "name": info.get("name") or headers.get("server", ""),
                "manufacturer": info.get("manufacturer", ""),
                "model": info.get("model", ""),
                "serial": info.get("serial", ""),
                "server": headers.get("server", ""),
                "location": location,
                "endpoint": location,
                "source": "ssdp",
            })
    finally:
        sock.close()
    return devices


def discover_dahua(timeout=4.0):
    """Dahua/Amcrest DHDiscover.search по UDP broadcast :37810."""
    devices = []
    seen = set()
    payload = json.dumps({
        "method": "DHDiscover.search",
        "params": {"mac": "", "unicast": False},
        "id": 1,
    }).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)
    sock.bind(("", 0))
    try:
        for target in broadcast_addresses():
            try:
                sock.sendto(payload, (target, DAHUA_PORT))
            except OSError:
                pass
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            src_ip = addr[0]
            if src_ip in seen:
                continue
            try:
                response = json.loads(data.decode("utf-8", errors="ignore"))
            except ValueError:
                continue
            if response.get("method") != "DHDiscover.search":
                continue
            params = response.get("params") or {}
            ip = params.get("IPv4Address") or src_ip
            if ip in seen:
                continue
            seen.add(ip)
            devices.append({
                "ip": ip,
                "name": params.get("DeviceType") or "Dahua",
                "model": params.get("DeviceType") or "",
                "mac": params.get("MAC") or "",
                "serial": params.get("SerialNo") or "",
                "firmware": params.get("Version") or "",
                "activated": params.get("Activated"),
                "http_port": 80,
                "endpoint": f"http://{ip}/",
                "source": "dahua",
            })
    finally:
        sock.close()
    return devices


def probe_isapi(ip, port=80, timeout=2.0):
    """GET /ISAPI/System/deviceInfo; возвращает данные или auth_required."""
    conn = http.client.HTTPConnection(ip, int(port), timeout=timeout)
    try:
        conn.request("GET", "/ISAPI/System/deviceInfo",
                     headers={"User-Agent": "VHS-Streamer/1.0"})
        response = conn.getresponse()
        body = response.read(256 * 1024)
        if response.status == 200:
            root = ET.fromstring(body)
            return {
                "ip": ip,
                "name": _xml_text(root, "deviceName") or _xml_text(root, "deviceType"),
                "model": _xml_text(root, "model"),
                "serial": _xml_text(root, "serialNumber"),
                "firmware": _xml_text(root, "firmwareVersion"),
                "http_port": int(port),
                "endpoint": f"http://{ip}" + ("" if int(port) == 80 else f":{port}") + "/",
                "source": "isapi",
                "auth_required": False,
            }
        if response.status in (401, 403):
            auth = (response.getheader("WWW-Authenticate") or "").lower()
            server = (response.getheader("Server") or "").lower()
            marker = auth + " " + server
            if any(token in marker for token in ("ip camera", "hikvision", "dahua", "nvr")):
                return {
                    "ip": ip,
                    "name": "Hikvision",
                    "http_port": int(port),
                    "endpoint": f"http://{ip}" + ("" if int(port) == 80 else f":{port}") + "/",
                    "source": "isapi",
                    "auth_required": True,
                }
            return None
    except Exception as exc:
        logger.debug("ISAPI %s:%s: %s", ip, port, exc)
    finally:
        conn.close()
    return None


def enrich_with_isapi(devices, timeout=2.0):
    """Пробует ISAPI на найденных IP и дополняет записи."""
    result = []
    targets = []
    seen = set()
    for item in devices:
        ip = item.get("ip")
        if not ip or ip in seen:
            continue
        seen.add(ip)
        targets.append((ip, int(item.get("http_port") or ISAPI_PORTS[0])))
    found = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        future_map = {
            pool.submit(probe_isapi, ip, port, timeout): (ip, port)
            for ip, port in targets
        }
        for future in concurrent.futures.as_completed(future_map):
            try:
                item = future.result()
            except Exception:
                item = None
            if item:
                found[item["ip"]] = item
    for item in devices:
        extra = found.get(item.get("ip"))
        if not extra:
            result.append(item)
            continue
        merged = dict(item)
        for key in ("name", "model", "serial", "firmware", "http_port", "endpoint"):
            if extra.get(key):
                merged[key] = extra[key]
        if extra.get("auth_required"):
            merged["auth_required"] = True
        source = merged.get("source") or ""
        if "isapi" not in source.split("+"):
            merged["source"] = (source + "+isapi").strip("+")
        result.append(merged)
    return result
