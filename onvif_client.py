"""
Минимальный ONVIF-клиент на стандартных библиотеках.

Функции:
    * discover_devices()   — WS-Discovery по multicast, поиск ONVIF-камер
    * OnvifDevice          — клиент одной камеры (профили, stream URI, PTZ)
    * get_rtsp_urls()      — высокоуровневая обёртка: получить все RTSP-ссылки
    * get_ptz_info()       — проверить поддержку PTZ и получить конфигурации
    * ptz_move() / ptz_stop() / ptz_home() / ptz_goto_preset() и др.

Зависимости: только requests и xml.etree (стандартный). Никаких lxml,
zeep или WSDiscovery — чтобы не тянуть нативные библиотеки в .exe.
"""
import hashlib
import logging
import re
import socket
import time
import uuid
from base64 import b64encode
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Константы WS-Discovery
# ----------------------------------------------------------------------
WS_DISCOVERY_MULTICAST = ("239.255.255.250", 3702)

WS_DISCOVERY_PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope
    xmlns:e="http://www.w3.org/2003/05/soap-envelope"
    xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
    xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{message_id}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""


# ----------------------------------------------------------------------
# WS-Discovery
# ----------------------------------------------------------------------
def discover_devices(timeout: float = 4.0) -> list:
    """
    Ищет ONVIF-камеры в локальной сети через WS-Discovery.
    Возвращает список словарей: endpoint, ip, name, hardware, scopes.
    """
    message_id = str(uuid.uuid4())
    probe = WS_DISCOVERY_PROBE_TEMPLATE.format(message_id=message_id)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)

    try:
        sock.sendto(probe.encode("utf-8"), WS_DISCOVERY_MULTICAST)
        logger.info("WS-Discovery: probe отправлен, ждём ответы %.1f сек", timeout)

        devices = {}
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                break

            try:
                text = data.decode("utf-8", errors="replace")
                device = _parse_probe_match(text, addr[0])
                if device and device["endpoint"] not in devices:
                    devices[device["endpoint"]] = device
                    logger.info("WS-Discovery: найдена камера %s (%s)",
                                device["ip"], device.get("name") or "без имени")
            except Exception as e:
                logger.debug("WS-Discovery: ошибка парсинга ответа: %s", e)

        logger.info("WS-Discovery: всего найдено %d устройств", len(devices))
        return list(devices.values())

    finally:
        sock.close()


def _parse_probe_match(xml_text: str, sender_ip: str):
    """Разбирает ответ ProbeMatch. Возвращает dict или None."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug("WS-Discovery: XML не распарсился: %s", e)
        return None

    xaddrs = None
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "XAddrs" and elem.text:
            xaddrs = elem.text.strip()
            break

    if not xaddrs:
        return None

    endpoint = xaddrs.split()[0] if xaddrs else None
    if not endpoint:
        return None

    ip = sender_ip
    m = re.match(r"https?://([\d\.]+)", endpoint)
    if m:
        ip = m.group(1)

    scopes_text = ""
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "Scopes" and elem.text:
            scopes_text = elem.text.strip()
            break

    scopes = scopes_text.split() if scopes_text else []

    name = ""
    hardware = ""
    for sc in scopes:
        if "/name/" in sc:
            name = sc.split("/name/")[-1]
        elif "/hardware/" in sc:
            hardware = sc.split("/hardware/")[-1]

    return {
        "endpoint": endpoint,
        "address": endpoint,
        "xaddrs": xaddrs,
        "scopes": scopes,
        "ip": ip,
        "name": name,
        "hardware": hardware,
    }


# ----------------------------------------------------------------------
# SOAP-клиент для одного устройства
# ----------------------------------------------------------------------
class OnvifDevice:
    """
    Клиент одной ONVIF-камеры.
    Использует SOAP-over-HTTP с WS-Security UsernameToken.
    """

    NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
    NS_WSSE = (
        "http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-wssecurity-secext-1.0.xsd"
    )
    NS_WSU = (
        "http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-wssecurity-utility-1.0.xsd"
    )
    NS_TDS = "http://www.onvif.org/ver10/device/wsdl"
    NS_TRT = "http://www.onvif.org/ver10/media/wsdl"

    def __init__(self, endpoint, username="", password="", timeout=8):
        self.endpoint = endpoint
        self.username = username
        self.password = password
        self.timeout = timeout

        # Кэш вычисленных endpoint'ов
        self._media_endpoint = None
        self._ptz_endpoint_cache = None

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------
    def _ws_security_header(self) -> str:
        """Формирует WS-Security UsernameToken."""
        if not self.username:
            return ""

        nonce = uuid.uuid4().bytes
        nonce_b64 = b64encode(nonce).decode()
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        digest_input = nonce + created.encode() + self.password.encode()
        digest = b64encode(hashlib.sha1(digest_input).digest()).decode()

        return (
            '<wsse:Security s:mustUnderstand="1" '
            'xmlns:wsse="' + self.NS_WSSE + '" '
            'xmlns:wsu="' + self.NS_WSU + '">'
            "<wsse:UsernameToken>"
            "<wsse:Username>" + self.username + "</wsse:Username>"
            '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
            + digest +
            "</wsse:Password>"
            '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
            + nonce_b64 +
            "</wsse:Nonce>"
            "<wsu:Created>" + created + "</wsu:Created>"
            "</wsse:UsernameToken>"
            "</wsse:Security>"
        )

    def _soap_call(self, url: str, action: str, body_xml: str) -> ET.Element:
        """Отправляет SOAP-запрос, возвращает корневой элемент ответа."""
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope'
            ' xmlns:s="' + self.NS_SOAP + '"'
            ' xmlns:tds="' + self.NS_TDS + '"'
            ' xmlns:trt="' + self.NS_TRT + '"'
            ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
            ' xmlns:tt="http://www.onvif.org/ver10/schema">'
            "<s:Header>" + self._ws_security_header() + "</s:Header>"
            "<s:Body>" + body_xml + "</s:Body>"
            "</s:Envelope>"
        )

        headers = {
            "Content-Type": 'application/soap+xml; charset=utf-8; action="' + action + '"',
        }

        try:
            r = requests.post(
                url,
                data=envelope.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
            if r.status_code == 401:
                r = requests.post(
                    url,
                    data=envelope.encode("utf-8"),
                    headers=headers,
                    timeout=self.timeout,
                    auth=HTTPDigestAuth(self.username, self.password),
                )
            r.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError("SOAP-запрос не удался: " + str(e))

        try:
            return ET.fromstring(r.content)
        except ET.ParseError as e:
            raise RuntimeError("Ответ не парсится как XML: " + str(e))

    @staticmethod
    def _find_text(root: ET.Element, local_name: str):
        """Ищет первый элемент по local-name, возвращает его текст или None."""
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == local_name:
                text = (elem.text or "").strip()
                return text or None
        return None

    # ------------------------------------------------------------------
    # Информация об устройстве
    # ------------------------------------------------------------------
    def get_device_info(self) -> dict:
        """GetDeviceInformation — модель, производитель, серийник."""
        body = "<tds:GetDeviceInformation/>"
        action = self.NS_TDS + "/GetDeviceInformation"

        try:
            root = self._soap_call(self.endpoint, action, body)
        except Exception as e:
            logger.warning("GetDeviceInformation: %s", e)
            return {}

        return {
            "manufacturer": self._find_text(root, "Manufacturer") or "",
            "model": self._find_text(root, "Model") or "",
            "firmware": self._find_text(root, "FirmwareVersion") or "",
            "serial": self._find_text(root, "SerialNumber") or "",
            "hardware": self._find_text(root, "HardwareId") or "",
        }

    # ------------------------------------------------------------------
    # Media
    # ------------------------------------------------------------------
    def _get_media_endpoint(self) -> str:
        """Возвращает media endpoint (может отличаться от device endpoint)."""
        if self._media_endpoint:
            return self._media_endpoint

        body = (
            "<tds:GetCapabilities>"
            "<tds:Category>Media</tds:Category>"
            "</tds:GetCapabilities>"
        )
        action = self.NS_TDS + "/GetCapabilities"

        try:
            root = self._soap_call(self.endpoint, action, body)
            media_xaddr = self._find_text(root, "XAddr")
            if media_xaddr:
                self._media_endpoint = media_xaddr
                return media_xaddr
        except Exception as e:
            logger.debug("GetCapabilities: %s", e)

        self._media_endpoint = self.endpoint
        return self.endpoint

    def get_profiles(self) -> list:
        """GetProfiles — список медиа-профилей камеры."""
        url = self._get_media_endpoint()
        body = "<trt:GetProfiles/>"
        action = self.NS_TRT + "/GetProfiles"

        try:
            root = self._soap_call(url, action, body)
        except Exception as e:
            logger.warning("GetProfiles: %s", e)
            return []

        profiles = []
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Profiles":
                token = elem.attrib.get("token", "")
                name = ""
                for child in elem:
                    ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if ctag == "Name" and child.text:
                        name = child.text.strip()
                        break
                if token:
                    profiles.append({"token": token, "name": name or token})

        return profiles

    def get_stream_uri(self, profile_token: str):
        """GetStreamUri — RTSP-адрес для профиля."""
        url = self._get_media_endpoint()
        body = (
            "<trt:GetStreamUri>"
            "<trt:StreamSetup>"
            "<trt:Stream>RTP-Unicast</trt:Stream>"
            "<trt:Transport>"
            "<trt:Protocol>RTSP</trt:Protocol>"
            "</trt:Transport>"
            "</trt:StreamSetup>"
            "<trt:ProfileToken>" + profile_token + "</trt:ProfileToken>"
            "</trt:GetStreamUri>"
        )
        action = self.NS_TRT + "/GetStreamUri"

        try:
            root = self._soap_call(url, action, body)
        except Exception as e:
            logger.warning("GetStreamUri: %s", e)
            return None

        return self._find_text(root, "Uri")

    # ------------------------------------------------------------------
    # PTZ
    # ------------------------------------------------------------------
    def _get_ptz_endpoint(self):
        """Возвращает PTZ endpoint из capabilities. None — если PTZ нет."""
        if self._ptz_endpoint_cache:
            return self._ptz_endpoint_cache

        body = (
            "<tds:GetCapabilities>"
            "<tds:Category>PTZ</tds:Category>"
            "</tds:GetCapabilities>"
        )
        action = self.NS_TDS + "/GetCapabilities"

        try:
            root = self._soap_call(self.endpoint, action, body)
        except Exception as e:
            logger.debug("GetCapabilities (PTZ) failed: %s", e)
            return None

        ptz_xaddr = None
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "PTZ":
                for child in elem.iter():
                    ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if ctag == "XAddr" and child.text:
                        ptz_xaddr = child.text.strip()
                        break
                if ptz_xaddr:
                    break

        self._ptz_endpoint_cache = ptz_xaddr
        return ptz_xaddr

    def has_ptz(self) -> bool:
        """Проверяет, поддерживает ли камера PTZ."""
        return self._get_ptz_endpoint() is not None

    def get_ptz_configurations(self) -> list:
        """GetConfigurations — список PTZ-конфигураций."""
        url = self._get_ptz_endpoint()
        if not url:
            return []

        body = "<tptz:GetConfigurations/>"
        action = "http://www.onvif.org/ver20/ptz/wsdl/GetConfigurations"

        try:
            root = self._soap_call(url, action, body)
        except Exception as e:
            logger.warning("GetConfigurations (PTZ): %s", e)
            return []

        result = []
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "PTZConfiguration":
                token = elem.attrib.get("token", "")
                name = ""
                for child in elem:
                    ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if ctag == "Name" and child.text:
                        name = child.text.strip()
                        break
                if token:
                    result.append({"token": token, "name": name or token})

        return result

    def get_ptz_status(self, profile_token: str):
        """GetStatus — текущее положение PTZ."""
        url = self._get_ptz_endpoint()
        if not url:
            return None

        body = (
            "<tptz:GetStatus>"
            "<tptz:ProfileToken>" + profile_token + "</tptz:ProfileToken>"
            "</tptz:GetStatus>"
        )
        action = "http://www.onvif.org/ver20/ptz/wsdl/GetStatus"

        try:
            root = self._soap_call(url, action, body)
        except Exception as e:
            logger.warning("GetStatus (PTZ): %s", e)
            return None

        pos = {"pan": 0.0, "tilt": 0.0, "zoom": 0.0}
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "PanTilt":
                try:
                    pos["pan"] = float(elem.attrib.get("x", 0))
                    pos["tilt"] = float(elem.attrib.get("y", 0))
                except (TypeError, ValueError):
                    pass
            elif tag == "Zoom":
                try:
                    pos["zoom"] = float(elem.attrib.get("x", 0))
                except (TypeError, ValueError):
                    pass

        return pos

    def continuous_move(self, profile_token: str,
                        pan: float = 0.0,
                        tilt: float = 0.0,
                        zoom: float = 0.0) -> bool:
        """ContinuousMove — движение до Stop. Значения от -1.0 до 1.0."""
        url = self._get_ptz_endpoint()
        if not url:
            return False

        body = (
            "<tptz:ContinuousMove>"
            "<tptz:ProfileToken>" + profile_token + "</tptz:ProfileToken>"
            "<tptz:Velocity>"
            '<tt:PanTilt x="' + str(pan) + '" y="' + str(tilt) + '"/>'
            '<tt:Zoom x="' + str(zoom) + '"/>'
            "</tptz:Velocity>"
            "</tptz:ContinuousMove>"
        )
        action = "http://www.onvif.org/ver20/ptz/wsdl/ContinuousMove"

        try:
            self._soap_call(url, action, body)
            return True
        except Exception as e:
            logger.warning("ContinuousMove: %s", e)
            return False

    def stop_move(self, profile_token: str) -> bool:
        """Stop — остановить движение."""
        url = self._get_ptz_endpoint()
        if not url:
            return False

        body = (
            "<tptz:Stop>"
            "<tptz:ProfileToken>" + profile_token + "</tptz:ProfileToken>"
            "<tptz:PanTilt>true</tptz:PanTilt>"
            "<tptz:Zoom>true</tptz:Zoom>"
            "</tptz:Stop>"
        )
        action = "http://www.onvif.org/ver20/ptz/wsdl/Stop"

        try:
            self._soap_call(url, action, body)
            return True
        except Exception as e:
            logger.warning("Stop: %s", e)
            return False

    def relative_move(self, profile_token: str,
                      pan: float = 0.0,
                      tilt: float = 0.0,
                      zoom: float = 0.0) -> bool:
        """RelativeMove — относительное движение. Значения от -1.0 до 1.0."""
        url = self._get_ptz_endpoint()
        if not url:
            return False

        body = (
            "<tptz:RelativeMove>"
            "<tptz:ProfileToken>" + profile_token + "</tptz:ProfileToken>"
            "<tptz:Translation>"
            '<tt:PanTilt x="' + str(pan) + '" y="' + str(tilt) + '"/>'
            '<tt:Zoom x="' + str(zoom) + '"/>'
            "</tptz:Translation>"
            "</tptz:RelativeMove>"
        )
        action = "http://www.onvif.org/ver20/ptz/wsdl/RelativeMove"

        try:
            self._soap_call(url, action, body)
            return True
        except Exception as e:
            logger.warning("RelativeMove: %s", e)
            return False

    def goto_home(self, profile_token: str) -> bool:
        """GotoHomePosition — вернуться в исходное положение."""
        url = self._get_ptz_endpoint()
        if not url:
            return False

        body = (
            "<tptz:GotoHomePosition>"
            "<tptz:ProfileToken>" + profile_token + "</tptz:ProfileToken>"
            "</tptz:GotoHomePosition>"
        )
        action = "http://www.onvif.org/ver20/ptz/wsdl/GotoHomePosition"

        try:
            self._soap_call(url, action, body)
            return True
        except Exception as e:
            logger.warning("GotoHomePosition: %s", e)
            return False

    # ---------- Пресеты ----------
    def get_presets(self, profile_token: str) -> list:
        """GetPresets — список пресетов."""
        url = self._get_ptz_endpoint()
        if not url:
            return []

        body = (
            "<tptz:GetPresets>"
            "<tptz:ProfileToken>" + profile_token + "</tptz:ProfileToken>"
            "</tptz:GetPresets>"
        )
        action = "http://www.onvif.org/ver20/ptz/wsdl/GetPresets"

        try:
            root = self._soap_call(url, action, body)
        except Exception as e:
            logger.warning("GetPresets: %s", e)
            return []

        result = []
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Preset":
                token = elem.attrib.get("token", "")
                name = ""
                for child in elem:
                    ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if ctag == "Name" and child.text:
                        name = child.text.strip()
                        break
                if token:
                    result.append({"token": token, "name": name or token})

        return result

    def set_preset(self, profile_token: str, preset_name: str):
        """SetPreset — создать пресет из текущего положения."""
        url = self._get_ptz_endpoint()
        if not url:
            return None

        body = (
            "<tptz:SetPreset>"
            "<tptz:ProfileToken>" + profile_token + "</tptz:ProfileToken>"
            "<tptz:PresetName>" + preset_name + "</tptz:PresetName>"
            "</tptz:SetPreset>"
        )
        action = "http://www.onvif.org/ver20/ptz/wsdl/SetPreset"

        try:
            root = self._soap_call(url, action, body)
        except Exception as e:
            logger.warning("SetPreset: %s", e)
            return None

        token = self._find_text(root, "PresetToken")
        return {"token": token or "", "name": preset_name}

    def goto_preset(self, profile_token: str, preset_token: str) -> bool:
        """GotoPreset — перейти к пресету."""
        url = self._get_ptz_endpoint()
        if not url:
            return False

        body = (
            "<tptz:GotoPreset>"
            "<tptz:ProfileToken>" + profile_token + "</tptz:ProfileToken>"
            "<tptz:PresetToken>" + preset_token + "</tptz:PresetToken>"
            "</tptz:GotoPreset>"
        )
        action = "http://www.onvif.org/ver20/ptz/wsdl/GotoPreset"

        try:
            self._soap_call(url, action, body)
            return True
        except Exception as e:
            logger.warning("GotoPreset: %s", e)
            return False

    def remove_preset(self, profile_token: str, preset_token: str) -> bool:
        """RemovePreset — удалить пресет."""
        url = self._get_ptz_endpoint()
        if not url:
            return False

        body = (
            "<tptz:RemovePreset>"
            "<tptz:ProfileToken>" + profile_token + "</tptz:ProfileToken>"
            "<tptz:PresetToken>" + preset_token + "</tptz:PresetToken>"
            "</tptz:RemovePreset>"
        )
        action = "http://www.onvif.org/ver20/ptz/wsdl/RemovePreset"

        try:
            self._soap_call(url, action, body)
            return True
        except Exception as e:
            logger.warning("RemovePreset: %s", e)
            return False


# ----------------------------------------------------------------------
# Высокоуровневая обёртка: RTSP
# ----------------------------------------------------------------------
def get_rtsp_urls(endpoint: str, username: str = "", password: str = "") -> dict:
    """
    Подключается к камере, собирает все профили с RTSP-адресами.
    Возвращает:
        {
          "ok": True/False,
          "error": "...",
          "device_info": {...},
          "profiles": [
            {"token": "...", "name": "...", "rtsp_url": "rtsp://..."},
            ...
          ],
        }
    """
    result = {
        "ok": False,
        "error": None,
        "device_info": {},
        "profiles": [],
    }

    try:
        device = OnvifDevice(endpoint, username, password)
        result["device_info"] = device.get_device_info()

        profiles = device.get_profiles()
        if not profiles:
            result["error"] = "Камера не вернула ни одного профиля"
            return result

        for p in profiles:
            uri = device.get_stream_uri(p["token"])
            result["profiles"].append({
                "token": p["token"],
                "name": p["name"],
                "rtsp_url": uri or "",
            })

        result["ok"] = True

    except Exception as e:
        logger.exception("Ошибка ONVIF: %s", e)
        result["error"] = str(e)

    return result


# ----------------------------------------------------------------------
# Высокоуровневая обёртка: PTZ
# ----------------------------------------------------------------------
def get_ptz_info(endpoint: str, username: str = "", password: str = "") -> dict:
    """
    Проверяет поддержку PTZ и возвращает конфигурации + пресеты.
    """
    result = {
        "ok": False,
        "error": None,
        "has_ptz": False,
        "configurations": [],
        "presets": [],
        "profile_token": None,
    }

    try:
        device = OnvifDevice(endpoint, username, password)

        if not device.has_ptz():
            result["ok"] = True
            result["has_ptz"] = False
            return result

        result["has_ptz"] = True
        result["configurations"] = device.get_ptz_configurations()

        profiles = device.get_profiles()
        if profiles:
            result["profile_token"] = profiles[0]["token"]
            result["presets"] = device.get_presets(profiles[0]["token"])

        result["ok"] = True

    except Exception as e:
        logger.exception("get_ptz_info: %s", e)
        result["error"] = str(e)

    return result


def ptz_move(endpoint: str, username: str, password: str,
             profile_token: str,
             pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0,
             duration: float = 0.5) -> bool:
    """
    Движение PTZ.
    Если duration > 0 — ContinuousMove с остановкой через duration секунд.
    Если duration == 0 — RelativeMove одним шагом.
    """
    try:
        device = OnvifDevice(endpoint, username, password)
        if not device.has_ptz():
            return False

        if duration > 0:
            ok = device.continuous_move(profile_token, pan, tilt, zoom)
            if ok:
                time.sleep(duration)
                device.stop_move(profile_token)
            return ok
        else:
            return device.relative_move(profile_token, pan, tilt, zoom)

    except Exception as e:
        logger.exception("ptz_move: %s", e)
        return False


def ptz_stop(endpoint: str, username: str, password: str,
             profile_token: str) -> bool:
    """Остановить движение."""
    try:
        device = OnvifDevice(endpoint, username, password)
        return device.stop_move(profile_token)
    except Exception as e:
        logger.exception("ptz_stop: %s", e)
        return False


def ptz_home(endpoint: str, username: str, password: str,
             profile_token: str) -> bool:
    """Вернуть в исходное положение."""
    try:
        device = OnvifDevice(endpoint, username, password)
        return device.goto_home(profile_token)
    except Exception as e:
        logger.exception("ptz_home: %s", e)
        return False


def ptz_goto_preset(endpoint: str, username: str, password: str,
                    profile_token: str, preset_token: str) -> bool:
    """Перейти к пресету."""
    try:
        device = OnvifDevice(endpoint, username, password)
        return device.goto_preset(profile_token, preset_token)
    except Exception as e:
        logger.exception("ptz_goto_preset: %s", e)
        return False


def ptz_set_preset(endpoint: str, username: str, password: str,
                   profile_token: str, name: str):
    """Создать пресет из текущего положения."""
    try:
        device = OnvifDevice(endpoint, username, password)
        return device.set_preset(profile_token, name)
    except Exception as e:
        logger.exception("ptz_set_preset: %s", e)
        return None


def ptz_remove_preset(endpoint: str, username: str, password: str,
                      profile_token: str, preset_token: str) -> bool:
    """Удалить пресет."""
    try:
        device = OnvifDevice(endpoint, username, password)
        return device.remove_preset(profile_token, preset_token)
    except Exception as e:
        logger.exception("ptz_remove_preset: %s", e)
        return False