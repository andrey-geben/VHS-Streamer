"""
Мониторинг системы для VHS-Streamer: CPU, RAM, диски, GPU.

Стратегия:
    * CPU / RAM / диски — psutil (быстро, миллисекунды).
    * GPU — PowerShell + WMI (Win32_VideoController, GPUEngine).
      Работает для Intel iGPU / AMD / NVIDIA, без пакета wmi.
      Медленно (~3–7 сек на первом «холодном» вызове), поэтому
      вынесено в фоновый поток: get_system_stats() читает готовый
      кэш и никогда не блокирует Flask-запрос.

Публичная функция:
    get_system_stats() -> dict

Ограничения (честно):
    * VRAM через WMI AdapterRAM обрезается до 4 ГБ для GPU с бо́льшим
      объёмом — известное ограничение WMI.
    * Загрузка GPU (GPUEngine) на некоторых конфигурациях Intel
      возвращает 0 всегда — WMI-счётчики не всегда инициализируются.
      В этом случае utilization_percent = None, в UI покажем «—».
    * Несколько GPU — суммируем загрузку по всем, делим поровну
      (точное разделение через LUID — отдельная задача).
"""
import json
import logging
import platform
import subprocess
import threading
import time

import psutil

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"

# --- Кэш GPU: обновляется фоновым потоком ---
_GPU_REFRESH_INTERVAL = 5.0        # 5 сек — WMI медленный, чаще нет смысла
_GPU_FIRST_DELAY = 0.5             # почти сразу после старта
_gpu_state = {"data": [], "ts": 0.0, "error": None}
_gpu_lock = threading.Lock()
_gpu_thread_started = False


# ======================================================================
# CPU / RAM / диски — через psutil (быстро)
# ======================================================================
def _get_cpu_stats() -> dict:
    try:
        pct = psutil.cpu_percent(interval=None)
        cores_logical = psutil.cpu_count(logical=True) or 1
        cores_physical = psutil.cpu_count(logical=False) or cores_logical
        freq_mhz = None
        try:
            f = psutil.cpu_freq()
            if f:
                freq_mhz = int(f.current)
        except Exception:
            pass
        return {
            "percent": round(pct, 1) if pct is not None else None,
            "cores_logical": cores_logical,
            "cores_physical": cores_physical,
            "freq_mhz": freq_mhz,
        }
    except Exception as e:
        logger.warning("cpu stats: %s", e)
        return {"percent": None, "cores_logical": None,
                "cores_physical": None, "freq_mhz": None}


def _get_ram_stats() -> dict:
    try:
        vm = psutil.virtual_memory()
        return {
            "total_mb": round(vm.total / 1024 / 1024, 1),
            "used_mb": round((vm.total - vm.available) / 1024 / 1024, 1),
            "available_mb": round(vm.available / 1024 / 1024, 1),
            "percent": round(vm.percent, 1),
        }
    except Exception as e:
        logger.warning("ram stats: %s", e)
        return {"total_mb": None, "used_mb": None,
                "available_mb": None, "percent": None}


def _get_disk_stats() -> list:
    result = []
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception as e:
        logger.warning("disk_partitions: %s", e)
        return result

    seen = set()
    for p in parts:
        opts = (p.opts or "").lower()
        if "cdrom" in opts:
            continue
        mount = p.mountpoint
        if mount in seen:
            continue
        seen.add(mount)
        try:
            usage = psutil.disk_usage(mount)
        except (PermissionError, OSError):
            continue
        result.append({
            "mount": mount,
            "label": (p.device or mount).rstrip("\\/"),
            "fstype": p.fstype or "",
            "total_gb": round(usage.total / 1024 ** 3, 1),
            "used_gb": round(usage.used / 1024 ** 3, 1),
            "free_gb": round(usage.free / 1024 ** 3, 1),
            "percent": round(usage.percent, 1),
        })
    return result


# ======================================================================
# GPU — через PowerShell + WMI, с JSON-парсингом
# ======================================================================
def _run_powershell(script: str, timeout: int = 15) -> str:
    """
    Запуск PowerShell-скрипта с явной UTF-8 кодировкой вывода.
    Возвращает stdout (может быть пустым) — без исключений наружу.
    """
    # Force UTF-8 output + bypass execution policy
    wrapped = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$ErrorActionPreference = 'SilentlyContinue'; "
        + script
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass",
             "-Command", wrapped],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            logger.warning(
                "powershell rc=%s stderr=%r",
                r.returncode, (r.stderr or "")[:300],
            )
            return ""
        return r.stdout or ""
    except subprocess.TimeoutExpired:
        logger.warning("powershell timeout (>%ss)", timeout)
        return ""
    except FileNotFoundError:
        logger.warning("powershell не найден в PATH")
        return ""
    except Exception as e:
        logger.warning("powershell error: %s", e)
        return ""


def _get_gpu_stats_uncached() -> list:
    """
    GPU через WMI. Медленно — вызывать только из фонового потока.
    Возвращает список GPU: [{name, vram_mb, utilization_percent}].
    """
    if not IS_WINDOWS:
        return []

    # 1) Имя + VRAM через Win32_VideoController → JSON
    ps_name = (
        "$gpus = Get-CimInstance Win32_VideoController; "
        "if (-not $gpus) { Write-Output '[]'; exit 0 }; "
        "$out = @(); "
        "foreach ($g in $gpus) { "
        "  $vram = 0; "
        "  try { if ($g.AdapterRAM) { $vram = [int64]$g.AdapterRAM } } "
        "  catch { $vram = 0 }; "
        "  $out += [PSCustomObject]@{ "
        "    name = [string]$g.Name; "
        "    vram = [int64]$vram "
        "  } "
        "}; "
        "$out | ConvertTo-Json -Compress -Depth 3"
    )
    out = _run_powershell(ps_name, timeout=20)
    if not out.strip():
        logger.warning("gpu: пустой ответ от PowerShell (Win32_VideoController)")
        return []

    try:
        data = json.loads(out.strip())
    except json.JSONDecodeError as e:
        logger.warning("gpu json parse error: %s; out=%r", e, out[:300])
        return []

    # ConvertTo-Json возвращает dict для одного элемента, list для массива
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        logger.warning("gpu: json не list и не dict, а %s", type(data).__name__)
        return []

    gpus = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        vram_bytes = item.get("vram") or 0
        try:
            vram_bytes = int(vram_bytes)
        except (TypeError, ValueError):
            vram_bytes = 0
        if not name:
            continue
        gpus.append({
            "name": name,
            "vram_mb": round(vram_bytes / 1024 / 1024, 1) if vram_bytes else 0,
            "utilization_percent": None,
        })

    if not gpus:
        return []

    # 2) Загрузка через GPUEngine (сумма по 3D-движкам) — JSON
    ps_util = (
        "$e = Get-CimInstance "
        "Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine; "
        "if (-not $e) { Write-Output '{\"sum\":0,\"count\":0}'; exit 0 }; "
        "$filtered = $e | Where-Object { $_.Name -like '*engtype_3D*' }; "
        "$sum = ($filtered | Measure-Object -Property UtilizationPercentage "
        "  -Sum).Sum; "
        "if ($sum -eq $null) { $sum = 0 }; "
        "$cnt = @($filtered).Count; "
        "[PSCustomObject]@{ sum = [double]$sum; count = [int]$cnt } | "
        "ConvertTo-Json -Compress"
    )
    util_out = _run_powershell(ps_util, timeout=20)
    util_total = None
    util_count = 0
    if util_out.strip():
        try:
            udata = json.loads(util_out.strip())
            if isinstance(udata, dict):
                util_total = float(udata.get("sum") or 0)
                util_count = int(udata.get("count") or 0)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.debug("gpu util json parse error: %s; out=%r",
                         e, util_out[:200])

    if util_total is not None and util_total > 0:
        per_gpu = util_total / len(gpus)
        for g in gpus:
            g["utilization_percent"] = min(round(per_gpu, 1), 100.0)
    else:
        # util_total == 0 или None → оставляем None (UI покажет «—»),
        # не «0%» (0% вводило бы в заблуждение — «GPU спит» vs «нет данных»)
        for g in gpus:
            g["utilization_percent"] = None

    return gpus


def _gpu_refresh_loop():
    """Фоновый поток: обновляет GPU-статистику каждые N секунд."""
    time.sleep(_GPU_FIRST_DELAY)
    while True:
        try:
            t0 = time.time()
            data = _get_gpu_stats_uncached()
            elapsed = time.time() - t0
            with _gpu_lock:
                _gpu_state["data"] = data
                _gpu_state["ts"] = time.time()
                _gpu_state["error"] = None
            logger.info(
                "system_monitor: GPU обновлён за %.1f сек, найдено %d",
                elapsed, len(data),
            )
        except Exception as e:
            logger.exception("gpu refresh error: %s", e)
            with _gpu_lock:
                _gpu_state["error"] = str(e)
        time.sleep(_GPU_REFRESH_INTERVAL)


def _ensure_gpu_thread():
    global _gpu_thread_started
    if _gpu_thread_started:
        return
    _gpu_thread_started = True
    t = threading.Thread(
        target=_gpu_refresh_loop,
        name="gpu-monitor", daemon=True,
    )
    t.start()
    logger.info("system_monitor: фоновый поток GPU-мониторинга запущен")


def _get_gpu_stats() -> list:
    """Возврат кэша GPU (мгновенно, без PowerShell)."""
    _ensure_gpu_thread()
    with _gpu_lock:
        return list(_gpu_state["data"])


# ======================================================================
# Публичное API
# ======================================================================
def get_system_stats() -> dict:
    """Текущее состояние системы (быстрый вызов, GPU — из кэша)."""
    _ensure_gpu_thread()
    with _gpu_lock:
        gpu_ready = _gpu_state["ts"] > 0.0
        gpu_error = _gpu_state["error"]
    return {
        "cpu": _get_cpu_stats(),
        "ram": _get_ram_stats(),
        "disks": _get_disk_stats(),
        "gpu": _get_gpu_stats(),
        "gpu_ready": gpu_ready,      # true → фоновый поток уже отработал
        "gpu_error": gpu_error,      # текст ошибки (если была)
        "platform": platform.system(),
        "ts": time.time(),
    }


# ======================================================================
# Проверка из консоли:  python system_monitor.py
# ======================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("Первый опрос (GPU — возможно, ещё пусто)...")
    s = get_system_stats()
    print(json.dumps(s, indent=2, ensure_ascii=False))

    print("\nЖдём готовности GPU (до 30 сек, проверяем каждые 3 сек):")
    for i in range(10):
        time.sleep(3)
        s = get_system_stats()
        if s["gpu_ready"]:
            print(f"\n✓ GPU готов через {(i + 1) * 3} сек:")
            print(json.dumps(s, indent=2, ensure_ascii=False))
            break
        print(f"  [{(i + 1) * 3}s] ждём... (gpu_ready={s['gpu_ready']})")
    else:
        print("\n⚠️ GPU так и не появился за 30 секунд.")
        print("Диагностика нужна. Пришлите вывод выше целиком.")
        print(json.dumps(s, indent=2, ensure_ascii=False))