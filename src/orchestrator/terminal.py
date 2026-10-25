"""Visible-terminal execution: run a worker inside a desktop terminal window while capturing its output.

The window shows a readable live view (assistant text, tool calls) and stays open briefly afterwards.
The orchestrator waits on an exit-code file, so behaviour matches headless runs exactly. Falls back to
headless (returns None) when no display or terminal emulator is available.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import List, Optional

from orchestrator.util import STOPPING, ProcResult, read_text, register_kill, unregister_kill

_TERMINALS = [
    ("gnome-terminal", lambda title, script: ["gnome-terminal", f"--title={title}", "--", "bash", script]),
    ("konsole", lambda title, script: ["konsole", "-p", f"tabtitle={title}", "-e", "bash", script]),
    ("xfce4-terminal", lambda title, script: ["xfce4-terminal", f"--title={title}", "-x", "bash", script]),
    ("kitty", lambda title, script: ["kitty", "--title", title, "bash", script]),
    ("alacritty", lambda title, script: ["alacritty", "--title", title, "-e", "bash", script]),
    ("xterm", lambda title, script: ["xterm", "-T", title, "-e", "bash", script]),
]


def terminal_available() -> Optional[str]:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) or sys.platform == "win32":
        return None
    for name, _ in _TERMINALS:
        if shutil.which(name):
            return name
    return None


def run_in_terminal(cmd: List[str], cwd: str, timeout: int, title: str, on_line=None) -> Optional[ProcResult]:
    if STOPPING.is_set():
        return ProcResult(-1, "", "stopped by user", False, 0.0)
    term = terminal_available()
    if not term:
        return None
    work = tempfile.mkdtemp(prefix="foreman_term_")
    out_f, err_f, code_f, pid_f = (os.path.join(work, n) for n in ("out", "err", "code", "pid"))
    cmd_f = os.path.join(work, "cmd.json")
    with open(cmd_f, "w", encoding="utf-8") as f:
        json.dump(cmd, f)
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(work, "run.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/bash\n"
            f"cd {shlex.quote(cwd)} || exit 1\n"
            f"PYTHONPATH={shlex.quote(pkg_root)} {shlex.quote(sys.executable)} -m orchestrator.terminal "
            f"--cmd {shlex.quote(cmd_f)} --out {shlex.quote(out_f)} --err {shlex.quote(err_f)} "
            f"--code {shlex.quote(code_f)} --pid {shlex.quote(pid_f)} --title {shlex.quote(title)}\n"
            "read -t 30 -p $'\\n[finished - window closes in 30s, or press Enter]' _\n"
        )
    builder = dict(_TERMINALS)[term]
    start = time.time()
    key = register_kill(lambda: _kill(pid_f))
    try:
        return _wait_window(builder, title, script, timeout, start, work, out_f, err_f, code_f, pid_f, on_line)
    finally:
        unregister_kill(key)


def _wait_window(builder, title, script, timeout, start, work, out_f, err_f, code_f, pid_f, on_line=None):
    try:
        subprocess.Popen(builder(title, script), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return None
    seen = 0

    def feed():  # pass new output lines on as live activity
        nonlocal seen
        if not on_line or not os.path.exists(out_f):
            return
        text = read_text(out_f, 5_000_000)
        complete = text[: text.rfind("\n") + 1]
        for line in complete[seen:].splitlines():
            on_line(line)
        seen = len(complete)

    while not os.path.exists(code_f):
        feed()
        if time.time() - start > timeout:
            _kill(pid_f)
            return ProcResult(-1, read_text(out_f, 5_000_000), read_text(err_f) + f"\ntimed out after {timeout}s",
                              True, time.time() - start)
        if time.time() - start > 20 and not os.path.exists(pid_f):
            return None  # window never started the runner; caller falls back to headless
        time.sleep(0.3)
    time.sleep(0.1)
    feed()
    try:
        code = int(read_text(code_f).strip() or "1")
    except ValueError:
        code = 1
    result = ProcResult(code, read_text(out_f, 5_000_000), read_text(err_f), False, time.time() - start)
    shutil.rmtree(work, ignore_errors=True)
    return result


def _kill(pid_f: str) -> None:
    try:
        pid = int(read_text(pid_f).strip())
        os.killpg(os.getpgid(pid), 15)
    except (ValueError, OSError):
        pass


# ------------------------------------------------------------------ in-window runner

def _pretty(line: str) -> Optional[str]:
    """Human-readable rendering of a JSON event line; None to hide it."""
    try:
        ev = json.loads(line)
    except ValueError:
        return line.rstrip("\n")
    if not isinstance(ev, dict):
        return None
    if ev.get("type") == "assistant":
        parts = []
        for c in (ev.get("message") or {}).get("content") or []:
            if c.get("type") == "text" and c.get("text", "").strip():
                parts.append(c["text"].strip())
            elif c.get("type") == "tool_use":
                inp = c.get("input") or {}
                detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                parts.append(f"\033[36m→ {c.get('name')}\033[0m {str(detail)[:120]}")
        return "\n".join(parts) or None
    if ev.get("type") == "result" or "conversation_id" in ev:
        status = "ok" if not ev.get("is_error") and ev.get("status", "SUCCESS") == "SUCCESS" else "error"
        return f"\n\033[1m■ finished ({status})\033[0m"
    text = ev.get("text") or (ev.get("part") or {}).get("text") or ev.get("response")
    return str(text).strip() if text else None


def _main() -> None:
    ap = argparse.ArgumentParser()
    for a in ("cmd", "out", "err", "code", "pid", "title"):
        ap.add_argument(f"--{a}")
    args = ap.parse_args()
    with open(args.cmd, encoding="utf-8") as f:
        cmd = json.load(f)
    print(f"\033[1m{args.title}\033[0m  ({os.getcwd()})\n")
    code = 1
    with open(args.out, "w", encoding="utf-8") as out, open(args.err, "w", encoding="utf-8") as err:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
                                    text=True, errors="replace", start_new_session=True)
            with open(args.pid, "w") as pf:
                pf.write(str(proc.pid))
            for line in proc.stdout:
                out.write(line)
                out.flush()
                shown = _pretty(line)
                if shown:
                    print(shown, flush=True)
            code = proc.wait()
        except OSError as e:
            err.write(str(e))
            with open(args.pid, "w") as pf:
                pf.write("0")
    with open(args.code + ".tmp", "w") as cf:
        cf.write(str(code))
    os.replace(args.code + ".tmp", args.code)


if __name__ == "__main__":
    _main()
