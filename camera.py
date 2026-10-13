"""
Камера: захват, буферизация, MJPEG-трансляция,
публикация кадров подписчикам (для рекордера).

Поддерживает три источника:
    1. IP-камера (RTSP) — читается напрямую OpenCV.
    2. Аналоговая камера (dshow/V4L2) — читается напрямую OpenCV,
       если для неё нет loopback.
    3. preview_source (RTSP из MediaMTX) — если задан, читается вместо
       физического устройства.
"""
import os
import platform
import time
import threading
import logging
import cv2

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"


class Camera:
    def __init__(self, config: dict):
        self.id: str = config["id"]
        self.name: str = config["name"]
        self.type: str = config.get("type", "ip")
        self.source = config["source"]
        self.preview_source: str | None = config.get("preview_source")

        ci = config.get("capture_index")
        try:
            self.capture_index: int | None = int(ci) if ci is not None else None
        except (TypeError, ValueError):
            self.capture_index = None

        self.fps: int = int(config.get("fps", 25))
        self.width: int = int(config.get("width", 640))
        self.height: int = int(config.get("height", 480))
        self.transport: str | None = config.get("transport")
        self.open_timeout_ms: int = int(config.get("open_timeout_ms", 5000))
        self.read_timeout_ms: int = int(config.get("read_timeout_ms", 5000))

        self._cap: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self._last_frame: bytes | None = None
        self._frame_lock = threading.Lock()
        self._connected: bool = False
        self._last_error: str | None = None

        self._subscribers: list = []
        self._sub_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Подписчики (для рекордера)
    # ------------------------------------------------------------------
    def subscribe(self, callback):
        with self._sub_lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback):
        with self._sub_lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    # ------------------------------------------------------------------
    # FFmpeg-опции для RTSP
    # ------------------------------------------------------------------
    def _build_ffmpeg_options(self) -> str:
        """
        Опции OpenCV/FFmpeg для чтения RTSP.
        Для loopback-потоков используем TCP и увеличенный буфер,
        чтобы избежать потери пакетов и ошибок декодирования.
        """
        opts = []
        opts.append("rtsp_transport;tcp")
        opts.append("buffer_size;2048000")
        opts.append("max_delay;500000")
        opts.append("reorder_queue_size;0")
        if self.open_timeout_ms:
            opts.append(f"stimeout;{self.open_timeout_ms * 1000}")
        return "|".join(opts)

    # ------------------------------------------------------------------
    # Открытие RTSP
    # ------------------------------------------------------------------
    def _open_rtsp(self, url: str) -> cv2.VideoCapture:
        ff_opts = self._build_ffmpeg_options()
        if ff_opts:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = ff_opts

        params = []
        if self.open_timeout_ms:
            params += [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.open_timeout_ms]
        if self.read_timeout_ms:
            params += [cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.read_timeout_ms]

        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    # ------------------------------------------------------------------
    # Открытие камеры (точка входа)
    # ------------------------------------------------------------------
    def _open_capture(self) -> cv2.VideoCapture:
        if self.preview_source:
            logger.info("[%s] Захват из loopback: %s",
                        self.id, self.preview_source)
            return self._open_rtsp(self.preview_source)

        if self.type == "analog":
            backend = cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_V4L2
            if IS_WINDOWS:
                if self.capture_index is not None:
                    source = self.capture_index
                else:
                    try:
                        source = int(self.source)
                    except (TypeError, ValueError):
                        raise RuntimeError(
                            f"Камера '{self.id}': на Windows OpenCV не умеет "
                            f"открывать камеру по имени. Добавьте capture_index."
                        )
            else:
                src = self.source
                source = int(src) if (isinstance(src, str) and src.isdigit()) else src
            cap = cv2.VideoCapture(source, backend)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            return cap
        else:
            return self._open_rtsp(self.source)

    # ------------------------------------------------------------------
    # Старт / стоп
    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"camera-{self.id}", daemon=True
        )
        self._thread.start()
        logger.info("[%s] Поток камеры запущен", self.id)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._release()
        logger.info("[%s] Поток камеры остановлен", self.id)

    def _release(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        self._connected = False

    # ------------------------------------------------------------------
    # Главный цикл
    # ------------------------------------------------------------------
    def _run(self):
        reconnect_delay = 1
        last_jpeg_time = 0.0
        # Целевой интервал MJPEG (сек). Совпадает с fps камеры.
        jpeg_interval = 1.0 / max(self.fps, 1)
        # Ширина превью: 640px хватает и для карточек,
        # и для полноэкранного просмотра. Кодировать 1280×720 ради
        # показа в 240-пиксельной карточке — впустую жечь CPU.
        preview_width = 640

        while not self._stop_event.is_set():
            # --- Подключение ---
            if self._cap is None or not self._cap.isOpened():
                try:
                    self._cap = self._open_capture()
                    if not self._cap.isOpened():
                        raise IOError("не удалось открыть поток")
                    self._connected = True
                    self._last_error = None
                    reconnect_delay = 1
                    logger.info("[%s] Подключено", self.id)
                except Exception as e:
                    self._connected = False
                    self._last_error = str(e)
                    logger.warning(
                        "[%s] Ошибка подключения: %s. Повтор через %s c",
                        self.id, e, reconnect_delay,
                    )
                    self._release()
                    if self._stop_event.wait(reconnect_delay):
                        break
                    reconnect_delay = min(reconnect_delay * 2, 30)
                    continue

            # --- Читаем кадр ---
            ret, frame = self._cap.read()

            # --- Дренируем буфер: пропускаем устаревшие кадры ---
            # Если декодер отстаёт, в буфере OpenCV скапливаются старые
            # кадры. Забираем самый свежий через быстрый grab().
            if ret:
                for _ in range(5):
                    if not self._cap.grab():
                        break
                    r2, f2 = self._cap.retrieve()
                    if r2 and f2 is not None:
                        frame = f2

            if not ret or frame is None:
                self._connected = False
                self._last_error = "потеря сигнала"
                logger.warning("[%s] Потеря сигнала, переподключение", self.id)
                self._release()
                if self._stop_event.wait(1):
                    break
                continue

            # --- MJPEG: кодируем не чаще fps, из уменьшенной копии ---
            now = time.time()
            if now - last_jpeg_time >= jpeg_interval:
                try:
                    h, w = frame.shape[:2]
                    if w > preview_width:
                        scale = preview_width / w
                        small = cv2.resize(
                            frame,
                            (preview_width, int(h * scale)),
                            interpolation=cv2.INTER_AREA,
                        )
                    else:
                        small = frame

                    ok, jpg = cv2.imencode(
                        ".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70]
                    )
                    if ok:
                        with self._frame_lock:
                            self._last_frame = jpg.tobytes()
                    last_jpeg_time = now
                except Exception as e:
                    logger.debug("[%s] Ошибка кодирования JPEG: %s",
                                 self.id, e)

            # --- Рекордеру публикуем ПОЛНЫЙ кадр (не уменьшенный) ---
            with self._sub_lock:
                subs = list(self._subscribers)
            if subs:
                frame_copy = frame.copy()
                for cb in subs:
                    try:
                        cb(frame_copy)
                    except Exception as e:
                        logger.debug("[%s] Ошибка подписчика: %s", self.id, e)

            # --- Управление темпом: спим только если успеваем ---
            elapsed = time.time() - now
            if elapsed < jpeg_interval:
                time.sleep(jpeg_interval - elapsed)

        self._release()
    # ------------------------------------------------------------------
    # MJPEG
    # ------------------------------------------------------------------
    def get_last_frame(self) -> bytes | None:
        with self._frame_lock:
            return self._last_frame

    def mjpeg_generator(self):
        boundary = b"--frame\r\n"
        last = None
        while not self._stop_event.is_set():
            frame = self.get_last_frame()
            if frame is None:
                time.sleep(0.1)
                continue
            if frame is last:
                time.sleep(1.0 / self.fps)
                continue
            last = frame
            yield (
                boundary
                + b"Content-Type: image/jpeg\r\n"
                + b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                + frame + b"\r\n"
            )
            time.sleep(1.0 / self.fps)

    def get_status(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "connected": self._connected,
            "last_error": self._last_error,
        }