"""CPU, GPU and RAM load of the Mac for the top bar, with the part this app uses (its server and the ComfyUI process).
App memory is the physical footprint (what Activity Monitor shows as Memory). macOS does not report GPU use per
process without root, so the GPU value is the whole GPU."""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
from typing import Any

import psutil

try:   # lives in the dyld shared cache, so there is no file to check for
    _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
except OSError:
    _libproc = None


class _RusageV2(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size", "resident_size",
        "phys_footprint", "proc_start_abstime", "proc_exit_abstime", "child_user_time", "child_system_time",
        "child_pkg_idle_wkups", "child_interrupt_wkups", "child_pageins", "child_elapsed_abstime",
        "diskio_bytesread", "diskio_byteswritten")]   # the whole rusage_info_v2: the call writes all of it


def footprint(pid: int) -> int:
    """Physical footprint of a process in bytes (falls back to RSS)."""
    if _libproc:
        ru = _RusageV2()
        if _libproc.proc_pid_rusage(pid, 2, ctypes.byref(ru)) == 0:
            return int(ru.phys_footprint)
    try:
        return psutil.Process(pid).memory_info().rss
    except psutil.Error:
        return 0


def gpu_percent() -> int | None:
    """Apple silicon GPU utilisation from the IOAccelerator statistics (no root needed)."""
    try:
        out = subprocess.run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"Device Utilization %"=(\d+)', out)
    return int(m[1]) if m else None


_procs: dict[int, psutil.Process] = {}   # kept between calls: cpu_percent() measures since the previous call


def comfy_pid(port: int) -> int | None:
    for p in psutil.process_iter(["cmdline"]):
        cmd = p.info.get("cmdline") or []
        if any(c.endswith("main.py") for c in cmd) and f"{port}" in cmd:
            return p.pid
    return None


def app_processes(comfy: int | None) -> list[psutil.Process]:
    """This server with its children plus the ComfyUI process (and its children)."""
    roots = [os.getpid()] + ([comfy] if comfy else [])
    pids: set[int] = set()
    for pid in roots:
        try:
            p = psutil.Process(pid)
            pids |= {pid, *(c.pid for c in p.children(recursive=True))}
        except psutil.Error:
            pass
    for gone in set(_procs) - pids:
        _procs.pop(gone)
    out = []
    for pid in pids:
        try:
            out.append(_procs.setdefault(pid, psutil.Process(pid)))
        except psutil.Error:
            pass
    return out


def snapshot(comfy: int | None) -> dict[str, Any]:
    cores = psutil.cpu_count() or 1
    procs = app_processes(comfy)
    app_cpu = 0.0
    for p in procs:
        try:
            app_cpu += p.cpu_percent(None)
        except psutil.Error:
            pass
    vm = psutil.virtual_memory()
    return {
        "cpu": {"total": psutil.cpu_percent(None), "app": min(100.0, app_cpu / cores)},
        "ram": {"total": vm.total, "used": vm.total - vm.available, "app": sum(footprint(p.pid) for p in procs)},
        "gpu": {"total": gpu_percent()},
    }
