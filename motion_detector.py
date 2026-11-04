"""
Motion detection на OpenCV (MOG2).

Независимый от камеры способ узнавать о движении: сервер сам смотрит
на кадры и решает «здесь что-то изменилось». Работает для любой камеры
(IP, USB, аналоговой), потому что кадры всё равно проходят через Camera._run().

Архитектура (шаг 3+):
    Camera._run() → кадр → MotionDetector.on_frame()
                                 ↓ (если движение)
                           callback(cam_id, event_dict)
                                 ↓
                           EventsManager.ingest_event()
                                 ↓
                     ┌───────────┴───────────┐
                     ↓                       ↓
                /events (журнал)      EventRecorder → запись в архив

На шаге 1 (этот файл) — только детектор + callback. Подключение к
EventsManager — отдельным шагом после проверки изолированно.

Логика событий:
    - Начало движения →  emit(active=True,  dedup=True,  save=True)
      (в БД + EventRecorder)
    - Движение идёт N сек → emit(active=True, dedup=False, save=False)
      раз в re_emit_interval (только EventRecorder, чтобы не рвать запись
      и не засорять /events)
    - Тишина > settle_seconds → emit(active=False, dedup=True, save=True)
      (в БД + EventRecorder)
"""
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Топик в формате ONVIF — чтобы _topic_group() в events_manager.py
# распознал его как "motion" (CellMotionDetector → "motion").
OPENCV_MOTION_TOPIC = "tns1:RuleEngine/CellMotionDetector/Motion"


class MotionDetector:
    """
    Детектор движения для одной камеры.

    Использование:
        det = MotionDetector("cam1", {"min_area": 500}, on_event_cb)
        det.on_frame(frame_bgr)  # на каждый кадр
    """

    def __init__(
        self,
        camera_id: str,
        config: dict,
        on_event: Callable[[str, dict], None],
    ):
        self.camera_id = camera_id
        self.cfg = config or {}
        self._on_event = on_event

        # Настройки (дефолты — «просто работает»)
        self.frame_skip: int = max(1, int(self.cfg.get("frame_skip", 2)))
        self.min_area: int = max(1, int(self.cfg.get("min_area", 500)))
        self.settle_seconds: float = float(
            self.cfg.get("settle_seconds", 5.0)
        )
        self.re_emit_interval: float = float(
            self.cfg.get("re_emit_interval", 10.0)
        )
        self.analysis_width: int = int(
            self.cfg.get("analysis_width", 320)
        )

        # MOG2 — стандартный вычитатель фона OpenCV
        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=500,
            varThreshold=16,
            detectShadows=False,
        )
        # Морфология — убираем шум от сенсора и сжатия
        self._kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (3, 3)
        )

        # Состояние
        self._frame_count: int = 0
        self._is_active: bool = False
        self._last_motion_ts: float = 0.0
        self._last_emit_ts: float = 0.0
        self._last_area: int = 0
        self._started_at: float = time.time()

    # ------------------------------------------------------------------
    def on_frame(self, frame_bgr: np.ndarray):
        """
        Обрабатывает один кадр (BGR, numpy).
        Вызывает self._on_event(...) при смене состояния и для
        периодического продления активного события.
        """
        if frame_bgr is None or frame_bgr.size == 0:
            return

        self._frame_count += 1

        # Frame skip — обрабатываем только каждый N-й кадр
        if self._frame_count % self.frame_skip != 0:
            return

        # Downscale для скорости
        h, w = frame_bgr.shape[:2]
        if w > self.analysis_width:
            scale = self.analysis_width / float(w)
            new_w = self.analysis_width
            new_h = max(1, int(h * scale))
            small = cv2.resize(
                frame_bgr, (new_w, new_h),
                interpolation=cv2.INTER_AREA,
            )
        else:
            small = frame_bgr
            scale = 1.0

        try:
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            mask = self._bg.apply(gray)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
            mask = cv2.dilate(mask, self._kernel, iterations=1)
            contours, _ = cv2.findContours(
                mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
        except cv2.error as e:
            logger.debug(
                "[%s] OpenCV error в on_frame: %s", self.camera_id, e
            )
            return

        if contours:
            max_contour = max(contours, key=cv2.contourArea)
            area_small = cv2.contourArea(max_contour)
        else:
            area_small = 0.0

        # Пересчёт площади в исходное разрешение
        if scale > 0:
            area = area_small / (scale * scale)
        else:
            area = area_small

        self._last_area = int(area)
        now = time.time()
        motion = area >= self.min_area

        if motion:
            self._last_motion_ts = now
            if not self._is_active:
                # Начало движения
                self._is_active = True
                self._emit(active=True, dedup=True, save=True)
                self._last_emit_ts = now
            else:
                # Движение продолжается — продлеваем запись,
                # но не засоряем /events повторными записями
                if now - self._last_emit_ts >= self.re_emit_interval:
                    self._emit(active=True, dedup=False, save=False)
                    self._last_emit_ts = now
        else:
            if self._is_active:
                idle = now - self._last_motion_ts
                if idle >= self.settle_seconds:
                    # Окончание движения
                    self._is_active = False
                    self._emit(active=False, dedup=True, save=True)

    # ------------------------------------------------------------------
    def _emit(self, active: bool, dedup: bool, save: bool):
        event = {
            "topic": OPENCV_MOTION_TOPIC,
            "topic_short": "Движение (OpenCV)",
            "severity": "info",
            "is_active": active,
            "source": {
                "Detector": "opencv_mog2",
                "CameraId": self.camera_id,
            },
            "data": {
                "area_px": self._last_area,
                "min_area": self.min_area,
            },
            "utc_time": datetime.now(timezone.utc).isoformat(),
            "_ts": time.time(),
            "_dedup": dedup,
            "_save": save,
        }
        try:
            self._on_event(self.camera_id, event)
        except Exception as e:
            logger.exception(
                "[%s] MotionDetector: ошибка callback: %s",
                self.camera_id, e,
            )

    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "frame_skip": self.frame_skip,
            "min_area": self.min_area,
            "settle_seconds": self.settle_seconds,
            "re_emit_interval": self.re_emit_interval,
            "analysis_width": self.analysis_width,
            "is_active": self._is_active,
            "last_area": self._last_area,
            "frames_processed": self._frame_count,
            "uptime_sec": round(time.time() - self._started_at, 1),
        }


class MotionDetectorManager:
    """
    Управляет детекторами для всех камер.

    На шаге 1 (изолированная проверка) не используется.
    Подключается к app.py на шаге 3:

        mgr = MotionDetectorManager(config.get("motion_detection", {}),
                                     on_event=events_manager.ingest_event)
        for cam in cameras.values():
            mgr.attach_camera(cam.id, cam_cfg)
            cam.subscribe(lambda frame, cid=cam.id: mgr.process_frame(cid, frame))
    """

    def __init__(
        self,
        config: dict,
        on_event: Callable[[str, dict], None],
    ):
        self.cfg = config or {}
        self._on_event = on_event
        self._detectors: dict[str, MotionDetector] = {}
        self._process_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def attach_camera(self, camera_id: str, camera_cfg: dict):
        """
        Подписать детектор на камеру.
        Если camera_cfg['motion_detection_enabled'] == False — не подписывает.
        Параметры min_area и др. берутся из camera_cfg (per-camera override)
        или из motion_detection.global (значения по умолчанию).
        """
        with self._lock:
            if camera_id in self._detectors:
                return
            if camera_cfg.get("motion_detection_enabled") is False:
                logger.info(
                    "[%s] MotionDetector: отключён в настройках камеры",
                    camera_id,
                )
                return
            cfg = self._merged_config(camera_id, camera_cfg)
            det = MotionDetector(camera_id, cfg, self._on_event)
            self._detectors[camera_id] = det
            self._process_locks[camera_id] = threading.Lock()
        logger.info(
            "[%s] MotionDetector: подписан (min_area=%s, frame_skip=%s)",
            camera_id, det.min_area, det.frame_skip,
        )

    def process_frame(self, camera_id: str, frame_bgr):
        """
        Вызывается из Camera._run() через Camera.subscribe().
        Skip-if-busy: если детектор ещё обрабатывает предыдущий кадр —
        пропускаем. Это не даёт Camera._run() блокироваться на MOG2
        (иначе MJPEG-поток тормозит).
        """
        det = self._detectors.get(camera_id)
        if det is None:
            return
        lock = self._process_locks.get(camera_id)
        if lock is None:
            return
        if not lock.acquire(blocking=False):
            return  # детектор занят, пропускаем кадр
        try:
            det.on_frame(frame_bgr)
        finally:
            lock.release()

    def detach_camera(self, camera_id: str):
        """
        Отписать детектор от камеры. Вызывается при удалении камеры
        или перед пересозданием (при редактировании).
        """
        with self._lock:
            det = self._detectors.pop(camera_id, None)
            self._process_locks.pop(camera_id, None)
        if det is not None:
            logger.info("[%s] MotionDetector: отписан", camera_id)

    def _merged_config(self, camera_id: str, camera_cfg: dict | None = None) -> dict:
        """
        Собирает итоговый конфиг для MotionDetector одной камеры.

        Приоритет (от высшего к низшему):
            1. camera_cfg['motion_min_area'] — переопределение из объекта камеры
               (задаётся в UI, пишется в config.yaml → Camera → cam_cfg)
            2. motion_detection.per_camera.<cam_id>.min_area — per-camera
               override в глобальной секции config.yaml
            3. motion_detection.global.min_area — общий дефолт
        """
        merged = dict(self.cfg.get("global", {}) or {})

        # Per-camera из глобальной секции config.yaml
        per_cam = (self.cfg.get("per_camera", {}) or {}).get(camera_id, {})
        if per_cam:
            merged.update(per_cam)

        # Per-camera override из объекта камеры (приоритет выше)
        if camera_cfg:
            if "motion_min_area" in camera_cfg:
                try:
                    merged["min_area"] = int(
                        camera_cfg["motion_min_area"]
                    )
                except (TypeError, ValueError):
                    pass

        return merged

    def get_status(self) -> dict:
        with self._lock:
            dets = list(self._detectors.values())
        return {
            "count": len(dets),
            "detectors": [d.get_status() for d in dets],
        }