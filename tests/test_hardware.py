from pathlib import Path
from unittest.mock import patch

from zimantic import hardware


def test_affinity_topology_and_parent_cgroup_quota_limit_workers():
    files = {
        "/sys/devices/system/cpu/cpu0/topology/core_id": "0",
        "/sys/devices/system/cpu/cpu0/topology/physical_package_id": "0",
        "/sys/devices/system/cpu/cpu1/topology/core_id": "0",
        "/sys/devices/system/cpu/cpu1/topology/physical_package_id": "0",
        "/sys/fs/cgroup/parent/cpu.max": "150000 100000",
        "/sys/fs/cgroup/parent/leaf/cpu.max": "max 100000",
    }
    with (
        patch.object(hardware.sys, "platform", "linux"),
        patch.object(
            hardware.os, "sched_getaffinity", return_value={0, 1}, create=True
        ),
        patch.object(
            hardware,
            "_cgroup_paths",
            return_value=[
                Path("/sys/fs/cgroup/parent"),
                Path("/sys/fs/cgroup/parent/leaf"),
            ],
        ),
        patch.object(
            hardware, "_read", side_effect=lambda path: files.get(path.as_posix(), "")
        ),
        patch.object(hardware, "_memory", return_value=(1024, 512)),
    ):
        result = hardware.detect()
    assert result.cores == result.threads == 1


def test_cgroup_memory_exhaustion_is_not_reported_as_half_the_ram():
    files = {
        "/proc/meminfo": "MemTotal: 2097152 kB\nMemAvailable: 1048576 kB",
        "/sys/fs/cgroup/memory.max": "536870912",
        "/sys/fs/cgroup/memory.current": "536870913",
    }
    with (
        patch.object(hardware.sys, "platform", "linux"),
        patch.object(hardware, "_cgroup_paths", return_value=[Path("/sys/fs/cgroup")]),
        patch.object(
            hardware, "_read", side_effect=lambda path: files.get(path.as_posix(), "")
        ),
    ):
        assert hardware._memory() == (536870912, 0)


def test_malformed_cpu_quota_cannot_crash_detection():
    with (
        patch.object(hardware.sys, "platform", "linux"),
        patch.object(
            hardware.os, "sched_getaffinity", return_value={0, 1}, create=True
        ),
        patch.object(hardware, "_read", return_value="oops 0"),
        patch.object(hardware, "_cgroup_paths", return_value=[Path("/sys/fs/cgroup")]),
        patch.object(hardware, "_memory", return_value=(0, 0)),
    ):
        assert hardware.detect().threads == 2


def test_leaf_and_ancestor_cgroups_are_examined():
    with patch.object(hardware, "_read", return_value="0::/parent/leaf"):
        paths = hardware._cgroup_paths()
    assert paths == [
        Path("/sys/fs/cgroup"),
        Path("/sys/fs/cgroup/parent"),
        Path("/sys/fs/cgroup/parent/leaf"),
    ]
