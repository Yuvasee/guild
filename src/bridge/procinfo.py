import ctypes
import ctypes.util
import os
import struct
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

_CTL_KERN = 1
_KERN_ARGMAX = 8
_KERN_PROC = 14
_KERN_PROC_PID = 1
_KERN_PROCARGS2 = 49
_KINFO_PROC_SIZE = 648
_PS_TIMEOUT_S = 5


@dataclass(frozen=True)
class ProcessArgs:
    argv: list[str]
    env: dict[str, str]


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    command: str


def read_process_table() -> list[ProcessRow] | None:
    try:
        done = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=_PS_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    rows: list[ProcessRow] = []
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append(ProcessRow(int(parts[0]), int(parts[1]), parts[2]))
    return rows


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_start_ms(pid: int) -> int | None:
    if sys.platform == "darwin":
        return _darwin_start_ms(pid)
    if sys.platform.startswith("linux"):
        return _linux_start_ms(pid)
    return None


def process_args(pid: int) -> ProcessArgs | None:
    if sys.platform == "darwin":
        return _darwin_args(pid)
    if sys.platform.startswith("linux"):
        return _linux_args(pid)
    return None


def _darwin_start_ms(pid: int) -> int | None:
    raw = _sysctl([_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid], _KINFO_PROC_SIZE)
    if raw is None or len(raw) < 16:
        return None
    # kinfo_proc begins with extern_proc, whose first member is p_starttime (struct timeval).
    seconds, micros = struct.unpack_from("=qi", raw, 0)
    if seconds <= 0:
        return None
    return seconds * 1000 + micros // 1000


def _darwin_args(pid: int) -> ProcessArgs | None:
    argmax_raw = _sysctl([_CTL_KERN, _KERN_ARGMAX], 4)
    if argmax_raw is None:
        return None
    (argmax,) = struct.unpack("=i", argmax_raw[:4])
    raw = _sysctl([_CTL_KERN, _KERN_PROCARGS2, pid], argmax)
    if raw is None or len(raw) < 4:
        return None
    return _parse_procargs2(raw)


def _parse_procargs2(raw: bytes) -> ProcessArgs:
    (argc,) = struct.unpack_from("=i", raw, 0)
    pos = raw.index(b"\0", 4)
    while pos < len(raw) and raw[pos] == 0:
        pos += 1
    strings = raw[pos:].split(b"\0")
    argv = [s.decode(errors="replace") for s in strings[:argc]]
    env: dict[str, str] = {}
    for item in strings[argc:]:
        if not item:
            break
        key, sep, value = item.decode(errors="replace").partition("=")
        if sep:
            env[key] = value
    return ProcessArgs(argv=argv, env=env)


def _sysctl(mib: list[int], size: int) -> bytes | None:
    libc = _libc()
    mib_array = (ctypes.c_int * len(mib))(*mib)
    buffer = ctypes.create_string_buffer(size)
    length = ctypes.c_size_t(size)
    rc = libc.sysctl(mib_array, len(mib), buffer, ctypes.byref(length), None, ctypes.c_size_t(0))
    if rc != 0 or length.value == 0:
        return None
    return buffer.raw[: length.value]


@cache
def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _linux_start_ms(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        boot_line = next(line for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime"))
    except (OSError, StopIteration):
        return None
    fields = stat.rsplit(")", 1)[1].split()
    start_ticks = int(fields[19])
    boot_s = int(boot_line.split()[1])
    return boot_s * 1000 + start_ticks * 1000 // os.sysconf("SC_CLK_TCK")


def _linux_args(pid: int) -> ProcessArgs | None:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    argv = [s.decode(errors="replace") for s in cmdline.split(b"\0") if s]
    env: dict[str, str] = {}
    for item in environ.split(b"\0"):
        key, sep, value = item.decode(errors="replace").partition("=")
        if sep:
            env[key] = value
    return ProcessArgs(argv=argv, env=env)
