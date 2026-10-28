"""Operating-system differences in one place: file locks, process groups and trees, liveness, file replacement.

Everything else calls these helpers instead of fcntl, killpg, signals or os.kill(pid, 0) directly, so Linux, macOS
and Windows behave the same. Standard library only (ctypes for the few Windows calls).

Windows notes (exercised by the Windows CI job, not on a developer machine yet):
- Locks use msvcrt.locking on one byte far past the end of the file, so the holder's details written at the start
  of the same file stay readable by others (Windows locks are mandatory: a locked range can't be read).
- os.kill(pid, 0) is not a liveness check there (0 is CTRL_C_EVENT); pid_alive asks the kernel instead.
- Child processes get CREATE_NO_WINDOW (a console program started from a windowless runner would otherwise open a
  console window each time) and their own process group; a tree is killed with the system's taskkill /T.
- A task runner joins a kill-on-close Job Object at start, so every process it starts, and theirs, ends with it.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

WINDOWS = os.name == "nt"

if WINDOWS:
    import msvcrt
    fcntl = None
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
else:
    import fcntl
    msvcrt = None

_LOCK_OFFSET = 1 << 30  # Windows: the locked byte, far past any holder details written at the start of the file


class FileLock:
    """An exclusive lock on a file, across processes. The OS releases it when the holder's process ends, however it
    ends, so a lock can never be left behind stale. Also exclusive between two FileLocks of one process.
    The file itself may carry details about the holder (write_info / read_info); they are information only."""

    def __init__(self, path: str):
        self.path = path
        self.fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def try_acquire(self) -> bool:
        if self.fd is not None:
            return True
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if WINDOWS:
                os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def acquire(self, timeout: float, poll: float = 0.05) -> bool:
        """Wait up to `timeout` seconds for the lock."""
        deadline = time.time() + max(0.0, timeout)
        while not self.try_acquire():
            if time.time() >= deadline:
                return False
            time.sleep(poll)
        return True

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            if WINDOWS:
                os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)

    def write_info(self, data: Dict[str, Any]) -> None:
        """Replace the holder details (only while holding the lock)."""
        if self.fd is None:
            return
        os.ftruncate(self.fd, 0)
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, json.dumps(data).encode("utf-8"))

    def clear_info(self) -> None:
        if self.fd is not None:
            os.ftruncate(self.fd, 0)

    @staticmethod
    def read_info(path: str) -> Dict[str, Any]:
        try:
            with open(path, "rb") as f:
                return json.loads(f.read(65536).decode("utf-8") or "{}")
        except (OSError, ValueError):
            return {}

    def __enter__(self) -> "FileLock":
        if not self.try_acquire():
            raise OSError(f"{self.path} is locked")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def pid_alive(pid: int) -> bool:
    """Whether a process with this id exists. For display and diagnostics only: decisions use FileLock, because a
    pid can be reused by another program."""
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists, it just isn't ours
        code = ctypes.c_ulong()
        try:
            ok = k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def group_kwargs() -> Dict[str, Any]:
    """Popen arguments that give a child its own process group (so its whole tree can be stopped) and, on Windows,
    no console window."""
    if WINDOWS:
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    return {"start_new_session": True}


def spawn_detached(cmd: List[str], **kwargs: Any) -> subprocess.Popen:
    """Start a process that outlives its parent (a task runner started by the chat, the MCP server or the desktop
    app). On Windows it also leaves the parent's job when that job allows it, so closing the parent doesn't end it."""
    if not WINDOWS:
        return subprocess.Popen(cmd, start_new_session=True, **kwargs)
    flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    try:
        return subprocess.Popen(cmd, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:  # the parent's job forbids breaking away
        return subprocess.Popen(cmd, creationflags=flags, **kwargs)


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a child and everything it started. POSIX: TERM to its process group, then KILL. Windows: the system's
    taskkill /T /F (called by absolute path, so nothing on PATH can stand in for it), else the child alone."""
    try:
        if WINDOWS:
            root = os.environ.get("SystemRoot") or r"C:\Windows"
            taskkill = os.path.join(root, "System32", "taskkill.exe")
            try:
                subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15, creationflags=CREATE_NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                pass
            if proc.poll() is None:
                proc.kill()
            return
        import signal
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        time.sleep(0.2)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


_JOB = None  # Windows: the runner's Job Object handle, kept open for the life of the process


def join_kill_on_close_job() -> bool:
    """Windows: put this process in a new Job Object that ends every process in it when this one ends. A task runner
    calls it first thing, before it starts anything, so all its workers and their children belong to the job
    without a race. POSIX: nothing to do (workers run in their own process groups). Returns whether it joined."""
    global _JOB
    if not WINDOWS or _JOB is not None:
        return _JOB is not None
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                      "OtherOperationCount", "ReadTransferCount",
                                                      "WriteTransferCount", "OtherTransferCount")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return False
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or \
            not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        k32.CloseHandle(ctypes.c_void_p(job))
        return False
    _JOB = job
    return True


def replace_file(src: str, dst: str) -> None:
    """os.replace that survives Windows' sharing rules: replacing a file another process has open for reading (the
    chat, the desktop app or a status call reading task.json) fails there for a moment, so it is retried."""
    if not WINDOWS:
        os.replace(src, dst)
        return
    for attempt in range(40):  # about 2 s
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


# Characters cmd.exe interprets in the arguments of a .cmd/.bat program (it re-parses its whole command line), and
# control characters. Escaping them reliably is not possible, so such arguments are refused instead.
_BATCH_UNSAFE = re.compile(r'[&|<>^%!"\x00-\x1f]')


def batch_args_problem(args: List[str]) -> str:
    """Why these arguments can't be passed to a batch-file program, or "" when they can."""
    bad = next((a for a in args if _BATCH_UNSAFE.search(a)), None)
    if bad is None:
        return ""
    return (f"an argument contains characters cmd.exe would interpret ({bad[:60]!r}); install the program's native "
            "executable, or avoid & | < > ^ % ! \" and line breaks in paths and arguments")


def resolve_command(cmd: List[str]) -> "tuple":
    """(command, problem). On Windows a program is found through PATHEXT as a shell would (npm -> npm.cmd;
    CreateProcess alone finds only .exe files), and a .cmd/.bat program gets only arguments cmd.exe can't
    misread. Elsewhere the command is returned unchanged."""
    if not WINDOWS or not cmd:
        return cmd, ""
    exe = cmd[0] if os.path.dirname(cmd[0]) else (shutil.which(cmd[0]) or cmd[0])
    if exe.lower().endswith((".cmd", ".bat")):
        problem = batch_args_problem([str(a) for a in cmd[1:]])
        if problem:
            return cmd, f"refused to run {os.path.basename(exe)}: {problem}"
    return [exe] + list(cmd[1:]), ""


def is_within(path: str, folder: str) -> bool:
    """path is folder itself or inside it. Separators and, on Windows, letter case are normalised, so "C:\\Work\\api"
    is inside "c:/work". Text comparison only: symbolic links are not resolved."""
    p = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    f = os.path.normcase(os.path.normpath(os.path.abspath(folder)))
    return p == f or p.startswith(f.rstrip(os.sep) + os.sep)


def python_command() -> List[str]:
    """The Python that runs Foreman, in UTF-8 mode, for the processes Foreman starts itself (task runners): output,
    logs and file names are UTF-8 whatever the system's code page is."""
    return [sys.executable, "-X", "utf8"]
