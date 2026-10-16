"""
Менеджер ONVIF-событий.

Фоновая задача:
    * раз в N секунд опрашивает PullPoint-подписки всех IP-камер;
    * сохраняет новые события в SQLite;
    * дедуплицирует одинаковые события по группе топика;
    * игнорирует служебные топики (Monitoring/...);
    * ведёт журнал (журнал обрезается по возрасту/количеству).

Хранение:
    * файл БД: рядом с приложением, events.db
"""
import json
import logging
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

from onvif_events import OnvifEvents

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Группировка топиков.
# Одно и то же событие камеры может приходить разными топиками —
# все они считаются одним «логическим» событием для дедупликации.
# ----------------------------------------------------------------------
TOPIC_GROUPS = {
    # Движение
    "MotionAlarm": "motion",
    "CellMotionDetector": "motion",
    "FieldDetector": "motion",
    "LineDetector": "motion",
    "Loitering": "motion",
    # Тревожный вход
    "DigitalInput": "input",
    # Состояние устройства
    "StorageFailure": "storage",
    "TamperDetector": "tamper",
    "NetworkLost": "network",
    "GlobalSceneChange": "scene",
    "ImageTooBlurry": "image",
    "ImageTooDark": "image",
}

# Топики-шум: не сохраняем.
TOPIC_IGNORE = (
    "Monitoring/ProcessorUsage",
    "Monitoring/OperatingTime",
    "Monitoring/SystemTime",
    "Monitoring/SystemLogs",
    "Monitoring/HealthCheck",
)


def _topic_group(topic: str) -> str:
    """
    Возвращает «группу» для дедупликации.
    Разные топики одной группы дают одно событие.
    """
    if not topic:
        return "unknown"

    t = topic
    if t.startswith("tns1:"):
        t = t[5:]

    for marker, group in TOPIC_GROUPS.items():
        if marker in t:
            return group

    return t


def _is_noise(topic: str) -> bool:
    """True, если топик — служебный шум."""
    if not topic:
        return False
    for marker in TOPIC_IGNORE:
        if marker in topic:
            return True
    return False


# ----------------------------------------------------------------------
# Пути
# ----------------------------------------------------------------------
def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()


EVENTS_DB_PATH = _app_dir() / "events.db"


# ----------------------------------------------------------------------
# Хранилище
# ----------------------------------------------------------------------
class EventsDB:
    """SQLite-хранилище событий. Потокобезопасное (через lock)."""

    def __init__(self, path: Path = EVENTS_DB_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        camera_id TEXT NOT NULL,
                        ts REAL NOT NULL,
                        topic TEXT,
                        topic_short TEXT,
                        severity TEXT,
                        is_active INTEGER,
                        source_json TEXT,
                        data_json TEXT,
                        utc_time TEXT
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_camera_ts "
                    "ON events(camera_id, ts DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_ts "
                    "ON events(ts DESC)"
                )
                conn.commit()
            finally:
                conn.close()

    def insert(self, camera_id: str, event: dict) -> int:
        """Сохраняет событие. Возвращает id записи."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """INSERT INTO events
                       (camera_id, ts, topic, topic_short, severity,
                        is_active, source_json, data_json, utc_time)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        camera_id,
                        event.get("_ts", time.time()),
                        event.get("topic") or "",
                        event.get("topic_short") or "",
                        event.get("severity") or "info",
                        1 if event.get("is_active") else 0,
                        json.dumps(event.get("source") or {},
                                   ensure_ascii=False),
                        json.dumps(event.get("data") or {},
                                   ensure_ascii=False),
                        event.get("utc_time"),
                    ),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def query(self, camera_id=None, since_ts=None,
              severity=None, limit: int = 200):
        """Возвращает список событий (новые — первыми)."""
        with self._lock:
            conn = self._connect()
            try:
                sql = "SELECT * FROM events WHERE 1=1"
                params = []
                if camera_id:
                    sql += " AND camera_id = ?"
                    params.append(camera_id)
                if since_ts is not None:
                    sql += " AND ts >= ?"
                    params.append(since_ts)
                if severity:
                    sql += " AND severity = ?"
                    params.append(severity)
                sql += " ORDER BY ts DESC LIMIT ?"
                params.append(int(limit))

                rows = conn.execute(sql, params).fetchall()
                result = []
                for r in rows:
                    result.append({
                        "id": r["id"],
                        "camera_id": r["camera_id"],
                        "ts": r["ts"],
                        "topic": r["topic"],
                        "topic_short": r["topic_short"],
                        "severity": r["severity"],
                        "is_active": bool(r["is_active"]),
                        "source": _safe_json(r["source_json"]),
                        "data": _safe_json(r["data_json"]),
                        "utc_time": r["utc_time"],
                    })
                return result
            finally:
                conn.close()

    def stats(self) -> dict:
        """Сводка: общее количество, диапазон дат, разбивка по severity."""
        with self._lock:
            conn = self._connect()
            try:
                total = conn.execute(
                    "SELECT COUNT(*) AS c FROM events"
                ).fetchone()["c"]

                oldest = conn.execute(
                    "SELECT MIN(ts) AS t FROM events"
                ).fetchone()["t"]
                newest = conn.execute(
                    "SELECT MAX(ts) AS t FROM events"
                ).fetchone()["t"]

                by_sev = {}
                for row in conn.execute(
                    "SELECT severity, COUNT(*) AS c FROM events "
                    "GROUP BY severity"
                ):
                    by_sev[row["severity"]] = row["c"]

                return {
                    "total": total,
                    "oldest_ts": oldest,
                    "newest_ts": newest,
                    "by_severity": by_sev,
                }
            finally:
                conn.close()

    def cleanup(self, max_age_days: int = 30,
                max_records: int = 200000) -> dict:
        """Удаляет старые записи: старше max_age_days и сверх max_records."""
        removed_age = 0
        removed_count = 0
        with self._lock:
            conn = self._connect()
            try:
                cutoff = time.time() - max_age_days * 86400
                cur = conn.execute(
                    "DELETE FROM events WHERE ts < ?", (cutoff,)
                )
                removed_age = cur.rowcount

                total = conn.execute(
                    "SELECT COUNT(*) AS c FROM events"
                ).fetchone()["c"]

                if total > max_records:
                    to_delete = total - max_records
                    cur = conn.execute(
                        """DELETE FROM events WHERE id IN (
                             SELECT id FROM events
                             ORDER BY ts ASC LIMIT ?
                           )""",
                        (to_delete,),
                    )
                    removed_count = cur.rowcount

                conn.commit()
            finally:
                conn.close()

        return {
            "removed_age": removed_age,
            "removed_count": removed_count,
        }


def _safe_json(text):
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


# ----------------------------------------------------------------------
# Менеджер
# ----------------------------------------------------------------------
class EventsManager:
    """Фоновый опрос ONVIF-событий для всех IP-камер."""

    def __init__(self, cameras_provider,
                 poll_interval: float = 2.0,
                 subscription_ttl_min: int = 10,
                 dedup_window: float = 30.0):
        self._cameras_provider = cameras_provider
        self.poll_interval = poll_interval
        self.subscription_ttl_min = subscription_ttl_min
        self.dedup_window = dedup_window

        self.db = EventsDB()
        self._stop_event = threading.Event()
        self._thread = None

        self._sessions = {}
        self._sessions_lock = threading.Lock()
        self._dedup_cache = {}
                # Камеры без ONVIF Events — не проверяем повторно
        self._no_events_cams = set()
        # Подписчики на события (рекордеры и т.п.)
        self._subscribers = []
        self._sub_lock = threading.Lock()

        self.started_at = 0.0
        self.events_saved = 0
        self.last_cleanup_ts = 0.0
        self.cleanup_interval = 300
        self.last_error = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="events-manager", daemon=True
        )
        self._thread.start()
        self.started_at = time.time()
        logger.info("EventsManager запущен (poll каждые %.1f сек)",
                    self.poll_interval)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

        with self._sessions_lock:
            for cam_id, sess in list(self._sessions.items()):
                try:
                    sess["client"].unsubscribe()
                except Exception:
                    pass
            self._sessions.clear()

        logger.info("EventsManager остановлен")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.exception("EventsManager: ошибка в цикле: %s", e)
                self.last_error = str(e)

            self._maybe_cleanup()

            if self._stop_event.wait(self.poll_interval):
                break

    def _tick(self):
        cameras = self._cameras_provider() or {}

        with self._sessions_lock:
            for cam_id in list(self._sessions.keys()):
                if cam_id not in cameras:
                    logger.info("Камера %s удалена — закрываю сессию", cam_id)
                    try:
                        self._sessions[cam_id]["client"].unsubscribe()
                    except Exception:
                        pass
                    del self._sessions[cam_id]

        for cam_id, cam in cameras.items():
            if cam.type != "ip":
                continue
            if not getattr(cam, "source", ""):
                continue

            parsed = self._parse_rtsp(cam.source)
            if not parsed:
                continue

            endpoint = f"http://{parsed['host']}/onvif/device_service"

            session = self._get_or_create_session(cam_id, endpoint, parsed)
            if not session:
                continue

            try:
                events = session["client"].pull_messages(timeout_sec=1)
                for ev in events:
                    self._save_event(cam_id, ev)
            except Exception as e:
                logger.debug("[%s] pull_messages error: %s", cam_id, e)

            age = time.time() - session["last_renew_ts"]
            if age > self.subscription_ttl_min * 60 * 0.7:
                try:
                    if session["client"].renew_subscription(
                            minutes=self.subscription_ttl_min):
                        session["last_renew_ts"] = time.time()
                except Exception:
                    pass

    def _get_or_create_session(self, cam_id, endpoint, parsed):
        with self._sessions_lock:
            sess = self._sessions.get(cam_id)
            if sess:
                return sess
            if cam_id in self._no_events_cams:
                return None

        try:
            client = OnvifEvents(
                endpoint,
                parsed.get("user", ""),
                parsed.get("password", ""),
            )

            if not client.has_events():
                logger.info("[%s] ONVIF Events не поддерживаются — "
                            "больше не проверяю", cam_id)
                with self._sessions_lock:
                    self._no_events_cams.add(cam_id)
                return None

            if not client.subscribe(
                    initial_termination_minutes=self.subscription_ttl_min):
                logger.warning("[%s] Не удалось подписаться на события", cam_id)
                return None

            session = {
                "client": client,
                "endpoint": endpoint,
                "last_renew_ts": time.time(),
            }

            with self._sessions_lock:
                self._sessions[cam_id] = session

            logger.info("[%s] Подписка на ONVIF Events активна", cam_id)
            return session

        except Exception as e:
            logger.warning("[%s] Не удалось создать сессию: %s", cam_id, e)
            self.last_error = str(e)
            return None

    def _save_event(self, cam_id, event):
        """
        Сохраняет событие с дедупликацией по группе топика.
        События из одной группы (MotionAlarm + CellMotionDetector)
        считаются одним логическим событием.
        Служебные топики (Monitoring/...) игнорируются.
        """
        topic = event.get("topic") or ""

        # Отбрасываем шум
        if _is_noise(topic):
            return None

        now = event.get("_ts", time.time())
        is_active = bool(event.get("is_active"))
        group = _topic_group(topic)

        # Дедуп по группе
        key = (cam_id, group)
        last = self._dedup_cache.get(key)

        if last:
            last_ts, last_active = last
            if (last_active == is_active
                    and now - last_ts < self.dedup_window):
                return None

        self._dedup_cache[key] = (now, is_active)

        try:
            self.db.insert(cam_id, event)
            self.events_saved += 1
            logger.info("[%s] Событие: %s (group=%s, active=%s)",
                        cam_id,
                        event.get("topic_short") or topic,
                        group,
                        is_active)

            # Уведомить подписчиков (рекордеры по событиям)
            self._notify_subscribers(cam_id, event)

            return True
        except Exception as e:
            logger.exception("[%s] Ошибка сохранения события: %s", cam_id, e)
            return None

    def _maybe_cleanup(self):
        now = time.time()
        if now - self.last_cleanup_ts < self.cleanup_interval:
            return
        self.last_cleanup_ts = now
        try:
            result = self.db.cleanup(max_age_days=30, max_records=200000)
            if result["removed_age"] or result["removed_count"]:
                logger.info("EventsDB: удалено %d старых и %d сверх лимита",
                            result["removed_age"], result["removed_count"])
        except Exception as e:
            logger.warning("Ошибка чистки EventsDB: %s", e)

    @staticmethod
    def _parse_rtsp(rtsp_url: str):
        m = re.match(
            r"^rtsp://"
            r"(?:(?P<user>[^:@/]+)(?::(?P<pass>[^@/]*))?@)?"
            r"(?P<host>[^:/]+)"
            r"(?::(?P<port>\d+))?"
            r"(?P<path>/.*)?$",
            rtsp_url.strip(),
            re.IGNORECASE,
        )
        if not m:
            return None
        return {
            "user": m.group("user") or "",
            "password": m.group("pass") or "",
            "host": m.group("host") or "",
            "port": int(m.group("port")) if m.group("port") else 554,
            "path": m.group("path") or "",
        }

    def get_status(self) -> dict:
        with self._sessions_lock:
            active_cams = list(self._sessions.keys())
        stats = self.db.stats()
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "started_at": self.started_at,
            "poll_interval": self.poll_interval,
            "dedup_window": self.dedup_window,
            "active_cameras": active_cams,
            "events_saved": self.events_saved,
            "last_error": self.last_error,
            "db_stats": stats,
        }

    def subscribe(self, callback):
        """
        Подписка на события. Callback вызывается как callback(camera_id, event).
        Используется EventRecorder для триггера записи.
        """
        with self._sub_lock:
            self._subscribers.append(callback)
        logger.debug("EventsManager: добавлен подписчик %s", callback)

    def unsubscribe(self, callback):
        with self._sub_lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)
        logger.debug("EventsManager: удалён подписчик %s", callback)

    def _notify_subscribers(self, cam_id, event):
        with self._sub_lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(cam_id, event)
            except Exception as e:
                logger.debug("EventsManager: ошибка подписчика: %s", e)