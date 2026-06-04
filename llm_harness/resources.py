"""Deterministic host resource probes used by the scheduler and reports."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any


def sample_resources(root: str | Path) -> dict[str, Any]:
    """Collect CPU, RAM, and disk usage without depending on Codex or psutil."""

    load1 = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    cpus = max(1, os.cpu_count() or 1)
    cpu_percent = min(100.0, (load1 / cpus) * 100.0)
    ram_percent = _ram_percent()
    disk = shutil.disk_usage(str(root))
    return {
        "cpu_percent": round(cpu_percent, 2),
        "ram_percent": round(ram_percent, 2),
        "disk_free_gb": round(disk.free / (1024**3), 2),
        "load1": round(load1, 2),
        "processes": _top_processes(),
    }


def _ram_percent() -> float:
    """Estimate RAM pressure on Linux, macOS, and unknown systems."""

    if Path("/proc/meminfo").exists():
        info: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(errors="ignore").splitlines():
            key, _, rest = line.partition(":")
            if rest:
                info[key] = int(rest.strip().split()[0])
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        return 0.0 if total == 0 else ((total - available) / total) * 100.0

    if platform.system() == "Darwin":
        try:
            pagesize = int(subprocess.check_output(["pagesize"], text=True).strip())
            output = subprocess.check_output(["vm_stat"], text=True)
            values: dict[str, int] = {}
            for line in output.splitlines():
                if ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                values[key.strip()] = int(raw.strip().rstrip(".").replace(".", ""))
            free = values.get("Pages free", 0) + values.get("Pages speculative", 0)
            active = values.get("Pages active", 0)
            inactive = values.get("Pages inactive", 0)
            wired = values.get("Pages wired down", 0) or values.get("Pages wired", 0)
            compressed = values.get("Pages occupied by compressor", 0)
            used = active + inactive + wired + compressed
            total = used + free
            # pagesize is intentionally read to ensure vm_stat values are pages;
            # it cancels out in the percentage but catches command failures.
            _ = pagesize
            return 0.0 if total == 0 else (used / total) * 100.0
        except (OSError, subprocess.CalledProcessError, ValueError):
            return 0.0

    return 0.0


def _top_processes() -> list[dict[str, Any]]:
    """Sample a tiny process list so reports can identify runaway Codex sessions."""

    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,pcpu=,pmem=,comm=", "-r"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    processes = []
    for line in output.splitlines()[:5]:
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, cpu, mem, command = parts
        processes.append({"pid": int(pid), "cpu": float(cpu), "mem": float(mem), "command": command})
    return processes
