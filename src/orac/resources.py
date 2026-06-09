from __future__ import annotations

import ctypes
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ResourceSnapshot:
    cpu_percent: float | None
    memory_percent: float | None
    memory_total_gb: float | None
    memory_available_gb: float | None
    gpu_percent: float | None
    vram_percent: float | None
    disk_free_gb: float
    busy: bool
    recommended_tier: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def read_resource_snapshot(target_utilization: float = 60.0) -> ResourceSnapshot:
    cpu = _cpu_percent()
    memory, memory_total_gb, memory_available_gb = _memory_snapshot()
    gpu, vram = _nvidia_snapshot()
    disk = shutil.disk_usage(".")
    disk_free_gb = round(disk.free / (1024**3), 2)

    busy_reasons: list[str] = []
    if cpu is not None and cpu > max(target_utilization, 75):
        busy_reasons.append(f"CPU {cpu:.0f}%")
    if memory is not None and memory > 85:
        busy_reasons.append(f"memory {memory:.0f}%")
    if gpu is not None and gpu > max(target_utilization, 70):
        busy_reasons.append(f"GPU {gpu:.0f}%")
    if vram is not None and vram > 85:
        busy_reasons.append(f"VRAM {vram:.0f}%")

    busy = bool(busy_reasons)
    recommended = "small_local" if busy else "local"
    reason = ", ".join(busy_reasons) if busy_reasons else "resources within policy"
    return ResourceSnapshot(
        cpu_percent=cpu,
        memory_percent=memory,
        memory_total_gb=memory_total_gb,
        memory_available_gb=memory_available_gb,
        gpu_percent=gpu,
        vram_percent=vram,
        disk_free_gb=disk_free_gb,
        busy=busy,
        recommended_tier=recommended,
        reason=reason,
    )


def _cpu_percent() -> float | None:
    try:
        import psutil  # type: ignore

        return round(float(psutil.cpu_percent(interval=0.2)), 1)
    except Exception:
        pass
    if sys.platform.startswith("win"):
        return _windows_cpu_percent()
    return None


def _memory_snapshot() -> tuple[float | None, float | None, float | None]:
    try:
        import psutil  # type: ignore

        memory = psutil.virtual_memory()
        return (
            round(float(memory.percent), 1),
            round(float(memory.total) / (1024**3), 2),
            round(float(memory.available) / (1024**3), 2),
        )
    except Exception:
        pass
    if sys.platform.startswith("win"):
        return _windows_memory_snapshot()
    return _linux_memory_snapshot()


def _windows_cpu_percent() -> float | None:
    try:
        completed = subprocess.run(
            ["typeperf", r"\Processor(_Total)\% Processor Time", "-sc", "1"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in completed.stdout.splitlines():
        if '","' not in line:
            continue
        try:
            return round(float(line.rsplit(",", 1)[1].strip().strip('"')), 1)
        except ValueError:
            continue
    return None


def _windows_memory_snapshot() -> tuple[float | None, float | None, float | None]:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except Exception:
        return None, None, None
    if not ok:
        return None, None, None
    return (
        round(float(status.dwMemoryLoad), 1),
        round(float(status.ullTotalPhys) / (1024**3), 2),
        round(float(status.ullAvailPhys) / (1024**3), 2),
    )


def _linux_memory_snapshot() -> tuple[float | None, float | None, float | None]:
    try:
        data = {}
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, value = line.split(":", 1)
                data[key] = float(value.strip().split()[0])
        total = data["MemTotal"]
        available = data["MemAvailable"]
    except (OSError, KeyError, ValueError):
        return None, None, None
    return (
        round((total - available) / total * 100, 1),
        round(total / (1024**2), 2),
        round(available / (1024**2), 2),
    )


def _nvidia_snapshot() -> tuple[float | None, float | None]:
    if shutil.which("nvidia-smi") is None:
        return None, None
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    rows = [row.strip() for row in completed.stdout.splitlines() if row.strip()]
    if not rows:
        return None, None
    try:
        gpu, used, total = [float(part.strip()) for part in rows[0].split(",")]
    except ValueError:
        return None, None
    vram = round(used / total * 100, 1) if total else None
    return round(gpu, 1), vram


def snapshot_json() -> str:
    return json.dumps(read_resource_snapshot().to_dict(), indent=2, sort_keys=True)
