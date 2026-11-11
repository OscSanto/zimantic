"""Best-effort CPU and memory budgets for index builds and serving."""

import ctypes
import math
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Hardware:
    cores: int
    threads: int
    ram_total: int
    ram_available: int

    def describe(self) -> str:
        return (
            f"{self.cores} cores / {self.threads} usable threads, "
            f"{self.ram_total / (1 << 30):.1f} GiB RAM "
            f"({self.ram_available / (1 << 30):.1f} GiB available), "
            f"{platform.system()} {platform.machine()}"
        )


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _cgroup_paths():
    """Include parent limits, not just the container's leaf cgroup."""
    root = Path("/sys/fs/cgroup")
    paths = {root}
    for line in _read(Path("/proc/self/cgroup")).splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        hierarchy, controllers, relative = parts
        if hierarchy == "0" and not controllers:
            leaf = root / relative.lstrip("/")
            if ".." not in leaf.parts:
                paths.update(
                    path for path in (leaf, *leaf.parents) if path.is_relative_to(root)
                )
        else:
            for controller in {controllers, *controllers.split(",")}:
                base = root / controller
                leaf = base / relative.lstrip("/")
                if ".." not in leaf.parts:
                    paths.add(base)
                    paths.update(
                        path
                        for path in (leaf, *leaf.parents)
                        if path.is_relative_to(base)
                    )
    return sorted(paths)


def _memory() -> tuple[int, int]:
    total = available = 0
    try:
        if sys.platform.startswith("linux"):
            info = {}
            for line in _read(Path("/proc/meminfo")).splitlines():
                name, _, value = line.partition(":")
                if value.split():
                    info[name] = int(value.split()[0]) * 1024
            total = info.get("MemTotal", 0)
            available = info.get("MemAvailable", info.get("MemFree", 0))
        elif os.name == "nt":

            class Status(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("load", ctypes.c_ulong),
                    *[
                        (name, ctypes.c_ulonglong)
                        for name in (
                            "total",
                            "available",
                            "page_total",
                            "page_available",
                            "virtual_total",
                            "virtual_available",
                            "extended",
                        )
                    ],
                ]

            status = Status(length=ctypes.sizeof(Status))
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total, available = status.total, status.available
        elif sys.platform == "darwin":
            total = int(_sysctl("hw.memsize"))
            page_size = int(_sysctl("hw.pagesize"))
            stats = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=5, check=True
            ).stdout
            for line in stats.splitlines():
                name, _, value = line.partition(":")
                if name in {"Pages free", "Pages inactive"}:
                    available += int(value.strip().rstrip(".")) * page_size
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        pass
    if sys.platform.startswith("linux"):
        for path in _cgroup_paths():
            for limit_name, used_name in (
                ("memory.max", "memory.current"),
                ("memory.limit_in_bytes", "memory.usage_in_bytes"),
            ):
                limit, used = _read(path / limit_name), _read(path / used_name)
                if limit.isdigit() and 0 < int(limit) < 1 << 60:
                    cap = int(limit)
                    total = min(total, cap) if total else cap
                    # An unreadable usage counter is not evidence of free RAM.
                    spare = max(0, cap - int(used)) if used.isdigit() else 0
                    available = min(available, spare)
    return max(0, total), max(0, min(available, total))


def _sysctl(name: str) -> str:
    return subprocess.run(
        ["sysctl", "-n", name], capture_output=True, text=True, timeout=5, check=True
    ).stdout.strip()


def detect() -> Hardware:
    try:
        affinity = set(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = set(range(os.cpu_count() or 1))
    threads = max(1, len(affinity))
    cores = threads
    if sys.platform.startswith("linux"):
        physical = set()
        for cpu in affinity:
            topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
            core, package = (
                _read(topology / "core_id"),
                _read(topology / "physical_package_id"),
            )
            if core and package:
                physical.add((package, core))
        if physical:
            cores = len(physical)
        for path in _cgroup_paths():
            quota = _read(path / "cpu.max").split()
            if len(quota) != 2 or quota[0] == "max":
                quota = [
                    _read(path / "cpu.cfs_quota_us"),
                    _read(path / "cpu.cfs_period_us"),
                ]
            try:
                amount, period = map(int, quota)
                if amount > 0 and period > 0:
                    threads = min(threads, max(1, math.floor(amount / period)))
            except ValueError:
                pass
    elif sys.platform == "darwin":
        try:
            cores = int(_sysctl("hw.physicalcpu"))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    total, available = _memory()
    return Hardware(max(1, min(cores, threads)), threads, total, available)
