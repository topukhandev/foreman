"""Small shared helpers: atomic file IO, subprocess execution with process-group timeouts, git snapshots."""

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tmp_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))


def read_text(path: str, limit: int = 200_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def short(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Return the last JSON object in free text (fenced block preferred), or None."""
    if not text:
        return None
    for block in reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)):
        try:
            return json.loads(block)
        except ValueError:
            continue
    # Fall back to the last balanced {...} span.
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            if text[i] == "}":
                depth += 1
            elif text[i] == "{":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:end + 1])
                    except ValueError:
                        break
        end = text.rfind("}", 0, end)
    return None


# Kill callbacks for every live worker/check process, so stopping a task stops its children too
# (workers run in their own process groups and would otherwise survive the orchestrator).
_LIVE: Dict[int, Any] = {}
_LIVE_LOCK = threading.Lock()


def register_kill(fn) -> int:
    with _LIVE_LOCK:
        key = id(fn)
        _LIVE[key] = fn
        return key


def unregister_kill(key: int) -> None:
    with _LIVE_LOCK:
        _LIVE.pop(key, None)


STOPPING = threading.Event()  # set once the task is being stopped: no new processes may start


def kill_all_children() -> None:
    STOPPING.set()
    with _LIVE_LOCK:
        fns = list(_LIVE.values())
    for fn in fns:
        try:
            fn()
        except Exception:
            pass


@dataclass
class ProcResult:
    code: int
    out: str
    err: str
    timed_out: bool
    duration: float


def run_process(cmd: List[str], cwd: str, timeout: int, env: Optional[Dict[str, str]] = None,
                shell: bool = False) -> ProcResult:
    """Run a command in its own process group; kill the whole group on timeout."""
    start = time.time()
    if STOPPING.is_set():
        return ProcResult(-1, "", "stopped by user", False, 0.0)
    kwargs: Dict[str, Any] = dict(cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, text=True, errors="replace", env=env, shell=shell)
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except (OSError, ValueError) as e:
        return ProcResult(-1, "", f"failed to start: {e}", False, 0.0)
    key = register_kill(lambda: _kill_group(proc))
    try:
        out, err = proc.communicate(timeout=timeout)
        return ProcResult(proc.returncode, out or "", err or "", False, time.time() - start)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, err = proc.communicate()
        return ProcResult(-1, out or "", (err or "") + f"\ntimed out after {timeout}s", True, time.time() - start)
    finally:
        unregister_kill(key)


def run_streaming(cmd: List[str], cwd: str, timeout: int, on_line, env: Optional[Dict[str, str]] = None) -> ProcResult:
    """Like run_process, but hands each stdout line to on_line as it arrives (for live activity)."""
    start = time.time()
    if STOPPING.is_set():
        return ProcResult(-1, "", "stopped by user", False, 0.0)
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", env=env, start_new_session=(os.name != "nt"))
    except (OSError, ValueError) as e:
        return ProcResult(-1, "", f"failed to start: {e}", False, 0.0)
    key = register_kill(lambda: _kill_group(proc))
    err_chunks: List[str] = []
    t_err = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    t_err.start()
    out_lines: List[str] = []
    timed_out = threading.Event()
    timer = threading.Timer(timeout, lambda: (timed_out.set(), _kill_group(proc)))
    timer.start()
    try:
        for line in proc.stdout:
            out_lines.append(line)
            try:
                on_line(line)
            except Exception:
                pass
        proc.wait()
    finally:
        timer.cancel()
        unregister_kill(key)
    t_err.join(2)
    err = "".join(err_chunks) + (f"\ntimed out after {timeout}s" if timed_out.is_set() else "")
    return ProcResult(-1 if timed_out.is_set() else proc.returncode, "".join(out_lines), err, timed_out.is_set(),
                      time.time() - start)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if os.name != "nt":
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(0.2)
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


# ---------------------------------------------------------------- git snapshots

def _git(path: str, *args: str) -> Optional[str]:
    res = run_process(["git", "-C", path, *args], cwd=path, timeout=30)
    return res.out if res.code == 0 else None


def is_git_repo(path: str) -> bool:
    return _git(path, "rev-parse", "--is-inside-work-tree") is not None


def git_head(path: str) -> Optional[str]:
    out = _git(path, "rev-parse", "HEAD")
    return out.strip() if out else None


def _file_hash(path: str) -> Optional[str]:
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def snapshot(path: str) -> Dict[str, Any]:
    """Record HEAD plus a hash of every dirty/untracked file so later diffs isolate this task's changes."""
    if not is_git_repo(path):
        return {"git": False}
    status = _git(path, "status", "--porcelain", "-uall") or ""
    files = {}
    for line in status.splitlines():
        rel = line[3:].split(" -> ")[-1].strip().strip('"')
        if rel.startswith(".foreman/"):  # Foreman's own project knowledge, never a worker's change
            continue
        files[rel] = _file_hash(os.path.join(path, rel))
    return {"git": True, "head": git_head(path), "dirty": files}


def changed_since(path: str, base: Dict[str, Any]) -> List[str]:
    """Files whose content differs from the snapshot (ignores pre-existing, untouched dirty files)."""
    if not base.get("git"):
        return []
    now = snapshot(path)
    before, after = base.get("dirty", {}), now.get("dirty", {})
    changed = [f for f, h in after.items() if f not in before or before[f] != h]
    changed += [f for f in before if f not in after]  # reverted to HEAD
    if now.get("head") != base.get("head") and base.get("head"):
        committed = _git(path, "diff", "--name-only", base["head"], now["head"] or "HEAD") or ""
        changed += [f for f in committed.splitlines() if f]
    return sorted(set(changed))


def diff_text(path: str, files: List[str], limit: int = 15000) -> str:
    """Unified diff of the given files against HEAD, including untracked files, truncated."""
    if not files:
        return ""
    parts = []
    tracked = _git(path, "diff", "HEAD", "--", *files) or ""
    parts.append(tracked)
    untracked = set((_git(path, "ls-files", "--others", "--exclude-standard") or "").splitlines())
    for f in files:
        if f in untracked:
            parts.append(f"--- /dev/null\n+++ b/{f}\n" + "".join("+" + l for l in read_text(os.path.join(path, f), 8000).splitlines(True)))
    text = "\n".join(p for p in parts if p)
    return text if len(text) <= limit else text[:limit] + f"\n… [diff truncated at {limit} chars]"
