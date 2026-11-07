"""What this machine has: CPU cores and threads, RAM, and which device it is (e.g. a Raspberry Pi model).

Standard library only, so it also works on a Pi without extra packages. Every value has a fallback:
if something can't be read on this OS, detection still returns a usable answer instead of failing.
"""
import ctypes
import math
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

GB = 1 << 30


@dataclass(frozen=True)
class Hardware:
    system: str          # "Linux", "Windows", "Darwin"
    machine: str         # "x86_64", "AMD64", "aarch64", ...
    device: str          # e.g. "Raspberry Pi 5 Model B Rev 1.0"; "" if unknown
    cpu: str             # CPU model name; "" if unknown
    threads: int         # hardware threads this process may use (affinity and container limits applied)
    cores: int           # physical cores among them (= threads when there's no hyperthreading)
    ram_total: int       # bytes (container limit applied)
    ram_available: int   # bytes free for new work right now (container limit applied)

    @property
    def hyperthreading(self) -> bool:
        return self.threads > self.cores

    def describe(self) -> str:
        """One line for logs and benchmark tables."""
        name = self.device or self.cpu or self.machine
        return (f"{name}: {self.cores} cores / {self.threads} threads, "
                f"{self.ram_total / GB:.1f} GB RAM ({self.ram_available / GB:.1f} GB free), "
                f"{self.system} {self.machine}")


def detect() -> Hardware:
    threads = _usable_threads()
    total, available = _ram()
    return Hardware(
        system=platform.system(),
        machine=platform.machine(),
        device=_device_model(),
        cpu=_cpu_name(),
        threads=threads,
        cores=max(1, min(threads, _physical_cores() or threads)),
        ram_total=total,
        ram_available=min(available, total),
    )


# --- CPU -------------------------------------------------------------------------------------------

def _usable_threads() -> int:
    """Hardware threads we may run on: os.cpu_count() ignores CPU affinity and container quotas."""
    if hasattr(os, "process_cpu_count"):  # Python 3.13+: respects affinity
        n = os.process_cpu_count() or 1
    elif hasattr(os, "sched_getaffinity"):
        n = len(os.sched_getaffinity(0))
    else:
        n = os.cpu_count() or 1
    quota = _read(Path("/sys/fs/cgroup/cpu.max")).split()  # Docker/Podman (cgroup v2): "200000 100000" = 2 CPUs
    if len(quota) == 2 and quota[0] != "max":
        n = min(n, max(1, math.ceil(int(quota[0]) / int(quota[1]))))
    return max(1, n)


def _physical_cores() -> int | None:
    try:
        if sys.platform.startswith("linux"):
            # each (package, core) pair is one physical core; hyperthread siblings share it
            cores = set()
            for cpu in Path("/sys/devices/system/cpu").glob("cpu[0-9]*"):
                topology = cpu / "topology"
                core = _read(topology / "core_id")
                if core:
                    cores.add((_read(topology / "physical_package_id"), core))
            return len(cores) or None
        if sys.platform == "win32":
            return _windows_physical_cores()
        if sys.platform == "darwin":
            return int(_sysctl("hw.physicalcpu")) or None
    except (OSError, ValueError):
        pass
    return None


def _windows_physical_cores() -> int | None:
    class Info(ctypes.Structure):  # SYSTEM_LOGICAL_PROCESSOR_INFORMATION
        _fields_ = [("mask", ctypes.c_size_t), ("relationship", ctypes.c_int), ("_union", ctypes.c_ulonglong * 2)]

    size = ctypes.c_ulong(0)
    get = ctypes.windll.kernel32.GetLogicalProcessorInformation
    get(None, ctypes.byref(size))  # first call only reports the buffer size
    buffer = (Info * (size.value // ctypes.sizeof(Info)))()
    if not get(buffer, ctypes.byref(size)):
        return None
    return sum(1 for info in buffer if info.relationship == 0) or None  # 0 = RelationProcessorCore


def _cpu_name() -> str:
    try:
        if sys.platform.startswith("linux"):
            for line in _read(Path("/proc/cpuinfo")).splitlines():
                key, _, value = line.partition(":")
                if key.strip() in ("model name", "Model", "Hardware") and value.strip():
                    return value.strip()
        elif sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        elif sys.platform == "darwin":
            return _sysctl("machdep.cpu.brand_string")
    except (OSError, ValueError):
        pass
    return platform.processor()


def _device_model() -> str:
    """Board name on single-board computers, e.g. "Raspberry Pi Zero 2 W Rev 1.0"."""
    return _read(Path("/proc/device-tree/model")).rstrip("\x00")


# --- RAM -------------------------------------------------------------------------------------------

def _ram() -> tuple[int, int]:
    """(total, available) in bytes. Falls back to 1 GB / 512 MB if the OS can't be read."""
    total = available = 0
    try:
        if sys.platform.startswith("linux"):
            info = {}
            for line in _read(Path("/proc/meminfo")).splitlines():
                key, _, value = line.partition(":")
                info[key] = int(value.split()[0]) * 1024  # kB
            total, available = info.get("MemTotal", 0), info.get("MemAvailable", info.get("MemFree", 0))
            limit = _cgroup_memory_limit()
            if limit:
                total, available = min(total, limit[0]), min(available, limit[1])
        elif sys.platform == "win32":
            total, available = _windows_ram()
        elif sys.platform == "darwin":
            total = int(_sysctl("hw.memsize"))
            available = _mac_available()
    except (OSError, ValueError):
        pass
    if not total:
        total = GB
    return total, available or total // 2


def _cgroup_memory_limit() -> tuple[int, int] | None:
    """(limit, limit - used) when running in a container with a memory limit (cgroup v2, then v1)."""
    for limit_file, used_file in (("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
                                  ("/sys/fs/cgroup/memory/memory.limit_in_bytes",
                                   "/sys/fs/cgroup/memory/memory.usage_in_bytes")):
        limit = _read(Path(limit_file))
        if limit.isdigit() and int(limit) < 1 << 60:  # v1 reports a huge number when there is no limit
            used = _read(Path(used_file))
            return int(limit), int(limit) - (int(used) if used.isdigit() else 0)
    return None


def _windows_ram() -> tuple[int, int]:
    class Status(ctypes.Structure):  # MEMORYSTATUSEX
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                    ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                    ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                    ("extended", ctypes.c_ulonglong)]

    status = Status()
    status.length = ctypes.sizeof(Status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0, 0
    return status.total, status.available


def _mac_available() -> int:
    """Free + inactive pages: memory macOS hands out without swapping."""
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    page = int(_sysctl("hw.pagesize") or 4096)
    pages = {}
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if value.strip().rstrip(".").isdigit():
            pages[key] = int(value.strip().rstrip("."))
    return (pages.get("Pages free", 0) + pages.get("Pages inactive", 0)) * page


# --- helpers ---------------------------------------------------------------------------------------

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _sysctl(name: str) -> str:
    return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip()


if __name__ == "__main__":
    print(detect().describe())
