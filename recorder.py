"""
Рекордер — циклическая запись RTSP-потока MediaMTX в MP4 (video + audio).

В отличие от старой версии, не использует OpenCV VideoWriter.
Вместо этого запускает FFmpeg-подпроцесс, который читает raw-путь
из MediaMTX и пишет сегменты в архив.

Аудио попадает в архив автоматически (если оно есть в потоке и включено
в настройках камеры).
"""
import platform
import subprocess
import time
import logging
import threading
from datetime import datetime
from pathlib import Path

import requests

import bootstrap

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"

FFMPEG_BIN = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
FFMPEG_PATH = str(bootstrap.get_path(FFMPEG_BIN)).replace("\\", "/")

RTSP_HOST = "127.0.0.1"
RTSP_PORT = 8554
MEDIAMTX_API = "http://127.0.0.1:9997"


class Recorder:
    def __init__(self, camera, archive_cfg: dict, camera_cfg: dict):
        self.camera = camera
        self.cfg = archive_cfg
        self.camera_cfg = camera_cfg

        self.base_path: Path = Path(archive_cfg["path"]).resolve() / camera.id
        self.base_path.mkdir(parents=True, exist_ok=True)

        self.segment_seconds: int = int(archive_cfg.get("segment_minutes", 5)) * 60
        self.max_size_bytes: int = int(archive_cfg.get("max_size_gb", 50) * (1024 ** 3))
        self.max_age_sec: int = int(archive_cfg.get("max_age_days", 30) * 86400)
        self.cleanup_interval: int = int(archive_cfg.get("cleanup_interval_sec", 60))

        # Аудио в архив: включается, только если камера IP и звук найден
        # и пользователь поставил галочку «Захватывать звук».
        self.include_audio: bool = bool(
            camera_cfg.get("audio", False) and camera_cfg.get("has_audio")
        )

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._stderr_thread: threading.Thread | None = None

        # Статистика
        self.segments_written: int = 0
        self.bytes_written: int = 0
        self.last_cleanup_ts: float = 0.0
        self.last_error: str | None = None
        self.current_file: str | None = None

    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"recorder-{self.camera.id}", daemon=True
        )
        self._thread.start()
        logger.info(
            "[%s] Рекордер запущен (audio=%s) → %s",
            self.camera.id, self.include_audio, self.base_path,
        )

    def stop(self):
        self._stop_event.set()
        # Аккуратно завершаем FFmpeg, чтобы MP4 закрылся корректно
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[%s] Рекордер остановлен", self.camera.id)

    # ------------------------------------------------------------------
    def _run(self):
        raw_name = f"__raw_{self.camera.id}"
        raw_url = f"rtsp://{RTSP_HOST}:{RTSP_PORT}/{raw_name}"

        while not self._stop_event.is_set():
            # --- Ждём готовности raw-потока (до 30 сек) ---
            if not self._wait_raw_ready(raw_name):
                if self._stop_event.is_set():
                    break
                # Не готов — цикл повторится
                continue

            # --- Открываем новый сегмент ---
            seg_path = self._new_segment_path()
            self.current_file = str(seg_path)
            cmd = self._build_ffmpeg_cmd(raw_url, seg_path)

            logger.info("[%s] Новый сегмент: %s",
                        self.camera.id, seg_path.name)
            logger.debug("[%s] FFmpeg cmd: %s", self.camera.id, " ".join(cmd))

            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
                )
            except Exception as e:
                self.last_error = str(e)
                logger.error("[%s] Не удалось запустить FFmpeg: %s",
                             self.camera.id, e)
                time.sleep(5)
                continue

            # --- Читаем stderr FFmpeg в фоне ---
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                args=(self._proc, seg_path.name),
                daemon=True,
                name=f"ffmpeg-stderr-{self.camera.id}",
            )
            self._stderr_thread.start()

            # --- Ждём завершения сегмента (FFmpeg сам завершится через -t) ---
            while not self._stop_event.is_set():
                ret = self._proc.poll()
                if ret is not None:
                    break
                time.sleep(0.5)

            # Если нас остановили — прерываем FFmpeg
            if self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                except Exception:
                    pass

            # Проверим результат
            return_code = self._proc.returncode if self._proc else None
            if seg_path.exists() and seg_path.stat().st_size > 0:
                self.segments_written += 1
                try:
                    self.bytes_written += seg_path.stat().st_size
                except Exception:
                    pass
                logger.info(
                    "[%s] Сегмент завершён: %s (%.1f МБ, код FFmpeg=%s)",
                    self.camera.id, seg_path.name,
                    seg_path.stat().st_size / (1024 * 1024),
                    return_code,
                )
            else:
                self.last_error = "сегмент пуст"
                logger.warning(
                    "[%s] Сегмент пуст или не создан: %s (код FFmpeg=%s)",
                    self.camera.id, seg_path.name, return_code,
                )
                # Подчистим пустышку
                try:
                    if seg_path.exists():
                        seg_path.unlink()
                except Exception:
                    pass

            self._proc = None
            self.current_file = None
            self._maybe_cleanup()

    # ------------------------------------------------------------------
    def _read_stderr(self, proc: subprocess.Popen, seg_name: str):
        """Читает stderr FFmpeg построчно и логирует. Ошибки — как ERROR."""
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                low = line.lower()
                if any(k in low for k in ("error", "fail", "fatal", "panic",
                                           "invalid", "no such", "cannot",
                                           "unable", "denied")):
                    logger.error("[%s][ffmpeg %s] %s",
                                 self.camera.id, seg_name, line)
                elif "warn" in low:
                    logger.warning("[%s][ffmpeg %s] %s",
                                   self.camera.id, seg_name, line)
                else:
                    logger.info("[%s][ffmpeg %s] %s",
                                self.camera.id, seg_name, line)
        except Exception as e:
            logger.debug("[%s] stderr-поток остановлен: %s", self.camera.id, e)

    # ------------------------------------------------------------------
    def _wait_raw_ready(self, raw_name: str, timeout: int = 30) -> bool:
        """Ждёт, пока raw-путь в MediaMTX станет ready."""
        deadline = time.time() + timeout
        warned = False
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            try:
                r = requests.get(
                    f"{MEDIAMTX_API}/v3/paths/get/{raw_name}",
                    timeout=2,
                )
                if r.status_code == 200 and r.json().get("ready"):
                    return True
            except Exception:
                pass
            if not warned and time.time() > deadline - timeout + 5:
                logger.info("[%s] Ожидаю готовности raw-потока %s",
                            self.camera.id, raw_name)
                warned = True
            time.sleep(1)
        logger.warning("[%s] Raw-поток %s не готов за %s сек",
                       self.camera.id, raw_name, timeout)
        return False

    # ------------------------------------------------------------------
    def _build_ffmpeg_cmd(self, url: str, out_path: Path) -> list[str]:
        cmd = [
            FFMPEG_PATH,
            "-nostdin",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", url,
            # Видео копируется без перекодирования
            "-c:v", "copy",
        ]
        if self.include_audio:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-an"]

        cmd += [
            "-t", str(self.segment_seconds),
            "-y",
            str(out_path),
        ]
        return cmd

    def _new_segment_path(self) -> Path:
        now = datetime.now()
        day_dir = self.base_path / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        # Уникальное имя: если два сегмента в одну секунду — добавим счётчик
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
            logger.warning("[%s] Ошибка чистки архива: %s", self.camera.id, e)

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

        # 1) Удаляем по возрасту
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

        # 2) Удаляем по размеру (самое старое — первым)
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

        # Удаляем пустые папки
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
            "frames": 0,                       # оставлено для совместимости UI
            "current_file": self.current_file,
            "archive_size_mb": round(total_size / (1024 * 1024), 1),
            "archive_size_limit_mb": round(self.max_size_bytes / (1024 * 1024), 1),
            "oldest_ts": oldest,
            "last_error": self.last_error,
            "include_audio": self.include_audio,
        }