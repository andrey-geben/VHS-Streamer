"""
Рекордер — DVR-подобная циклическая запись с ротацией сегментов.

Логика:
    * камера шлёт кадры в очередь рекордера (не блокируя захват);
    * отдельный поток открывает новый файл каждые `segment_minutes` минут;
    * периодически (cleanup_interval_sec) выполняется чистка:
        - удаляются файлы старше max_age_days;
        - если общий размер превышает max_size_gb, удаляются самые старые.
"""
import queue
import time
import logging
import threading
from datetime import datetime
from pathlib import Path

import cv2

logger = logging.getLogger(__name__)


class Recorder:
    def __init__(self, camera, archive_cfg: dict, camera_cfg: dict):
        self.camera = camera
        self.cfg = archive_cfg
        self.camera_cfg = camera_cfg

        self.base_path: Path = Path(archive_cfg["path"]).resolve() / camera.id
        self.base_path.mkdir(parents=True, exist_ok=True)

        self.segment_seconds: int = int(archive_cfg.get("segment_minutes", 5)) * 60
        self.codec: str = archive_cfg.get("codec", "mp4v")
        self.extension: str = archive_cfg.get("extension", ".mp4")
        self.max_size_bytes: int = int(archive_cfg.get("max_size_gb", 50) * (1024 ** 3))
        self.max_age_sec: int = int(archive_cfg.get("max_age_days", 30) * 86400)
        self.cleanup_interval: int = int(archive_cfg.get("cleanup_interval_sec", 60))

        self._queue: queue.Queue = queue.Queue(maxsize=120)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._current_path: Path | None = None
        self._current_start: float = 0.0
        self._fourcc = cv2.VideoWriter_fourcc(*self.codec)

        # Статистика
        self.segments_written: int = 0
        self.frames_written: int = 0
        self.bytes_written: int = 0
        self.last_cleanup_ts: float = 0.0
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    # Управление
    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"recorder-{self.camera.id}", daemon=True
        )
        self._thread.start()
        self.camera.subscribe(self._on_frame)
        logger.info("[%s] Рекордер запущен (%s)", self.camera.id, self.base_path)

    def stop(self):
        self._stop_event.set()
        self.camera.unsubscribe(self._on_frame)
        if self._thread:
            self._thread.join(timeout=5)
        # закрываем текущий writer извне — writer живёт в потоке, но
        # после join он уже закрыт в _run()
        logger.info("[%s] Рекордер остановлен", self.camera.id)

    # ------------------------------------------------------------------
    # Приём кадра от камеры
    # ------------------------------------------------------------------
    def _on_frame(self, frame):
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            # Сбросим самый старый и положим текущий
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(frame)
            except queue.Empty:
                pass

    # ------------------------------------------------------------------
    # Рабочий поток
    # ------------------------------------------------------------------
    def _run(self):
        writer: cv2.VideoWriter | None = None
        frame_size: tuple[int, int] | None = None
        fps: int = max(1, self.camera.fps)

        while not self._stop_event.is_set():
            try:
                frame = self._queue.get(timeout=0.5)
            except queue.Empty:
                # Периодическая чистка на случай простоя
                self._maybe_cleanup()
                continue

            # --- открыть новый сегмент при необходимости ---
            need_new = (
                writer is None
                or self._current_path is None
                or (time.time() - self._current_start) >= self.segment_seconds
            )

            if need_new:
                if writer is not None:
                    writer.release()
                    writer = None
                    self.segments_written += 1
                # размер кадра фиксируем по первому кадру в сегменте
                frame_size = (frame.shape[1], frame.shape[0])
                self._current_path = self._new_file_path()
                writer = cv2.VideoWriter(
                    str(self._current_path),
                    self._fourcc,
                    fps,
                    frame_size,
                )
                if not writer.isOpened():
                    self.last_error = f"Не удалось открыть VideoWriter: {self._current_path}"
                    logger.error("[%s] %s", self.camera.id, self.last_error)
                    writer = None
                    self._current_path = None
                    time.sleep(1)
                    continue
                self._current_start = time.time()
                logger.info(
                    "[%s] Новый сегмент: %s", self.camera.id, self._current_path.name
                )

            # --- размеры должны совпадать ---
            if frame.shape[1] != frame_size[0] or frame.shape[0] != frame_size[1]:
                frame = cv2.resize(frame, frame_size)

            try:
                writer.write(frame)
                self.frames_written += 1
                self.bytes_written += frame.nbytes // 4  # очень грубая оценка
            except Exception as e:
                self.last_error = str(e)
                logger.error("[%s] Ошибка записи кадра: %s", self.camera.id, e)

            self._maybe_cleanup()

        # корректное завершение
        if writer is not None:
            writer.release()
            self.segments_written += 1

    def _new_file_path(self) -> Path:
        """Путь вида archive/<cam>/YYYY-MM-DD/HH-MM-SS.mp4"""
        now = datetime.now()
        day_dir = self.base_path / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        return day_dir / f"{now.strftime('%H-%M-%S')}{self.extension}"

    # ------------------------------------------------------------------
    # Чистка
    # ------------------------------------------------------------------
    def _maybe_cleanup(self):
        now = time.time()
        if now - self.last_cleanup_ts < self.cleanup_interval:
            return
        self.last_cleanup_ts = now
        try:
            self._cleanup()
        except Exception as e:
            logger.warning("[%s] Ошибка чистки архива: %s", self.camera.id, e)

    def _list_files(self) -> list[Path]:
        files = []
        for p in self.base_path.rglob(f"*{self.extension}"):
            if p.is_file():
                files.append(p)
        files.sort(key=lambda f: f.stat().st_mtime)  # от старых к новым
        return files

    def _cleanup(self):
        files = self._list_files()
        now = time.time()

        # 1) удаление по возрасту
        removed_age = 0
        remaining = []
        for f in files:
            if f == self._current_path:
                remaining.append(f)
                continue
            try:
                age = now - f.stat().st_mtime
                if age > self.max_age_sec:
                    f.unlink(missing_ok=True)
                    removed_age += 1
                    continue
            except FileNotFoundError:
                continue
            remaining.append(f)

        # 2) удаление по размеру
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
            if f == self._current_path:
                continue
            try:
                sz = f.stat().st_size
                f.unlink(missing_ok=True)
                total -= sz
                removed_size += 1
            except FileNotFoundError:
                continue

        # удаляем пустые папки
        for d in sorted(self.base_path.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass

        if removed_age or removed_size:
            logger.info(
                "[%s] Чистка архива: удалено %s по возрасту, %s по размеру",
                self.camera.id, removed_age, removed_size,
            )

    # ------------------------------------------------------------------
    # Статус
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        try:
            files = self._list_files()
            total_size = sum(f.stat().st_size for f in files)
            oldest = files[0].stat().st_mtime if files else None
        except Exception:
            total_size = 0
            oldest = None
        return {
            "camera_id": self.camera.id,
            "segments": self.segments_written,
            "frames": self.frames_written,
            "current_file": str(self._current_path) if self._current_path else None,
            "archive_size_mb": round(total_size / (1024 * 1024), 1),
            "archive_size_limit_mb": round(self.max_size_bytes / (1024 * 1024), 1),
            "oldest_ts": oldest,
            "last_error": self.last_error,
        }