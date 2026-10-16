"""
Работа с ONVIF Events через PullPointSubscription.

Камера не отправляет события нам — мы периодически их вытягиваем
через SOAP-запрос PullMessages. Это безопасно (не нужно открывать
входящий порт) и работает за NAT.

Стандартные топики событий:
    tns1:RuleEngine/CellMotionDetector/Motion  — детекция движения
    tns1:RuleEngine/FieldDetector/ObjectsInside — объекты в зоне
    tns1:Device/Trigger/DigitalInput           — тревожный вход
    tns1:VideoSource/MotionAlarm               — тревога движения
    tns1:Device/HardwareFailure/StorageFailure — сбой диска
    tns1:Device/TamperDetector/...             — вскрытие корпуса
    tns1:VideoSource/ImageTooBlurry/...        — потеря фокуса
    tns1:VideoSource/GlobalSceneChange/...     — перекрытие/смена сцены
"""
import hashlib
import logging
import uuid
from base64 import b64encode
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Маппинг топиков → человекочитаемые названия и severity
# ----------------------------------------------------------------------
TOPIC_MAP = {
    "CellMotionDetector/Motion": ("Движение", "info"),
    "FieldDetector/ObjectsInside": ("Объекты в зоне", "info"),
    "MotionAlarm": ("Тревога движения", "alert"),
    "DigitalInput": ("Тревожный вход", "alert"),
    "StorageFailure": ("Сбой носителя", "warning"),
    "TamperDetector": ("Вскрытие корпуса", "warning"),
    "LineDetector": ("Пересечение линии", "info"),
    "Loitering": ("Задержка в зоне", "info"),
    "ImageTooBlurry": ("Изображение размыто", "warning"),
    "ImageTooDark": ("Изображение слишком тёмное", "warning"),
    "GlobalSceneChange": ("Смена сцены / перекрытие", "warning"),
    "NetworkLost": ("Потеря сети", "warning"),
}


def _simplify_topic(topic: str) -> tuple:
    """
    Возвращает (short_name, severity) по полному топику.
    Если не нашли — ("Событие", "info").
    """
    if not topic:
        return ("Событие", "info")

    # Отрезаем префикс tns1:
    t = topic
    if t.startswith("tns1:"):
        t = t[5:]

    # Ищем наиболее специфичное совпадение (по концу строки)
    for key, (name, sev) in TOPIC_MAP.items():
        if key in t:
            return (name, sev)

    # Фолбэк — последняя часть через '/'
    parts = t.split("/")
    if parts:
        return (parts[-1], "info")
    return ("Событие", "info")


# ----------------------------------------------------------------------
# Парсинг одного NotificationMessage
# ----------------------------------------------------------------------
def _parse_message(msg_elem: ET.Element) -> dict | None:
    """
    Разбирает <wsnt:NotificationMessage> в словарь.
    Возвращает None, если не удалось извлечь полезные данные.
    """
    # Ищем Topic
    topic = ""
    for elem in msg_elem.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "Topic" and elem.text:
            topic = elem.text.strip()
            break

    # Ищем Source (SimpleItem с Name="..." и Value="...")
    source = {}
    data = {}
    utc_time = None

    for elem in msg_elem.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "SimpleItem":
            name = elem.attrib.get("Name", "")
            value = elem.attrib.get("Value", "")
            if name:
                # Все SimpleItem собираем и в data — потом разделим
                data[name] = value
        elif tag == "UtcTime" and elem.text:
            utc_time = elem.text.strip()

    # Часть полей в data — это на самом деле Source.
    # Типичные Source-поля: VideoSourceConfigurationToken,
    # VideoAnalyticsConfigurationToken, Rule, InputToken и т.п.
    SOURCE_KEYS = (
        "VideoSourceConfigurationToken",
        "VideoAnalyticsConfigurationToken",
        "Rule",
        "InputToken",
        "VideoOutputConfigurationToken",
        "Channel",
    )
    for k in list(data.keys()):
        if k in SOURCE_KEYS:
            source[k] = data.pop(k)

    # Простое имя топика
    short, severity = _simplify_topic(topic)

    # Пытаемся понять, что говорит событие: IsMotion=true, State=true и т.п.
    # Часто это свойство с именем "IsMotion" / "State" / "Value" / "LogicalState"
    is_active = None
    for k in ("IsMotion", "State", "LogicalState", "Value", "Active"):
        if k in data:
            v = str(data[k]).lower()
            if v in ("true", "1", "yes", "on"):
                is_active = True
            elif v in ("false", "0", "no", "off"):
                is_active = False
            break

    return {
        "topic": topic,
        "topic_short": short,
        "severity": severity,
        "source": source,
        "data": data,
        "is_active": is_active,
        "utc_time": utc_time,
    }


# ----------------------------------------------------------------------
# Клиент событий для одной камеры
# ----------------------------------------------------------------------
class OnvifEvents:
    """
    Управляет PullPoint-подпиской на одной камере.
    Использование:
        ev = OnvifEvents(endpoint, user, password)
        if ev.subscribe():
            events = ev.pull_messages(timeout=5)
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
    NS_TEV = "http://www.onvif.org/ver10/events/wsdl"

    def __init__(self, endpoint, username="", password="", timeout=8):
        self.endpoint = endpoint
        self.username = username
        self.password = password
        self.timeout = timeout

        self._events_endpoint = None
        self._subscription_url = None
        self._subscription_created = 0.0

    # ------------------------------------------------------------------
    def _ws_security_header(self) -> str:
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

    def _soap_call(self, url, action, body_xml):
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope'
            ' xmlns:s="' + self.NS_SOAP + '"'
            ' xmlns:tds="' + self.NS_TDS + '"'
            ' xmlns:tev="' + self.NS_TEV + '"'
            ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
            ' xmlns:tt="http://www.onvif.org/ver10/schema"'
            ' xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"'
            ' xmlns:wsa5="http://www.w3.org/2005/08/addressing">'
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
    def _find_text(root, local_name):
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == local_name:
                text = (elem.text or "").strip()
                return text or None
        return None

    # ------------------------------------------------------------------
    # Events endpoint
    # ------------------------------------------------------------------
    def _get_events_endpoint(self):
        if self._events_endpoint:
            return self._events_endpoint

        body = (
            "<tds:GetCapabilities>"
            "<tds:Category>Events</tds:Category>"
            "</tds:GetCapabilities>"
        )
        action = self.NS_TDS + "/GetCapabilities"

        try:
            root = self._soap_call(self.endpoint, action, body)
        except Exception as e:
            logger.debug("GetCapabilities (Events) failed: %s", e)
            return None

        xaddr = None
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Events":
                for child in elem.iter():
                    ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if ctag == "XAddr" and child.text:
                        xaddr = child.text.strip()
                        break
                if xaddr:
                    break

        self._events_endpoint = xaddr
        return xaddr

    def has_events(self) -> bool:
        """Поддерживает ли камера события."""
        return self._get_events_endpoint() is not None

    # ------------------------------------------------------------------
    # Подписка
    # ------------------------------------------------------------------
    def subscribe(self, initial_termination_minutes: int = 10) -> bool:
        """
        Создаёт PullPoint-подписку. Возвращает True при успехе.
        Срок жизни — initial_termination_minutes, потом надо renew.
        """
        url = self._get_events_endpoint()
        if not url:
            return False

        body = (
            '<tev:CreatePullPointSubscription>'
            '<tev:InitialTerminationTime>PT' +
            str(initial_termination_minutes) + 'M'
            '</tev:InitialTerminationTime>'
            '</tev:CreatePullPointSubscription>'
        )
        action = self.NS_TEV + "/CreatePullPointSubscription"

        try:
            root = self._soap_call(url, action, body)
        except Exception as e:
            logger.warning("CreatePullPointSubscription: %s", e)
            return False

        # Адрес подписки лежит в <wsa5:Address> элемента SubscriptionReference
        sub_url = None
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Address" and elem.text:
                sub_url = elem.text.strip()
                break

        if not sub_url:
            logger.warning("Не найден SubscriptionReference в ответе")
            return False

        self._subscription_url = sub_url
        self._subscription_created = datetime.now().timestamp()
        logger.info("ONVIF Events: подписка создана (%s)", sub_url)
        return True

    def renew_subscription(self, minutes: int = 10) -> bool:
        """Продлевает подписку."""
        if not self._subscription_url:
            return False

        body = (
            '<wsnt:Renew>'
            '<wsnt:TerminationTime>PT' + str(minutes) + 'M</wsnt:TerminationTime>'
            '</wsnt:Renew>'
        )
        action = "http://docs.oasis-open.org/wsn/b-2/Renew"

        try:
            self._soap_call(self._subscription_url, action, body)
            logger.debug("ONVIF Events: подписка продлена")
            return True
        except Exception as e:
            logger.warning("Renew: %s", e)
            return False

    def unsubscribe(self):
        """Закрывает подписку."""
        if not self._subscription_url:
            return
        body = "<wsnt:Unsubscribe/>"
        action = "http://docs.oasis-open.org/wsn/b-2/Unsubscribe"
        try:
            self._soap_call(self._subscription_url, action, body)
        except Exception:
            pass
        self._subscription_url = None
        logger.info("ONVIF Events: подписка закрыта")

    # ------------------------------------------------------------------
    # Опрос
    # ------------------------------------------------------------------
    def pull_messages(self, timeout_sec: int = 5, max_messages: int = 50):
        """
        Запрашивает накопившиеся сообщения.
        Возвращает список dict или [].
        """
        if not self._subscription_url:
            return []

        body = (
            '<tev:PullMessages>'
            '<tev:Timeout>PT' + str(timeout_sec) + 'S</tev:Timeout>'
            '<tev:MessageLimit>' + str(max_messages) + '</tev:MessageLimit>'
            '</tev:PullMessages>'
        )
        action = self.NS_TEV + "/PullMessages"

        try:
            root = self._soap_call(self._subscription_url, action, body)
        except Exception as e:
            logger.warning("PullMessages: %s", e)
            # Подписка могла истечь
            self._subscription_url = None
            return []

        events = []
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "NotificationMessage":
                parsed = _parse_message(elem)
                if parsed:
                    parsed["_ts"] = datetime.now().timestamp()
                    events.append(parsed)

        return events


# ----------------------------------------------------------------------
# Высокоуровневая обёртка для API
# ----------------------------------------------------------------------
def test_events(endpoint: str, username: str = "",
                password: str = "", pull_timeout: int = 5) -> dict:
    """
    Быстрая проверка: создаёт подписку, ждёт события pull_timeout секунд.
    Возвращает:
        {
          "ok": True/False,
          "error": "...",
          "has_events": True/False,
          "subscription_url": "...",
          "events_received": N,
          "events": [...],
        }
    """
    result = {
        "ok": False,
        "error": None,
        "has_events": False,
        "subscription_url": None,
        "events_received": 0,
        "events": [],
    }

    try:
        ev = OnvifEvents(endpoint, username, password)

        if not ev.has_events():
            result["ok"] = True
            result["has_events"] = False
            return result

        result["has_events"] = True

        if not ev.subscribe(initial_termination_minutes=5):
            result["error"] = "Не удалось создать PullPoint-подписку"
            return result

        result["subscription_url"] = ev._subscription_url

        try:
            events = ev.pull_messages(timeout_sec=pull_timeout)
            result["events"] = events
            result["events_received"] = len(events)
        finally:
            ev.unsubscribe()

        result["ok"] = True

    except Exception as e:
        logger.exception("test_events: %s", e)
        result["error"] = str(e)

    return result