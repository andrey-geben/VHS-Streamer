"""
Рекордер по событиям.

Пишет сегменты в архив не постоянно, а только когда приходит событие
(движение, тревожный вход, вскрытие корпуса) с камеры.

Логика:
    * подписывается на события из EventsManager;
    * при событии is_active=true запускает запись FFmpeg на duration секунд;
    * если во время записи приходит новое событие — таймер продлевается;
    * между событиями запись не ведётся.
"""
import logging
import platform
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

import bootstrap
from events_manager import _topic_group

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"

FFMPEG_BIN = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
FFMPEG_PATH = str(bootstrap.get_path(FFMPEG_BIN)).replace("\\", "/")

RTSP_HOST = "127.0.0.1"
RTSP_PORT = 8554
MEDIAMTX_API = "http://127.0.0.1:9997"


class EventRecorder:
    """Запись по событиям."""

    def __init__(self, camera, archive_cfg, camera_cfg, events_manager):
        self.camera = camera
        self.cfg = archive_cfg
        self.camera_cfg = camera_cfg
        self._events_manager = events_manager

        # Путь архива — как в обычном Recorder
        individual_path = (camera_cfg.get("archive_path") or "").strip()
        if individual_path:
            base = Path(individual_path).expanduser()
            if "{camera_id}" in str(base):
                base = Path(str(base).replace("{camera_id}", camera.id))
            elif base.name != camera.id:
                base = base / camera.id
        else:
            base = Path(archive_cfg["path"]).expanduser() / camera.id

        self.base_path: Path = base.resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)

        # Настройки событийного режима
        self.event_duration: int = int(
            camera_cfg.get("event_record_duration", 30)
        )
        topics = camera_cfg.get("event_topics") or ["motion", "input", "tamper"]
        self.event_topics = set(topics)

        # Лимиты архива
        self.max_size_bytes = int(
            archive_cfg.get("max_size_gb", 50) * (1024 ** 3)
        )
        self.max_age_sec = int(
            archive_cfg.get("max_age_days", 30) * 86400
        )
        self.cleanup_interval = int(
            archive_cfg.get("cleanup_interval_sec", 60)
        )

        # Внутреннее состояние
        self._active_until: float = 0.0
        self._active_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._stderr_thread: threading.Thread | None = None
        self.current_file: str | None = None

        # Статистика
        self.segments_written: int = 0
        self.events_triggered: int = 0
        self.last_error: str | None = None
        self.last_cleanup_ts: float = 0.0

    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()

        # Подписка на события
        try:
            self._events_manager.subscribe(self._on_event)
        except Exception as e:
            logger.warning("[%s] EventRecorder: не удалось подписаться "
                           "на события: %s", self.camera.id, e)

        self._thread = threading.Thread(
            target=self._run,
            name=f"event-recorder-{self.camera.id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[%s] EventRecorder запущен (duration=%s сек, topics=%s)",
            self.camera.id, self.event_duration, sorted(self.event_topics),
        )

    def stop(self):
        self._stop_event.set()
        try:
            self._events_manager.unsubscribe(self._on_event)
        except Exception:
            pass

        # Закрыть активный сегмент
        self._stop_current_segment()

        if self._thread:
            self._thread.join(timeout=5)

        logger.info("[%s] EventRecorder остановлен", self.camera.id)

    # ------------------------------------------------------------------
    def _on_event(self, cam_id, event):
        """Колбэк из EventsManager — новое событие."""
        if cam_id != self.camera.id:
            return
        # Реагируем только на начало события
        if not event.get("is_active"):
            return

        topic = event.get("topic", "")
        group = _topic_group(topic)
        if group not in self.event_topics:
            return

        with self._active_lock:
            self._active_until = time.time() + self.event_duration
            self.events_triggered += 1

        logger.info(
            "[%s] EventRecorder: старт/продление записи на %s сек (событие=%s)",
            self.camera.id, self.event_duration, group,
        )

    # ------------------------------------------------------------------
    def _run(self):
        raw_name = f"__raw_{self.camera.id}"
        raw_url = f"rtsp://{RTSP_HOST}:{RTSP_PORT}/{raw_name}"

        while not self._stop_event.is_set():
            with self._active_lock:
                active_until = self._active_until

            now = time.time()
            if now >= active_until:
                # Не пишем — ждём событие
                if self._proc and self._proc.poll() is None:
                    self._stop_current_segment()
                self._maybe_cleanup()
                if self._stop_event.wait(0.5):
                    break
                continue

            # Нужно писать — если ещё не пишем, запускаем FFmpeg
            if self._proc is None or self._proc.poll() is not None:
                if not self._wait_raw_ready(raw_name):
                    if self._stop_event.wait(1):
                        break
                    continue

                seg_path = self._new_segment_path()
                self.current_file = str(seg_path)
                cmd = self._build_ffmpeg_cmd(raw_url, seg_path)
                logger.info("[%s] EventRecorder: новый сегмент: %s",
                            self.camera.id, seg_path.name)

                try:
                    self._proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        creationflags=(
                            subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
                        ),
                    )
                    self._stderr_thread = threading.Thread(
                        target=self._read_stderr,
                        args=(self._proc, seg_path.name),
                        daemon=True,
                    )
                    self._stderr_thread.start()
                except Exception as e:
                    self.last_error = str(e)
                    logger.error("[%s] EventRecorder: ошибка FFmpeg: %s",
                                 self.camera.id, e)
                    if self._stop_event.wait(2):
                        break
                    continue

            self._maybe_cleanup()
            if self._stop_event.wait(0.5):
                break

        self._stop_current_segment()

    # ------------------------------------------------------------------
    def _stop_current_segment(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception:
                pass
            self.segments_written += 1
            logger.info("[%s] EventRecorder: сегмент закрыт", self.camera.id)
        self._proc = None
        self.current_file = None

    def _build_ffmpeg_cmd(self, url: str, out_path: Path):
        cmd = [
            FFMPEG_PATH, "-nostdin", "-loglevel", "warning",
            "-rtsp_transport", "tcp", "-i", url,
            "-c:v", "copy",
            "-an",
            "-y", str(out_path),
        ]
        return cmd

    def _read_stderr(self, proc: subprocess.Popen, seg_name: str):
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                low = line.lower()
                if any(k in low for k in ("error", "fail", "fatal", "invalid")):
                    logger.error("[%s][ffmpeg %s] %s",
                                 self.camera.id, seg_name, line)
                elif "warn" in low:
                    logger.warning("[%s][ffmpeg %s] %s",
                                   self.camera.id, seg_name, line)
                else:
                    logger.debug("[%s][ffmpeg %s] %s",
                                 self.camera.id, seg_name, line)
        except Exception:
            pass

    def _wait_raw_ready(self, raw_name: str, timeout: int = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            try:
                r = requests.get(
                    f"{MEDIAMTX_API}/v3/paths/get/{raw_name}", timeout=2
                )
                if r.status_code == 200 and r.json().get("ready"):
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _new_segment_path(self) -> Path:
        now = datetime.now()
        day_dir = self.base_path / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        base_name = now.strftime("%H-%M-%S")
        candidate = day_dir / f"{base_name}.mp4"
        suffix = 1
        while candidate.exists():
            candidate = day_dir / f"{base_name}_{suffix}.mp4"
            suffix += 1
        return candidate

    # ------------------------------------------------------------------
    def _maybe_cleanup(self):
        now = time.time()
        if now - self.last_cleanup_ts < self.cleanup_interval:
            return
        self.last_cleanup_ts = now
        try:
            self._cleanup()
        except Exception as e:
            logger.warning("[%s] EventRecorder: ошибка чистки: %s",
                           self.camera.id, e)

    def _list_files(self):
        files = []
        for p in self.base_path.rglob("*.mp4"):
            if p.is_file():
                files.append(p)
        files.sort(key=lambda f: f.stat().st_mtime)
        return files

    def _cleanup(self):
        files = self._list_files()
        now = time.time()

        removed_age = 0
        remaining = []
        for f in files:
            try:
                age = now - f.stat().st_mtime
                if age > self.max_age_sec:
                    f.unlink(missing_ok=True)
                    removed_age += 1
                    continue
            except FileNotFoundError:
                continue
            remaining.append(f)

        total = 0
        for f in remaining:
            try:
                total += f.stat().st_size
            except FileNotFoundError:
                pass

        removed_size = 0
        for f in remaining:
            if total <= self.max_size_bytes:
                break
            try:
                sz = f.stat().st_size
                f.unlink(missing_ok=True)
                total -= sz
                removed_size += 1
            except FileNotFoundError:
                continue

        for d in sorted(self.base_path.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass

        if removed_age or removed_size:
            logger.info(
                "[%s] EventRecorder: чистка — %d старых, %d сверх лимита",
                self.camera.id, removed_age, removed_size,
            )

    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        try:
            files = self._list_files()
            total_size = sum(f.stat().st_size for f in files)
            oldest = files[0].stat().st_mtime if files else None
        except Exception:
            total_size = 0
            oldest = None

        is_recording = self._proc is not None and self._proc.poll() is None

        with self._active_lock:
            active_until = self._active_until

        return {
            "camera_id": self.camera.id,
            "mode": "events",
            "segments": self.segments_written,
            "frames": 0,
            "current_file": self.current_file,
            "archive_size_mb": round(total_size / (1024 * 1024), 1),
            "archive_size_limit_mb": round(
                self.max_size_bytes / (1024 * 1024), 1
            ),
            "oldest_ts": oldest,
            "last_error": self.last_error,
            "include_audio": False,
            "is_recording": is_recording,
            "events_triggered": self.events_triggered,
            "active_until": active_until if is_recording else None,
        }
