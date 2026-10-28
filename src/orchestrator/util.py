"""Small shared helpers: atomic file IO, subprocess execution with process-group timeouts, git snapshots."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.oscompat import group_kwargs, kill_tree, replace_file, resolve_command


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def atomic_write(path: str, text: str) -> None:
    """Write a whole file so readers see the old or the new content, never a mix, even if the process dies midway.
    No fsync: it costs 6-12 ms per write (measured) and only adds safety against power loss; a record damaged by
    one is reported as unreadable, not silently dropped."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        replace_file(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))


def update_json(path: str, change, default: Any = None, timeout: float = 10.0) -> Any:
    """Read, change and write a JSON file shared by several Foreman processes (suggestions, provider health, the daily
    budget) under a cross-process lock, so two processes never lose each other's update. `change(data)` edits data in
    place or returns the new value; the value written is returned."""
    from orchestrator.oscompat import FileLock
    lock = FileLock(path + ".lock")
    if not lock.acquire(timeout):
        raise TimeoutError(f"{path} stayed locked for {timeout:.0f}s")
    try:
        data = read_json(path, default)
        new = change(data)
        data = data if new is None else new
        write_json(path, data)
        return data
    finally:
        lock.release()


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
                shell: bool = False, stoppable: bool = True) -> ProcResult:
    """Run a command in its own process group; kill the whole group on timeout. Once the task is being stopped, no
    new stoppable process starts (workers, checks); Foreman's own short git reads (stoppable=False) still run, so the
    record of what changed stays right."""
    start = time.time()
    if stoppable and STOPPING.is_set():
        return ProcResult(-1, "", "stopped by user", False, 0.0)
    if not shell:
        cmd, problem = resolve_command(cmd)
        if problem:
            return ProcResult(-1, "", problem, False, 0.0)
    kwargs: Dict[str, Any] = dict(cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                  text=True, encoding="utf-8", errors="replace", env=env, shell=shell, **group_kwargs())
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except (OSError, ValueError) as e:
        return ProcResult(-1, "", f"failed to start: {e}", False, 0.0)
    key = register_kill(lambda: kill_tree(proc))
    try:
        out, err = proc.communicate(timeout=timeout)
        return ProcResult(proc.returncode, out or "", err or "", False, time.time() - start)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        return ProcResult(-1, out or "", (err or "") + f"\ntimed out after {timeout}s", True, time.time() - start)
    finally:
        unregister_kill(key)


def run_streaming(cmd: List[str], cwd: str, timeout: int, on_line, env: Optional[Dict[str, str]] = None) -> ProcResult:
    """Like run_process, but hands each stdout line to on_line as it arrives (for live activity)."""
    start = time.time()
    if STOPPING.is_set():
        return ProcResult(-1, "", "stopped by user", False, 0.0)
    cmd, problem = resolve_command(cmd)
    if problem:
        return ProcResult(-1, "", problem, False, 0.0)
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", env=env, **group_kwargs())
    except (OSError, ValueError) as e:
        return ProcResult(-1, "", f"failed to start: {e}", False, 0.0)
    key = register_kill(lambda: kill_tree(proc))
    err_chunks: List[str] = []
    t_err = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    t_err.start()
    out_lines: List[str] = []
    timed_out = threading.Event()
    timer = threading.Timer(timeout, lambda: (timed_out.set(), kill_tree(proc)))
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
    for pipe in (proc.stdout, proc.stderr):
        try:
            pipe.close()
        except OSError:
            pass
    err = "".join(err_chunks) + (f"\ntimed out after {timeout}s" if timed_out.is_set() else "")
    return ProcResult(-1 if timed_out.is_set() else proc.returncode, "".join(out_lines), err, timed_out.is_set(),
                      time.time() - start)


# ---------------------------------------------------------------- git snapshots

def _git(path: str, *args: str) -> Optional[str]:
    res = run_process(["git", "-C", path, *args], cwd=path, timeout=30, stoppable=False)
    return res.out if res.code == 0 else None


# HEAD and the repository check are read from the .git files when they are plain (the usual case): Foreman asks for
# them many times per step and per prompt, and starting a git process for each costs ~5 ms on Linux and ten times that
# on Windows. Anything unusual (a project in a subfolder of a repository, the reftable format, a missing ref) is
# asked of git itself, so the answer is always git's.
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _git_dir(path: str) -> Optional[str]:
    """The git directory of a repository whose top level is `path` (.git, or the folder a worktree's .git file names)."""
    dot = os.path.join(path, ".git")
    if os.path.isdir(dot):
        return dot
    if os.path.isfile(dot):
        text = read_text(dot, 4096).strip()
        if text.startswith("gitdir:"):
            gitdir = os.path.normpath(os.path.join(path, text[7:].strip()))
            return gitdir if os.path.isdir(gitdir) else None
    return None


def _head_from_files(gitdir: str) -> Optional[str]:
    if os.path.isdir(os.path.join(gitdir, "reftable")):
        return None
    head = read_text(os.path.join(gitdir, "HEAD"), 4096).strip()
    if _SHA_RE.fullmatch(head):
        return head  # detached
    if not head.startswith("ref:"):
        return None
    ref = head[4:].strip()
    common = read_text(os.path.join(gitdir, "commondir"), 4096).strip()
    bases = [gitdir] + ([os.path.normpath(os.path.join(gitdir, common))] if common else [])
    for base in bases:
        value = read_text(os.path.join(base, *ref.split("/")), 4096).strip()
        if _SHA_RE.fullmatch(value):
            return value
    for base in bases:
        for line in read_text(os.path.join(base, "packed-refs")).splitlines():
            sha, _, name = line.partition(" ")
            if name.strip() == ref and _SHA_RE.fullmatch(sha):
                return sha
    return None


def is_git_repo(path: str) -> bool:
    if _git_dir(path):
        return True
    return _git(path, "rev-parse", "--is-inside-work-tree") is not None


def git_head(path: str) -> Optional[str]:
    gitdir = _git_dir(path)
    head = _head_from_files(gitdir) if gitdir else None
    if head:
        return head
    out = _git(path, "rev-parse", "HEAD")
    return out.strip() if out else None


def review_files(path: str, kind: str, base: str = "main") -> Tuple[List[str], Optional[str]]:
    """Files to review that Foreman didn't change: your uncommitted ones, or everything on this branch since it left
    `base` (committed or not). Returns (files, the commit to diff against; None means HEAD)."""
    snap = snapshot(path)
    dirty = sorted(snap.get("dirty") or {})
    if kind != "branch":
        return dirty, None
    mb = (_git(path, "merge-base", base, "HEAD") or "").strip()
    if not mb:
        return dirty, None
    committed = [f for f in (_git(path, "diff", "--name-only", mb) or "").splitlines() if f and not f.startswith(".foreman/")]
    return sorted(set(committed) | set(dirty)), mb


# Files in a project's .foreman folder that steer Foreman (trusted check commands, decisions, mode and role files).
TRUST_FILES_RE = re.compile(r"^(checks\.yaml|decisions\.md|knowledge\.md|modes/.*|roles/.*)$")


def trust_files(path: str) -> Dict[str, Optional[str]]:
    """Hashes of the .foreman files Foreman trusts. Only Foreman writes there; a worker changing them is a violation."""
    root = os.path.join(path, ".foreman")
    out: Dict[str, Optional[str]] = {}
    for d, _, files in os.walk(root):
        for f in files:
            rel = os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
            if TRUST_FILES_RE.match(rel):
                out[rel] = file_hash(os.path.join(d, f))
    return out


def remote_refs(path: str) -> Dict[str, str]:
    """Remote-tracking branches and their commits. A push (or fetch) during a step changes them."""
    out = _git(path, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes") or ""
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


def file_hash(path: str) -> Optional[str]:
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
        files[rel] = file_hash(os.path.join(path, rel))
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


def diff_text(path: str, files: List[str], limit: int = 15000, base: Optional[str] = None) -> str:
    """Unified diff of the given files against HEAD (or against `base`, a commit), including untracked files,
    truncated."""
    if not files:
        return ""
    parts = []
    tracked = _git(path, "diff", base or "HEAD", "--", *files) or ""
    parts.append(tracked)
    untracked = set((_git(path, "ls-files", "--others", "--exclude-standard") or "").splitlines())
    for f in files:
        if f in untracked:
            parts.append(f"--- /dev/null\n+++ b/{f}\n" + "".join("+" + l for l in read_text(os.path.join(path, f), 8000).splitlines(True)))
    text = "\n".join(p for p in parts if p)
    return text if len(text) <= limit else text[:limit] + f"\n… [diff truncated at {limit} chars]"


_TYPES = {"string": str, "array": list, "object": dict, "boolean": bool, "integer": int, "number": (int, float)}


def check_result(data: Any, schema: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Validate an AI result against its JSON schema (the subset Foreman's schemas use: type, properties, required,
    items, enum). A wrong top level (not an object, a required field missing, a field of the wrong type, a value
    outside its enum) is fatal: returns (None, problems). Inside lists, items of the wrong type (or with a field of the
    wrong type) are dropped and named in the problems; missing item fields get the callers' defaults, and fields the
    schema doesn't know are kept."""
    if not isinstance(data, dict):
        return None, ["the result is not a JSON object"]
    fatal = [f"'{k}' is missing" for k in schema.get("required", []) if k not in data or data[k] is None]
    clean, notes = dict(data), []
    for key, sub in (schema.get("properties") or {}).items():
        if key not in data or data[key] is None:
            continue
        problem = _type_problem(data[key], sub)
        if problem:
            fatal.append(f"'{key}' {problem}")
        elif sub.get("type") == "array" and isinstance(sub.get("items"), dict):
            kept = [x for x in data[key] if not _item_problem(x, sub["items"])]
            if len(kept) != len(data[key]):
                notes.append(f"dropped {len(data[key]) - len(kept)} malformed item(s) in '{key}'")
            clean[key] = kept
    if fatal:
        return None, fatal
    return clean, notes


def _type_problem(value: Any, sub: Dict[str, Any]) -> str:
    want = _TYPES.get(sub.get("type", ""))
    if want and (not isinstance(value, want) or (sub.get("type") in ("integer", "number") and isinstance(value, bool))):
        return f"should be {sub['type']}"
    if sub.get("enum") and value not in sub["enum"]:
        return f"is {value!r}, not one of {', '.join(map(str, sub['enum']))}"
    return ""


def _item_problem(item: Any, sub: Dict[str, Any]) -> str:
    problem = _type_problem(item, sub)
    if problem or sub.get("type") != "object":
        return problem
    for k, s in (sub.get("properties") or {}).items():  # nested values: types only (an odd enum value is tolerated)
        if item.get(k) is not None and s.get("type") in ("string", "boolean", "integer", "number") and \
                _type_problem(item[k], {"type": s["type"]}):
            return f"'{k}' should be {s['type']}"
    return ""


# Environment variables AI workers don't get: anything that looks like a secret, plus access the worker has no use
# for (the ssh agent, which would let it push; the parent Claude Code session's messaging channel). A provider keeps
# only the exact names its CLI needs to log in (Provider.env_keep, plus providers.<name>.env_keep in config.yaml).
SECRET_ENV_RE = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|_PAT$|PRIVATE|COOKIE|AUTH|MESSAGING|_PWD$|DATABASE_URL|"
                           r"_DSN$|_URI$|CONNECTION_STRING", re.I)


def worker_env(keep: Any = (), environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    keep = set(keep or ())
    return {k: v for k, v in env.items() if k in keep or not SECRET_ENV_RE.search(k)}


def secret_values(environ: Optional[Dict[str, str]] = None) -> List[Tuple[str, str]]:
    """(name, value) of the secret-looking variables in the environment, longest value first (for redaction)."""
    env = os.environ if environ is None else environ
    found = [(k, v) for k, v in env.items() if SECRET_ENV_RE.search(k) and len(v or "") >= 8]
    return sorted(found, key=lambda kv: -len(kv[1]))


def redact(text: str, secrets: List[Tuple[str, str]]) -> str:
    """Replace secret values (as they are, and as they look inside JSON strings) before text is stored."""
    for name, value in secrets:
        if value in text:
            text = text.replace(value, f"[redacted {name}]")
        escaped = json.dumps(value)[1:-1]
        if escaped != value and escaped in text:
            text = text.replace(escaped, f"[redacted {name}]")
    return text
