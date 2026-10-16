"""
ONVIF-прокси на стороне VHS-Streamer.

Позволяет Интеллект-икс (и другим NVR) подключаться к нашему серверу
как к обычному ONVIF-устройству. Мы отдаём:
    * Device Service — информацию об устройстве;
    * Media Service — список профилей и RTSP-адреса дублирующих потоков;
    * Events Service — события от камер через PullPoint.

Все ответы — SOAP over HTTP. Namespace'ы соответствуют стандарту ONVIF.
"""
import base64
import hashlib
import hmac
import logging
import re
import threading
import time as _time
import uuid
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from flask import Blueprint, request, Response, current_app

logger = logging.getLogger(__name__)

onvif_bp = Blueprint("onvif_proxy", __name__)


# ======================================================================
# Константы — namespace'ы ONVIF
# ======================================================================
NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_TDS = "http://www.onvif.org/ver10/device/wsdl"
NS_TRT = "http://www.onvif.org/ver10/media/wsdl"
NS_TEV = "http://www.onvif.org/ver10/events/wsdl"
NS_TT = "http://www.onvif.org/ver10/schema"


# ======================================================================
# Парсинг SOAP
# ======================================================================
def _parse_soap(body_bytes: bytes):
    try:
        text = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        return None, None
    return text, None


def _get_action_from_headers() -> str:
    ct = request.headers.get("Content-Type", "")
    m = re.search(r'action="([^"]+)"', ct)
    if m:
        return m.group(1)
    sa = request.headers.get("SOAPAction", "").strip('"')
    return sa or ""


def _detect_operation(body_text: str) -> str:
    operations = (
        "GetDeviceInformation",
        "GetCapabilities",
        "GetServices",
        "GetSystemDateAndTime",
        "GetWsdlUrl",
        "GetNetworkInterfaces",
        "GetServiceCapabilities",
        "GetProfiles",
        "GetStreamUri",
        "GetVideoSources",
        "GetVideoEncoderConfigurations",
        "GetEventProperties",
        "CreatePullPointSubscription",
        "PullMessages",
        "Renew",
        "Unsubscribe",
    )
    for op in operations:
        if f":{op}" in body_text or f"<{op}" in body_text:
            return op
    return ""


# ======================================================================
# Общие хелперы
# ======================================================================
def _soap_envelope(body_inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope'
        ' xmlns:s="' + NS_SOAP + '"'
        ' xmlns:tds="' + NS_TDS + '"'
        ' xmlns:trt="' + NS_TRT + '"'
        ' xmlns:tev="' + NS_TEV + '"'
        ' xmlns:tt="' + NS_TT + '"'
        ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
        ' xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"'
        ' xmlns:wsa5="http://www.w3.org/2005/08/addressing"'
        '>'
        '<s:Body>' + body_inner + '</s:Body>'
        '</s:Envelope>'
    )


def _soap_response(body_inner: str) -> Response:
    xml = _soap_envelope(body_inner)
    return Response(xml, mimetype='application/soap+xml; charset=utf-8')


def _soap_fault(reason: str, code: str = "ter:ActionNotSupported") -> Response:
    body = (
        '<s:Fault>'
        '<s:Code><s:Value>s:Receiver</s:Value>'
        f'<s:Subcode><s:Value>{escape(code)}</s:Value></s:Subcode>'
        '</s:Code>'
        f'<s:Reason><s:Text xml:lang="en">{escape(reason)}</s:Text></s:Reason>'
        '</s:Fault>'
    )
    return _soap_response(body)

# ======================================================================
# WS-Security авторизация
# ======================================================================
def _unauthorized_response() -> Response:
    """401 с SOAP-Fault (NotAuthorized)."""
    body = (
        '<s:Fault>'
        '<s:Code><s:Value>s:Sender</s:Value>'
        '<s:Subcode><s:Value>ter:NotAuthorized</s:Value></s:Subcode>'
        '</s:Code>'
        '<s:Reason><s:Text xml:lang="en">Sender not Authorized</s:Text></s:Reason>'
        '</s:Fault>'
    )
    xml = _soap_envelope(body)
    return Response(
        xml,
        status=401,
        mimetype='application/soap+xml; charset=utf-8',
    )


def _extract_ws_credentials() -> dict | None:
    """
    Извлекает WS-UsernameToken из SOAP-заголовка.
    Возвращает dict или None.
    """
    text = request.get_data(as_text=True) or ""

    # Username
    m_user = re.search(
        r'<[^>]*Username[^>]*>([^<]+)</[^>]*Username>', text
    )
    if not m_user:
        return None
    username = m_user.group(1).strip()

    # Password с типом
    m_pass = re.search(
        r'<[^>]*Password[^>]*Type="([^"]*)"[^>]*>([^<]+)</[^>]*Password>',
        text,
    )
    if m_pass:
        pass_type = m_pass.group(1)
        pass_value = m_pass.group(2).strip()
    else:
        # Password без Type (по умолчанию — PasswordText)
        m_pass2 = re.search(
            r'<[^>]*Password[^>]*>([^<]+)</[^>]*Password>', text
        )
        if not m_pass2:
            return None
        pass_type = "PasswordText"
        pass_value = m_pass2.group(1).strip()

    # Nonce и Created — нужны для digest
    m_nonce = re.search(
        r'<[^>]*Nonce[^>]*>([^<]+)</[^>]*Nonce>', text
    )
    m_created = re.search(
        r'<[^>]*Created[^>]*>([^<]+)</[^>]*Created>', text
    )

    return {
        "username": username,
        "password": pass_value,
        "password_type": pass_type,
        "nonce": m_nonce.group(1).strip() if m_nonce else None,
        "created": m_created.group(1).strip() if m_created else None,
    }


def _get_allowed_users() -> list:
    """
    Возвращает объединённый список пользователей:
        * security.users (главные)
        * onvif_proxy.auth.users (дополнительные)
    """
    cfg = current_app.config.get("VHS_CONFIG", {}) or {}
    users = []

    # Основные пользователи
    sec = cfg.get("security", {}) or {}
    for u in sec.get("users", []):
        if u.get("username"):
            users.append({
                "username": u["username"],
                "password": u.get("password", ""),
            })

    # Дополнительные из onvif_proxy.auth
    proxy = cfg.get("onvif_proxy", {}) or {}
    auth = proxy.get("auth", {}) or {}
    for u in auth.get("users", []):
        if u.get("username"):
            # Не дублируем, если такой логин уже есть
            if not any(x["username"] == u["username"] for x in users):
                users.append({
                    "username": u["username"],
                    "password": u.get("password", ""),
                })

    return users


def _verify_credentials(creds: dict) -> bool:
    """Проверяет WS-UsernameToken."""
    if not creds:
        return False

    username = creds["username"]
    password = creds["password"]
    pass_type = creds.get("password_type", "")

    allowed = _get_allowed_users()
    for u in allowed:
        if u["username"] != username:
            continue

        # PasswordText — сравнение в чистом виде
        if "PasswordText" in pass_type:
            return hmac.compare_digest(password, u["password"])

        # PasswordDigest — base64(sha1(nonce + created + password))
        if "PasswordDigest" in pass_type:
            nonce_b64 = creds.get("nonce")
            created = creds.get("created")
            if not nonce_b64 or not created:
                return False
            try:
                nonce_bytes = base64.b64decode(nonce_b64)
            except Exception:
                return False
            digest_input = nonce_bytes + created.encode("utf-8") + u["password"].encode("utf-8")
            expected = base64.b64encode(
                hashlib.sha1(digest_input).digest()
            ).decode("ascii")
            if hmac.compare_digest(password, expected):
                return True
            return False

        # Неизвестный тип
        return False

    return False


def _is_auth_enabled() -> bool:
    """Включена ли авторизация прокси."""
    proxy = _get_proxy_cfg()
    auth = proxy.get("auth", {}) or {}
    return bool(auth.get("enabled", False))

# ======================================================================
# Общие данные о камерах и потоках
# ======================================================================
def _get_state():
    return {
        "cameras": current_app.config.get("CAMERAS", {}),
        "config": current_app.config.get("VHS_CONFIG", {}),
        "loopback_urls": current_app.config.get("LOOPBACK_URLS", {}),
    }


def _get_proxy_cfg() -> dict:
    cfg = current_app.config.get("VHS_CONFIG", {})
    return cfg.get("onvif_proxy", {}) or {}


def _get_host_for_clients() -> str:
    proxy_cfg = _get_proxy_cfg()
    public_host = proxy_cfg.get("public_host")
    if public_host:
        return public_host
    return request.host.split(":")[0]


def _build_rtsp_url(stream_path: str) -> str:
    cfg = current_app.config.get("VHS_CONFIG", {})
    rtsp_port = cfg.get("mediamtx", {}).get("rtsp_port", 8554)
    host = _get_host_for_clients()
    return f"rtsp://{host}:{rtsp_port}{stream_path}"


def _list_streams():
    st = _get_state()
    cfg = st["config"]
    streams = cfg.get("streams", []) or []
    cameras = st["cameras"]

    result = []
    for s in streams:
        if not s.get("enabled", True):
            continue
        cam = cameras.get(s.get("camera_id"))
        if not cam:
            continue
        rtsp_url = _build_rtsp_url(s["path"])
        result.append((s, cam, rtsp_url))
    return result


# ======================================================================
# Device Service
# ======================================================================
def _handle_get_device_information() -> Response:
    proxy_cfg = _get_proxy_cfg()
    manufacturer = proxy_cfg.get("manufacturer", "VHS-Streamer")
    model = proxy_cfg.get("model", "VHS-Streamer ONVIF Proxy")
    firmware = proxy_cfg.get("firmware", "1.0.0")
    serial = proxy_cfg.get("serial", "VHS-PROXY-0001")
    hardware = proxy_cfg.get("hardware", "VHS-Proxy")

    body = (
        '<tds:GetDeviceInformationResponse>'
        f'<tds:Manufacturer>{escape(manufacturer)}</tds:Manufacturer>'
        f'<tds:Model>{escape(model)}</tds:Model>'
        f'<tds:FirmwareVersion>{escape(firmware)}</tds:FirmwareVersion>'
        f'<tds:SerialNumber>{escape(serial)}</tds:SerialNumber>'
        f'<tds:HardwareId>{escape(hardware)}</tds:HardwareId>'
        '</tds:GetDeviceInformationResponse>'
    )
    return _soap_response(body)


def _handle_get_capabilities() -> Response:
    host = request.host
    base_url = f"http://{host}/onvif"

    body = (
        '<tds:GetCapabilitiesResponse>'
        '<tds:Capabilities>'
        # Device
        '<tt:Device>'
        '<tt:XAddr>' + escape(f"{base_url}/device_service") + '</tt:XAddr>'
        '<tt:Network><tt:IPFilter>false</tt:IPFilter>'
        '<tt:ZeroConfiguration>false</tt:ZeroConfiguration>'
        '<tt:IPVersion6>false</tt:IPVersion6>'
        '<tt:DynDNS>false</tt:DynDNS></tt:Network>'
        '<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve>'
        '<tt:DiscoveryBye>false</tt:DiscoveryBye>'
        '<tt:RemoteDiscovery>false</tt:RemoteDiscovery>'
        '<tt:SystemBackup>false</tt:SystemBackup>'
        '<tt:SystemLogging>false</tt:SystemLogging>'
        '<tt:FirmwareUpgrade>false</tt:FirmwareUpgrade></tt:System>'
        '<tt:IO><tt:InputConnectors>0</tt:InputConnectors>'
        '<tt:RelayOutputs>0</tt:RelayOutputs></tt:IO>'
        '<tt:Security><tt:TLS1.1>false</tt:TLS1.1>'
        '<tt:TLS1.2>false</tt:TLS1.2>'
        '<tt:OnboardKeyGeneration>false</tt:OnboardKeyGeneration>'
        '<tt:AccessPolicyConfig>false</tt:AccessPolicyConfig>'
        '<tt:X.509Token>false</tt:X.509Token>'
        '<tt:SAMLToken>false</tt:SAMLToken>'
        '<tt:KerberosToken>false</tt:KerberosToken>'
        '<tt:RELToken>false</tt:RELToken></tt:Security>'
        '</tt:Device>'
        # Media
        '<tt:Media>'
        '<tt:XAddr>' + escape(f"{base_url}/media_service") + '</tt:XAddr>'
        '<tt:StreamingCapabilities>'
        '<tt:RTPMulticast>false</tt:RTPMulticast>'
        '<tt:RTP_TCP>true</tt:RTP_TCP>'
        '<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>'
        '</tt:StreamingCapabilities>'
        '</tt:Media>'
        # Events
        '<tt:Events>'
        '<tt:XAddr>' + escape(f"{base_url}/events_service") + '</tt:XAddr>'
        '<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>'
        '<tt:WSPullPointSupport>true</tt:WSPullPointSupport>'
        '<tt:WSPausableSubscriptionManagerInterfaceSupport>false</tt:WSPausableSubscriptionManagerInterfaceSupport>'
        '</tt:Events>'
        '</tds:Capabilities>'
        '</tds:GetCapabilitiesResponse>'
    )
    return _soap_response(body)


def _handle_get_services() -> Response:
    host = request.host
    base_url = f"http://{host}/onvif"

    body = (
        '<tds:GetServicesResponse>'
        '<tds:Service>'
        '<tds:Namespace>' + NS_TDS + '</tds:Namespace>'
        '<tds:XAddr>' + escape(f"{base_url}/device_service") + '</tds:XAddr>'
        '<tds:Version><tt:Major>2</tt:Major><tt:Minor>6</tt:Minor></tds:Version>'
        '</tds:Service>'
        '<tds:Service>'
        '<tds:Namespace>' + NS_TRT + '</tds:Namespace>'
        '<tds:XAddr>' + escape(f"{base_url}/media_service") + '</tds:XAddr>'
        '<tds:Version><tt:Major>2</tt:Major><tt:Minor>6</tt:Minor></tds:Version>'
        '</tds:Service>'
        '<tds:Service>'
        '<tds:Namespace>' + NS_TEV + '</tds:Namespace>'
        '<tds:XAddr>' + escape(f"{base_url}/events_service") + '</tds:XAddr>'
        '<tds:Version><tt:Major>2</tt:Major><tt:Minor>6</tt:Minor></tds:Version>'
        '</tds:Service>'
        '</tds:GetServicesResponse>'
    )
    return _soap_response(body)


def _handle_get_system_date_and_time() -> Response:
    now = datetime.now(timezone.utc)
    body = (
        '<tds:GetSystemDateAndTimeResponse>'
        '<tds:SystemDateAndTime>'
        '<tt:DateTimeType>Manual</tt:DateTimeType>'
        '<tt:DaylightSavings>false</tt:DaylightSavings>'
        '<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>'
        '<tt:UTCDateTime>'
        f'<tt:Time><tt:Hour>{now.hour}</tt:Hour>'
        f'<tt:Minute>{now.minute}</tt:Minute>'
        f'<tt:Second>{now.second}</tt:Second></tt:Time>'
        f'<tt:Date><tt:Year>{now.year}</tt:Year>'
        f'<tt:Month>{now.month}</tt:Month>'
        f'<tt:Day>{now.day}</tt:Day></tt:Date>'
        '</tt:UTCDateTime>'
        '</tds:SystemDateAndTime>'
        '</tds:GetSystemDateAndTimeResponse>'
    )
    return _soap_response(body)


def _handle_get_wsdl_url() -> Response:
    body = '<tds:GetWsdlUrlResponse/>'
    return _soap_response(body)


def _handle_get_service_capabilities() -> Response:
    body = (
        '<tds:GetServiceCapabilitiesResponse>'
        '<tds:Capabilities>'
        '<tds:Network><tt:IPFilter>false</tt:IPFilter>'
        '<tt:ZeroConfiguration>false</tt:ZeroConfiguration>'
        '<tt:IPVersion6>false</tt:IPVersion6>'
        '<tt:DynDNS>false</tt:DynDNS></tds:Network>'
        '<tds:Security><tt:TLS1.1>false</tt:TLS1.1>'
        '<tt:TLS1.2>false</tt:TLS1.2>'
        '<tt:OnboardKeyGeneration>false</tt:OnboardKeyGeneration>'
        '<tt:AccessPolicyConfig>false</tt:AccessPolicyConfig>'
        '<tt:X.509Token>false</tt:X.509Token>'
        '<tt:SAMLToken>false</tt:SAMLToken>'
        '<tt:KerberosToken>false</tt:KerberosToken>'
        '<tt:RELToken>false</tt:RELToken></tds:Security>'
        '</tds:Capabilities>'
        '</tds:GetServiceCapabilitiesResponse>'
    )
    return _soap_response(body)


def _handle_get_network_interfaces() -> Response:
    host = request.host.split(":")[0]
    body = (
        '<tds:GetNetworkInterfacesResponse>'
        '<tds:NetworkInterfaces token="eth0">'
        '<tt:Enabled>true</tt:Enabled>'
        '<tt:Info><tt:Name>eth0</tt:Name>'
        '<tt:HwAddress>00:00:00:00:00:00</tt:HwAddress>'
        '<tt:MTU>1500</tt:MTU></tt:Info>'
        '<tt:Link><tt:AdminSettings>'
        '<tt:AutoNegotiation>true</tt:AutoNegotiation>'
        '<tt:Speed>1000</tt:Speed>'
        '<tt:Duplex>Full</tt:Duplex></tt:AdminSettings>'
        '<tt:OperSettings>'
        '<tt:AutoNegotiation>true</tt:AutoNegotiation>'
        '<tt:Speed>1000</tt:Speed>'
        '<tt:Duplex>Full</tt:Duplex></tt:OperSettings>'
        '<tt:InterfaceType>6</tt:InterfaceType></tt:Link>'
        '<tt:IPv4><tt:Enabled>true</tt:Enabled>'
        '<tt:Config><tt:Manual>'
        f'<tt:Address>{escape(host)}</tt:Address>'
        '<tt:PrefixLength>24</tt:PrefixLength>'
        '</tt:Manual><tt:DHCP>false</tt:DHCP></tt:Config>'
        '</tt:IPv4>'
        '<tt:IPv6><tt:Enabled>false</tt:Enabled>'
        '<tt:Config/></tt:IPv6>'
        '</tds:NetworkInterfaces>'
        '</tds:GetNetworkInterfacesResponse>'
    )
    return _soap_response(body)


# ======================================================================
# Media Service
# ======================================================================
def _profile_token(stream_id: str) -> str:
    return "profile_" + re.sub(r"[^A-Za-z0-9_-]", "_", stream_id)


def _handle_get_profiles() -> Response:
    streams = _list_streams()

    parts = []
    for stream_cfg, cam_cfg, rtsp_url in streams:
        token = _profile_token(stream_cfg["id"])
        name = f"{cam_cfg.name} — {stream_cfg['id']}"

        vsc_token = f"vsc_{stream_cfg['id']}"
        vec_cfg_token = f"veccfg_{stream_cfg['id']}"

        width = stream_cfg.get("width") or cam_cfg.width or 1920
        height = stream_cfg.get("height") or cam_cfg.height or 1080

        profile_xml = (
            f'<trt:Profiles token="{escape(token)}" fixed="true">'
            f'<tt:Name>{escape(name)}</tt:Name>'
            f'<tt:VideoSourceConfiguration token="{vsc_token}">'
            f'<tt:Name>VideoSource_{stream_cfg["id"]}</tt:Name>'
            '<tt:UseCount>1</tt:UseCount>'
            '<tt:SourceToken>vs_1</tt:SourceToken>'
            '<tt:Bounds x="0" y="0" '
            f'width="{width}" height="{height}"/>'
            '</tt:VideoSourceConfiguration>'
            f'<tt:VideoEncoderConfiguration token="{vec_cfg_token}">'
            f'<tt:Name>VideoEncoder_{stream_cfg["id"]}</tt:Name>'
            '<tt:UseCount>1</tt:UseCount>'
            '<tt:Encoding>H264</tt:Encoding>'
            f'<tt:Resolution><tt:Width>{width}</tt:Width>'
            f'<tt:Height>{height}</tt:Height></tt:Resolution>'
            '<tt:Quality>5</tt:Quality>'
            '<tt:RateControl>'
            f'<tt:FrameRateLimit>{cam_cfg.fps}</tt:FrameRateLimit>'
            '<tt:EncodingInterval>1</tt:EncodingInterval>'
            '<tt:BitrateLimit>4096</tt:BitrateLimit>'
            '</tt:RateControl>'
            '<tt:H264>'
            '<tt:GovLength>50</tt:GovLength>'
            '<tt:H264Profile>Main</tt:H264Profile>'
            '</tt:H264>'
            '<tt:Multicast><tt:Address>'
            '<tt:Type>IPv4</tt:Type>'
            '<tt:IPv4Address>0.0.0.0</tt:IPv4Address>'
            '<tt:Port>0</tt:Port>'
            '<tt:TTL>1</tt:TTL>'
            '<tt:AutoStart>false</tt:AutoStart>'
            '</tt:Address></tt:Multicast>'
            '<tt:SessionTimeout>PT60S</tt:SessionTimeout>'
            '</tt:VideoEncoderConfiguration>'
            '</trt:Profiles>'
        )
        parts.append(profile_xml)

    body = (
        '<trt:GetProfilesResponse>'
        + "".join(parts)
        + '</trt:GetProfilesResponse>'
    )
    return _soap_response(body)


def _handle_get_stream_uri() -> Response:
    text = request.get_data(as_text=True)
    m = re.search(r"<[^>]*ProfileToken[^>]*>([^<]+)</[^>]*ProfileToken>", text)
    if not m:
        return _soap_fault("ProfileToken отсутствует")
    token = m.group(1).strip()

    stream_id = token
    if stream_id.startswith("profile_"):
        stream_id = stream_id[len("profile_"):]

    streams = _list_streams()
    target = None
    for stream_cfg, cam_cfg, rtsp_url in streams:
        if stream_cfg["id"] == stream_id:
            target = (stream_cfg, cam_cfg, rtsp_url)
            break

    if not target:
        return _soap_fault(f"Профиль не найден: {token}")

    _, _, rtsp_url = target

    body = (
        '<trt:GetStreamUriResponse>'
        '<trt:MediaUri>'
        f'<tt:Uri>{escape(rtsp_url)}</tt:Uri>'
        '<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>'
        '<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>'
        '<tt:Timeout>PT0S</tt:Timeout>'
        '</trt:MediaUri>'
        '</trt:GetStreamUriResponse>'
    )
    return _soap_response(body)


def _handle_get_video_sources() -> Response:
    streams = _list_streams()
    parts = []
    seen = set()
    for stream_cfg, cam_cfg, _ in streams:
        if cam_cfg.id in seen:
            continue
        seen.add(cam_cfg.id)
        parts.append(
            '<trt:VideoSources token="vs_1">'
            f'<tt:Framerate>{cam_cfg.fps}</tt:Framerate>'
            f'<tt:Resolution><tt:Width>{cam_cfg.width}</tt:Width>'
            f'<tt:Height>{cam_cfg.height}</tt:Height></tt:Resolution>'
            '</trt:VideoSources>'
        )
    body = '<trt:GetVideoSourcesResponse>' + "".join(parts) + '</trt:GetVideoSourcesResponse>'
    return _soap_response(body)


def _handle_get_video_encoder_configurations() -> Response:
    streams = _list_streams()
    parts = []
    for stream_cfg, cam_cfg, _ in streams:
        width = stream_cfg.get("width") or cam_cfg.width or 1920
        height = stream_cfg.get("height") or cam_cfg.height or 1080
        vec_cfg_token = f"veccfg_{stream_cfg['id']}"
        parts.append(
            f'<trt:Configurations token="{vec_cfg_token}">'
            f'<tt:Name>VideoEncoder_{stream_cfg["id"]}</tt:Name>'
            '<tt:UseCount>1</tt:UseCount>'
            '<tt:Encoding>H264</tt:Encoding>'
            f'<tt:Resolution><tt:Width>{width}</tt:Width>'
            f'<tt:Height>{height}</tt:Height></tt:Resolution>'
            '<tt:Quality>5</tt:Quality>'
            '<tt:RateControl>'
            f'<tt:FrameRateLimit>{cam_cfg.fps}</tt:FrameRateLimit>'
            '<tt:EncodingInterval>1</tt:EncodingInterval>'
            '<tt:BitrateLimit>4096</tt:BitrateLimit>'
            '</tt:RateControl>'
            '<tt:H264><tt:GovLength>50</tt:GovLength>'
            '<tt:H264Profile>Main</tt:H264Profile></tt:H264>'
            '<tt:SessionTimeout>PT60S</tt:SessionTimeout>'
            '</trt:Configurations>'
        )
    body = (
        '<trt:GetVideoEncoderConfigurationsResponse>'
        + "".join(parts)
        + '</trt:GetVideoEncoderConfigurationsResponse>'
    )
    return _soap_response(body)


# ======================================================================
# Events Service
# ======================================================================
# Активные подписки: {sub_id: {created, last_pull, last_event_ts, ttl}}
_onvif_subscriptions = {}
_onvif_subs_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _handle_get_event_properties() -> Response:
    topics = [
        "tns1:VideoSource/MotionAlarm",
        "tns1:RuleEngine/CellMotionDetector/Motion",
        "tns1:RuleEngine/FieldDetector/ObjectsInside",
        "tns1:RuleEngine/LineDetector/Crossed",
        "tns1:RuleEngine/Loitering",
        "tns1:Device/Trigger/DigitalInput",
        "tns1:Device/HardwareFailure/StorageFailure",
        "tns1:Device/TamperDetector/Detected",
        "tns1:VideoSource/GlobalSceneChange/ImagingSettingsChange",
    ]

    body = (
        '<tev:GetEventPropertiesResponse>'
        '<tev:TopicNamespaceLocation>'
        'http://www.onvif.org/ver10/topics/topicns.xml'
        '</tev:TopicNamespaceLocation>'
        '<tt:FixedTopicSet>true</tt:FixedTopicSet>'
        '<tev:TopicSet>'
        + "".join(f'<tt:Topic>{escape(t)}</tt:Topic>' for t in topics)
        + '</tev:TopicSet>'
        '<tev:TopicExpressionDialect>'
        'http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet'
        '</tev:TopicExpressionDialect>'
        '<tev:TopicExpressionDialect>'
        'http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete'
        '</tev:TopicExpressionDialect>'
        '<tev:MessageContentFilterDialect>'
        'http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter'
        '</tev:MessageContentFilterDialect>'
        '<tev:ProducerPropertiesFilterDialect>'
        'http://www.onvif.org/ver10/tev/producerPropertiesFilter/ItemFilter'
        '</tev:ProducerPropertiesFilterDialect>'
        '<tev:MessageContentSchemaLocation>'
        'http://www.onvif.org/onvif/ver10/schema/onvif.xsd'
        '</tev:MessageContentSchemaLocation>'
        '</tev:GetEventPropertiesResponse>'
    )
    return _soap_response(body)


def _handle_create_pullpoint() -> Response:
    sub_id = uuid.uuid4().hex
    start_ts = _time.time()

    with _onvif_subs_lock:
        _onvif_subscriptions[sub_id] = {
            "created": start_ts,
            "last_pull": start_ts,
            "last_event_ts": start_ts,
            "ttl": 600,
        }

    logger.info("ONVIF Events: создана подписка %s", sub_id)

    host = request.host
    sub_url = f"http://{host}/onvif/events_service/{sub_id}"

    body = (
        '<tev:CreatePullPointSubscriptionResponse>'
        '<tev:SubscriptionReference>'
        '<wsa5:Address>'
        + escape(sub_url)
        + '</wsa5:Address>'
        '</tev:SubscriptionReference>'
        '<tt:CurrentTime>' + _now_iso() + '</tt:CurrentTime>'
        '<tt:TerminationTime>'
        + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        + '</tt:TerminationTime>'
        '</tev:CreatePullPointSubscriptionResponse>'
    )
    return _soap_response(body)


def _handle_pull_messages() -> Response:
    sub_id = request.path.rstrip("/").split("/")[-1]

    with _onvif_subs_lock:
        sub = _onvif_subscriptions.get(sub_id)
        if not sub:
            return _soap_fault(f"Подписка не найдена: {sub_id}")
        sub["last_pull"] = _time.time()
        since_ts = sub["last_event_ts"]

    em = current_app.config.get("EVENTS_MANAGER")
    events = []
    if em:
        try:
            events = em.db.query(since_ts=since_ts, limit=100)
        except Exception as e:
            logger.warning("ONVIF Pull: ошибка чтения событий: %s", e)

    if events:
        max_ts = max(ev["ts"] for ev in events)
        with _onvif_subs_lock:
            if sub_id in _onvif_subscriptions:
                _onvif_subscriptions[sub_id]["last_event_ts"] = max_ts

    # Сортируем по возрастанию времени (ONVIF-клиенты ожидают этого)
    events_sorted = sorted(events, key=lambda e: e.get("ts", 0))

    parts = []
    for ev in events_sorted:
        msg = _event_to_notification(ev)
        if msg:
            parts.append(msg)

    body = (
        '<tev:PullMessagesResponse>'
        '<tt:CurrentTime>' + _now_iso() + '</tt:CurrentTime>'
        '<tt:TerminationTime>'
        + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        + '</tt:TerminationTime>'
        + "".join(parts)
        + '</tev:PullMessagesResponse>'
    )
    return _soap_response(body)


def _event_to_notification(ev: dict) -> str:
    topic = ev.get("topic") or ""
    is_active = bool(ev.get("is_active"))
    ts = ev.get("ts", _time.time())
    utc_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    state_value = "true" if is_active else "false"
    camera_id = ev.get("camera_id", "")

    notification = (
        '<wsnt:NotificationMessage>'
        '<wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">'
        + escape(topic)
        + '</wsnt:Topic>'
        '<wsnt:Message>'
        '<tt:Message UtcTime="' + utc_str + '" PropertyOperation="Changed">'
        '<tt:Source>'
        '<tt:SimpleItem Name="VideoSourceConfigurationToken" '
        'Value="' + escape(camera_id) + '"/>'
        '</tt:Source>'
        '<tt:Data>'
        '<tt:SimpleItem Name="State" Value="' + state_value + '"/>'
        '<tt:SimpleItem Name="IsMotion" Value="' + state_value + '"/>'
        '<tt:SimpleItem Name="Active" Value="' + state_value + '"/>'
        '</tt:Data>'
        '</tt:Message>'
        '</wsnt:Message>'
        '</wsnt:NotificationMessage>'
    )
    return notification


def _handle_renew() -> Response:
    sub_id = request.path.rstrip("/").split("/")[-1]

    with _onvif_subs_lock:
        sub = _onvif_subscriptions.get(sub_id)
        if not sub:
            return _soap_fault(f"Подписка не найдена: {sub_id}")
        sub["ttl"] = 600
        sub["last_pull"] = _time.time()

    body = (
        '<wsnt:RenewResponse>'
        '<wsnt:CurrentTime>' + _now_iso() + '</wsnt:CurrentTime>'
        '<wsnt:TerminationTime>'
        + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        + '</wsnt:TerminationTime>'
        '</wsnt:RenewResponse>'
    )
    return _soap_response(body)


def _handle_unsubscribe() -> Response:
    sub_id = request.path.rstrip("/").split("/")[-1]

    with _onvif_subs_lock:
        if sub_id in _onvif_subscriptions:
            del _onvif_subscriptions[sub_id]
            logger.info("ONVIF Events: подписка %s закрыта", sub_id)

    body = '<wsnt:UnsubscribeResponse/>'
    return _soap_response(body)


def _cleanup_stale_subscriptions():
    now = _time.time()
    with _onvif_subs_lock:
        stale = [
            sid for sid, s in _onvif_subscriptions.items()
            if now - s["last_pull"] > s["ttl"]
        ]
        for sid in stale:
            del _onvif_subscriptions[sid]
            logger.info("ONVIF Events: подписка %s устарела, удаляю", sid)


def _start_subscription_cleaner():
    def _loop():
        while True:
            _time.sleep(60)
            try:
                _cleanup_stale_subscriptions()
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True, name="onvif-subs-cleanup")
    t.start()


_start_subscription_cleaner()



# ======================================================================
# Роутинг
# ======================================================================
def _handle_operation(operation: str) -> Response:
    handlers = {
        # Device Service
        "GetDeviceInformation": _handle_get_device_information,
        "GetCapabilities": _handle_get_capabilities,
        "GetServices": _handle_get_services,
        "GetServiceCapabilities": _handle_get_service_capabilities,
        "GetSystemDateAndTime": _handle_get_system_date_and_time,
        "GetWsdlUrl": _handle_get_wsdl_url,
        "GetNetworkInterfaces": _handle_get_network_interfaces,
        # Media Service
        "GetProfiles": _handle_get_profiles,
        "GetStreamUri": _handle_get_stream_uri,
        "GetVideoSources": _handle_get_video_sources,
        "GetVideoEncoderConfigurations": _handle_get_video_encoder_configurations,
        # Events Service
        "GetEventProperties": _handle_get_event_properties,
        "CreatePullPointSubscription": _handle_create_pullpoint,
        "PullMessages": _handle_pull_messages,
        "Renew": _handle_renew,
        "Unsubscribe": _handle_unsubscribe,
    }
    handler = handlers.get(operation)
    if not handler:
        return _soap_fault(f"Операция не поддерживается: {operation}")
    return handler()


# Методы, доступные без авторизации (требование ONVIF-стандарта)
PUBLIC_OPERATIONS = ("GetSystemDateAndTime", "GetWsdlUrl")


def _dispatch():
    body_bytes = request.get_data()
    text = _parse_soap(body_bytes)[0] or ""

    operation = _detect_operation(text)
    if not operation:
        action = _get_action_from_headers()
        if action:
            operation = action.rsplit("/", 1)[-1]

    logger.info("ONVIF proxy: %s %s → %s",
                request.method, request.path, operation or "?")
    logger.debug("ONVIF body: %s", text[:2000])

    if not operation:
        return _soap_fault("Не удалось определить операцию")

    # Проверка авторизации
    if _is_auth_enabled() and operation not in PUBLIC_OPERATIONS:
        creds = _extract_ws_credentials()
        if not creds:
            logger.info("ONVIF proxy: %s — нет WS-Security заголовка",
                        operation)
            return _unauthorized_response()
        if not _verify_credentials(creds):
            logger.warning(
                "ONVIF proxy: %s — неверные креды (user=%r, type=%r)",
                operation,
                creds.get("username"),
                creds.get("password_type"),
            )
            return _unauthorized_response()
        logger.debug("ONVIF proxy: %s — авторизован как %s",
                     operation, creds.get("username"))

    return _handle_operation(operation)


# ----------------------------------------------------------------------
# HTTP endpoints
# ----------------------------------------------------------------------
@onvif_bp.route("/onvif/device_service", methods=["POST", "GET"])
def device_service():
    if request.method == "GET":
        return Response("ONVIF Device Service — используйте POST",
                        mimetype="text/plain")
    return _dispatch()


@onvif_bp.route("/onvif/media_service", methods=["POST", "GET"])
def media_service():
    if request.method == "GET":
        return Response("ONVIF Media Service — используйте POST",
                        mimetype="text/plain")
    return _dispatch()


@onvif_bp.route("/onvif/events_service", methods=["POST", "GET"])
def events_service():
    if request.method == "GET":
        return Response("ONVIF Events Service — используйте POST",
                        mimetype="text/plain")
    return _dispatch()


@onvif_bp.route("/onvif/events_service/<sub_id>", methods=["POST", "GET"])
def events_subscription(sub_id: str):
    if request.method == "GET":
        return Response(f"Subscription {sub_id}", mimetype="text/plain")
    return _dispatch()


@onvif_bp.route("/onvif", methods=["GET"])
def onvif_root():
    return Response(
        "VHS-Streamer ONVIF Proxy. Эндпоинты: "
        "/onvif/device_service, /onvif/media_service, /onvif/events_service",
        mimetype="text/plain",
    )

