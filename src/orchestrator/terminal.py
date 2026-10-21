"""Desktop visual terminal emulator manager and persistent worker session launcher.
Spawns visible terminal windows (e.g. GNOME Terminal, x-terminal-emulator, Konsole)
that stay open across conversation turns so AI context is retained and users can visually
inspect AI CLI workers in real time.
"""

import atexit
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from orchestrator.finder import find_tool_executable


@dataclass
class TerminalEmulatorInfo:
    name: str
    executable: str
    args_builder: Callable[[str, str], List[str]]
    nowait_args_builder: Optional[Callable[[str, str], List[str]]] = None


def _build_gnome_terminal_args(title: str, script_path: str) -> List[str]:
    # gnome-terminal --title="..." --wait -- script_path
    return ["gnome-terminal", f"--title={title}", "--wait", "--", script_path]


def _build_gnome_terminal_nowait_args(title: str, script_path: str) -> List[str]:
    return ["gnome-terminal", f"--title={title}", "--", script_path]


def _build_generic_xterm_args(title: str, script_path: str) -> List[str]:
    # xterm / x-terminal-emulator -T "title" -e script_path
    return ["x-terminal-emulator", "-T", title, "-e", script_path]


def _build_konsole_args(title: str, script_path: str) -> List[str]:
    # konsole --nofork -e script_path
    return ["konsole", "--nofork", "-e", script_path]


def _build_konsole_nowait_args(title: str, script_path: str) -> List[str]:
    return ["konsole", "-e", script_path]


def _build_xfce4_terminal_args(title: str, script_path: str) -> List[str]:
    # xfce4-terminal --title="..." --disable-server -x script_path
    return ["xfce4-terminal", f"--title={title}", "--disable-server", "-x", script_path]


def _build_xfce4_terminal_nowait_args(title: str, script_path: str) -> List[str]:
    return ["xfce4-terminal", f"--title={title}", "-x", script_path]


def _build_alacritty_args(title: str, script_path: str) -> List[str]:
    return ["alacritty", "--title", title, "-e", script_path]


def _build_kitty_args(title: str, script_path: str) -> List[str]:
    return ["kitty", "--title", title, script_path]


def _build_xterm_args(title: str, script_path: str) -> List[str]:
    return ["xterm", "-title", title, "-e", script_path]


def detect_desktop_terminal() -> Optional[TerminalEmulatorInfo]:
    """
    Detect whether a GUI display is active and find the best installed terminal emulator.
    Returns None if running headless (SSH/Docker/no display).
    """
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not has_display:
        return None

    # Check terminal emulators in order of preference
    emulators = [
        ("gnome-terminal", _build_gnome_terminal_args, _build_gnome_terminal_nowait_args),
        ("x-terminal-emulator", _build_generic_xterm_args, _build_generic_xterm_args),
        ("konsole", _build_konsole_args, _build_konsole_nowait_args),
        ("xfce4-terminal", _build_xfce4_terminal_args, _build_xfce4_terminal_nowait_args),
        ("alacritty", _build_alacritty_args, _build_alacritty_args),
        ("kitty", _build_kitty_args, _build_kitty_args),
        ("xterm", _build_xterm_args, _build_xterm_args),
    ]

    for bin_name, builder, nowait_builder in emulators:
        path = shutil.which(bin_name)
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return TerminalEmulatorInfo(
                name=bin_name,
                executable=path,
                args_builder=builder,
                nowait_args_builder=nowait_builder
            )

    return None


def can_launch_visual_terminal() -> bool:
    """Return True if visual terminal execution is enabled and supported in this environment."""
    env_override = os.environ.get("FOREMAN_VISUAL_TERMINAL", "1").strip().lower()
    if env_override in ("0", "false", "no", "off"):
        return False

    return detect_desktop_terminal() is not None


def create_visual_runner_script(
    cmd: List[str],
    cwd: str,
    worker_name: str,
    task_id: str,
    role: str,
    title: str,
    out_path: str,
    code_path: str,
    pid_path: str
) -> str:
    """
    Create a standalone bash runner script that executes cmd inside cwd,
    displays a live colored banner, streams output in real time to the terminal window,
    and captures stdout/stderr and exit code to files.
    """
    quoted_cwd = shlex.quote(cwd)
    quoted_cmd = " ".join(shlex.quote(arg) for arg in cmd)
    quoted_out = shlex.quote(out_path)
    quoted_code = shlex.quote(code_path)
    quoted_pid = shlex.quote(pid_path)

    current_path = os.environ.get("PATH", "")
    home = os.path.expanduser("~")
    path_extras = [
        os.path.join(home, ".local/bin"),
        os.path.join(home, ".nvm/versions/node/v24.14.1/bin"),
    ]
    extended_path = ":".join(path_extras + [current_path])

    display_cmd = quoted_cmd
    if len(display_cmd) > 80:
        display_cmd = display_cmd[:77] + "..."

    content = f"""#!/bin/bash
# Foreman Orchestrator - Visual Worker Terminal Session
set -o pipefail

# ANSI color codes
CYAN='\\033[1;36m'
GREEN='\\033[1;32m'
YELLOW='\\033[1;33m'
RED='\\033[1;31m'
GRAY='\\033[90m'
WHITE='\\033[1;37m'
RESET='\\033[0m'

clear
echo -e "${{CYAN}}╔════════════════════════════════════════════════════════════════════════════╗${{RESET}}"
echo -e "${{CYAN}}║${{RESET}}  ${{GREEN}}🤖 FOREMAN AI WORKER SESSION${{RESET}} : ${{WHITE}}[{worker_name}]${{RESET}}"
echo -e "${{CYAN}}║${{RESET}}  ${{YELLOW}}Role / Task${{RESET}}                  : ${{WHITE}}[{role.upper()}] #{task_id} - {title[:40]}${{RESET}}"
echo -e "${{CYAN}}║${{RESET}}  Directory                    : ${{GRAY}}{cwd}${{RESET}}"
echo -e "${{CYAN}}║${{RESET}}  Command                      : ${{GRAY}}{display_cmd}${{RESET}}"
echo -e "${{CYAN}}╚════════════════════════════════════════════════════════════════════════════╝${{RESET}}"
echo ""

export PATH="{extended_path}"
cd {quoted_cwd} || exit 1

({quoted_cmd}) 2>&1 | tee {quoted_out} &
MAIN_PID=$!
echo $MAIN_PID > {quoted_pid}

wait $MAIN_PID
EXIT_CODE=$?
echo $EXIT_CODE > {quoted_code}

echo ""
echo -e "${{GRAY}}────────────────────────────────────────────────────────────────────────────${{RESET}}"
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${{GREEN}}✓ Worker completed successfully (exit code 0).${{RESET}}"
    echo -e "${{GRAY}}(This window will automatically close in 3 seconds...)${{RESET}}"
    sleep 3
else
    echo -e "${{RED}}✗ Worker exited with error code $EXIT_CODE.${{RESET}}"
    echo -e "${{GRAY}}(This window will close in 5 seconds...)${{RESET}}"
    sleep 5
fi
"""
    tmp_fd, script_path = tempfile.mkstemp(prefix="foreman_worker_", suffix=".sh")
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(script_path, stat.S_IRWXU)
    return script_path


def _read_file_text(path: str) -> str:
    """Safely read and return text from file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


@dataclass
class VisualSession:
    """Represents a persistent desktop visual terminal session for an AI worker."""
    key: str
    worker_name: str
    session_dir: str
    script_path: str
    proc: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    _closed: bool = False

    def is_alive(self) -> bool:
        """Return True if the terminal window process or bash runner is still alive."""
        if self._closed:
            return False

        if self.proc is not None and self.proc.poll() is not None:
            return False

        pid_file = os.path.join(self.session_dir, "session.pid")
        if os.path.exists(pid_file):
            try:
                pid = int(_read_file_text(pid_file))
                self.pid = pid
                os.kill(pid, 0)
                return True
            except (ValueError, ProcessLookupError, PermissionError):
                return False

        return self.proc is not None and self.proc.poll() is None

    def kill_active_task(self) -> None:
        """Kill the currently active inner task process if any."""
        pid_file = os.path.join(self.session_dir, "task.pid")
        if os.path.exists(pid_file):
            try:
                tpid = int(_read_file_text(pid_file))
                os.kill(tpid, signal.SIGTERM)
                time.sleep(0.1)
                os.kill(tpid, signal.SIGKILL)
            except Exception:
                pass

    def close(self) -> None:
        """Gracefully close the terminal window and clean up session directory."""
        self._closed = True
        close_trigger = os.path.join(self.session_dir, "close_trigger")
        try:
            open(close_trigger, "w").close()
        except Exception:
            pass

        if self.proc is not None:
            try:
                self.proc.wait(timeout=0.3)
            except Exception:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=0.2)
                except Exception:
                    pass

        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except Exception:
                pass

        try:
            shutil.rmtree(self.session_dir, ignore_errors=True)
        except Exception:
            pass


class VisualSessionManager:
    """Singleton manager tracking persistent desktop terminal windows across turns."""

    _instance: Optional["VisualSessionManager"] = None

    def __init__(self):
        self._sessions: Dict[str, VisualSession] = {}

    @classmethod
    def get_instance(cls) -> "VisualSessionManager":
        if cls._instance is None:
            cls._instance = VisualSessionManager()
        return cls._instance

    def get_session(self, key: str) -> Optional[VisualSession]:
        session = self._sessions.get(key)
        if session and session.is_alive():
            return session
        if session:
            session.close()
            del self._sessions[key]
        return None

    def start_session(
        self,
        key: str,
        worker_name: str,
        cwd: str,
        title: Optional[str] = None
    ) -> Optional[VisualSession]:
        emulator = detect_desktop_terminal()
        if not emulator:
            return None

        if key in self._sessions:
            self._sessions[key].close()
            del self._sessions[key]

        session_dir = tempfile.mkdtemp(prefix=f"foreman_vsession_{worker_name}_")
        window_title = title or f"Foreman AI Worker: [{worker_name}]"

        current_path = os.environ.get("PATH", "")
        home = os.path.expanduser("~")
        path_extras = [
            os.path.join(home, ".local/bin"),
            os.path.join(home, ".nvm/versions/node/v24.14.1/bin"),
        ]
        extended_path = ":".join(path_extras + [current_path])
        quoted_cwd = shlex.quote(cwd)

        runner_script_content = f"""#!/bin/bash
# Foreman Orchestrator - Persistent AI Worker Terminal Session
set -o pipefail

CYAN='\\033[1;36m'
GREEN='\\033[1;32m'
YELLOW='\\033[1;33m'
RED='\\033[1;31m'
GRAY='\\033[90m'
WHITE='\\033[1;37m'
RESET='\\033[0m'

export PATH="{extended_path}"
cd {quoted_cwd} || exit 1

SESSION_DIR="{session_dir}"
echo $$ > "$SESSION_DIR/session.pid"
touch "$SESSION_DIR/ready"

clear
echo -e "${{CYAN}}╔════════════════════════════════════════════════════════════════════════════╗${{RESET}}"
echo -e "${{CYAN}}║${{RESET}}  ${{GREEN}}🤖 FOREMAN AI WORKER — PERSISTENT SESSION${{RESET}} : ${{WHITE}}[{worker_name}]${{RESET}}"
echo -e "${{CYAN}}║${{RESET}}  Directory : ${{GRAY}}{cwd}${{RESET}}"
echo -e "${{CYAN}}║${{RESET}}  Status    : ${{GREEN}}ACTIVE & READY FOR ORCHESTRATOR TASKS${{RESET}}"
echo -e "${{CYAN}}║${{RESET}}  Context   : ${{WHITE}}Preserved Across Turns (Window Kept Open)${{RESET}}"
echo -e "${{CYAN}}╚════════════════════════════════════════════════════════════════════════════╝${{RESET}}"
echo ""
echo -e "${{GRAY}}This terminal window stays open across conversation turns so AI context is retained.${{RESET}}"
echo -e "${{GRAY}}Foreman commands and agent outputs will run and stream here in real time.${{RESET}}"
echo ""

while true; do
    echo -e "${{GRAY}}⏳ Waiting for next instruction from Orchestrator...${{RESET}}"
    echo "idle" > "$SESSION_DIR/status"

    while true; do
        if [ -f "$SESSION_DIR/close_trigger" ]; then
            echo ""
            echo -e "${{YELLOW}}👋 Session closed by Orchestrator. Exiting window...${{RESET}}"
            sleep 0.1
            exit 0
        fi
        if [ -f "$SESSION_DIR/task_trigger" ]; then
            rm -f "$SESSION_DIR/task_trigger"
            break
        fi
        sleep 0.05
    done

    TASK_SH="$SESSION_DIR/run_task.sh"
    if [ ! -f "$TASK_SH" ]; then
        continue
    fi

    echo "running" > "$SESSION_DIR/status"
    chmod +x "$TASK_SH"
    bash "$TASK_SH"
    EXIT_CODE=$?
    echo $EXIT_CODE > "$SESSION_DIR/task_done"
    echo "idle" > "$SESSION_DIR/status"

    echo ""
    echo -e "${{GRAY}}────────────────────────────────────────────────────────────────────────────${{RESET}}"
    if [ $EXIT_CODE -eq 0 ]; then
        echo -e "${{GREEN}}✓ Task completed successfully.${{RESET}}"
    else
        echo -e "${{RED}}✗ Task completed with exit code $EXIT_CODE.${{RESET}}"
    fi
    echo -e "${{GRAY}}⏳ Session kept open — waiting for next instruction from Orchestrator...${{RESET}}"
    echo ""
done
"""
        runner_sh = os.path.join(session_dir, "session_runner.sh")
        with open(runner_sh, "w", encoding="utf-8") as f:
            f.write(runner_script_content)
        os.chmod(runner_sh, stat.S_IRWXU)

        term_cmd = emulator.args_builder(window_title, runner_sh)
        try:
            proc = subprocess.Popen(term_cmd)
        except Exception:
            shutil.rmtree(session_dir, ignore_errors=True)
            return None

        ready_file = os.path.join(session_dir, "ready")
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if os.path.exists(ready_file):
                break
            if proc.poll() is not None:
                shutil.rmtree(session_dir, ignore_errors=True)
                return None
            time.sleep(0.05)

        if not os.path.exists(ready_file):
            try:
                proc.terminate()
            except Exception:
                pass
            shutil.rmtree(session_dir, ignore_errors=True)
            return None

        session = VisualSession(
            key=key,
            worker_name=worker_name,
            session_dir=session_dir,
            script_path=runner_sh,
            proc=proc
        )
        pid_file = os.path.join(session_dir, "session.pid")
        val = _read_file_text(pid_file)
        if val:
            try:
                session.pid = int(val)
            except Exception:
                pass

        self._sessions[key] = session
        return session

    def close_session(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session:
            session.close()

    def close_all(self) -> None:
        for key in list(self._sessions.keys()):
            self.close_session(key)


def close_all_visual_sessions() -> None:
    """Close all active persistent visual worker terminal windows."""
    VisualSessionManager.get_instance().close_all()


def close_visual_session(key: str) -> None:
    """Close a specific persistent visual worker terminal window."""
    VisualSessionManager.get_instance().close_session(key)


# Auto-cleanup on process termination
atexit.register(close_all_visual_sessions)


def _launch_oneshot_visual_worker(
    cmd: List[str],
    cwd: str,
    title: str,
    worker_name: str,
    task_id: str = "general",
    role: str = "worker",
    timeout_sec: int = 300,
    show_progress: bool = True,
    progress_label: Optional[str] = None
) -> Optional[Any]:
    """Fallback runner for single-shot ephemeral visual terminal."""
    emulator = detect_desktop_terminal()
    if not emulator:
        return None

    from orchestrator.adapters.base import ExecutionResult

    out_file = tempfile.mktemp(prefix="foreman_out_")
    code_file = tempfile.mktemp(prefix="foreman_code_")
    pid_file = tempfile.mktemp(prefix="foreman_pid_")

    script_path = create_visual_runner_script(
        cmd=cmd,
        cwd=cwd,
        worker_name=worker_name,
        task_id=task_id,
        role=role,
        title=title,
        out_path=out_file,
        code_path=code_file,
        pid_path=pid_file
    )

    term_cmd = emulator.args_builder(title, script_path)
    start_time = time.time()
    lbl = progress_label or worker_name

    try:
        proc = subprocess.Popen(term_cmd)

        if show_progress and sys.stdout.isatty():
            spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            idx = 0
            while proc.poll() is None:
                elapsed = time.time() - start_time
                if elapsed > timeout_sec:
                    break
                cols = shutil.get_terminal_size(fallback=(80, 24)).columns
                spin_ch = spinner[idx % len(spinner)]
                full_desc = f"Running ({emulator.name})... ({elapsed:.1f}s)"
                if len(f"{spin_ch} [{lbl}] {full_desc}") >= cols - 1:
                    full_desc = f"Running... ({elapsed:.1f}s)"
                    if len(f"{spin_ch} [{lbl}] {full_desc}") >= cols - 1:
                        full_desc = f"({elapsed:.1f}s)"
                sys.stdout.write(f"\r\033[K\033[1;36m{spin_ch} [{lbl}]\033[0m {full_desc}")
                sys.stdout.flush()
                idx += 1
                time.sleep(0.1)

            if proc.poll() is None:
                if os.path.exists(pid_file):
                    try:
                        pid = int(open(pid_file).read().strip())
                        os.kill(pid, signal.SIGTERM)
                        time.sleep(0.2)
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass
                try:
                    proc.terminate()
                except Exception:
                    pass

                sys.stdout.write(f"\r\033[K\033[1;31m✗ [{lbl}]\033[0m Timed out after {timeout_sec}s\n")
                sys.stdout.flush()
                return ExecutionResult(
                    success=False,
                    output="",
                    error=f"Visual worker timed out after {timeout_sec} seconds.",
                    exit_code=-1,
                    duration_sec=time.time() - start_time
                )

            duration = time.time() - start_time
            proc.wait()

            exit_code = 0
            if os.path.exists(code_file):
                try:
                    exit_code = int(open(code_file).read().strip())
                except Exception:
                    exit_code = 1
            else:
                exit_code = 130

            output_content = ""
            if os.path.exists(out_file):
                try:
                    with open(out_file, "r", encoding="utf-8", errors="replace") as f:
                        output_content = f.read()
                except Exception:
                    pass

            if exit_code == 0:
                sys.stdout.write(f"\r\033[K\033[1;32m✓ [{lbl}]\033[0m Responded in {duration:.1f}s\n\n")
            else:
                sys.stdout.write(f"\r\033[K\033[1;31m✗ [{lbl}]\033[0m Finished with code {exit_code} ({duration:.1f}s)\n\n")
            sys.stdout.flush()

            return ExecutionResult(
                success=(exit_code == 0),
                output=output_content,
                error=None if exit_code == 0 else f"Visual worker exited with code {exit_code}",
                exit_code=exit_code,
                duration_sec=duration
            )

        else:
            proc.wait(timeout=timeout_sec)
            duration = time.time() - start_time
            exit_code = 0
            if os.path.exists(code_file):
                try:
                    exit_code = int(open(code_file).read().strip())
                except Exception:
                    exit_code = 1
            else:
                exit_code = 130

            output_content = ""
            if os.path.exists(out_file):
                try:
                    with open(out_file, "r", encoding="utf-8", errors="replace") as f:
                        output_content = f.read()
                except Exception:
                    pass

            return ExecutionResult(
                success=(exit_code == 0),
                output=output_content,
                error=None if exit_code == 0 else f"Visual worker exited with code {exit_code}",
                exit_code=exit_code,
                duration_sec=duration
            )

    except subprocess.TimeoutExpired:
        if os.path.exists(pid_file):
            try:
                pid = int(open(pid_file).read().strip())
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        return ExecutionResult(
            success=False,
            output="",
            error=f"Visual worker timed out after {timeout_sec}s",
            exit_code=-1,
            duration_sec=time.time() - start_time
        )
    finally:
        for fpath in [script_path, out_file, code_file, pid_file]:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass


def launch_visual_worker(
    cmd: List[str],
    cwd: str,
    title: str,
    worker_name: str,
    task_id: str = "general",
    role: str = "worker",
    timeout_sec: int = 300,
    show_progress: bool = True,
    progress_label: Optional[str] = None,
    session_id: Optional[str] = None
) -> Optional[Any]:
    """
    Execute a worker command inside a visible desktop terminal window.
    Maintains a persistent terminal window across turns so AI context is retained.
    Returns ExecutionResult or None if desktop terminal is not available.
    """
    emulator = detect_desktop_terminal()
    if not emulator:
        return None

    from orchestrator.adapters.base import ExecutionResult

    manager = VisualSessionManager.get_instance()
    session_key = worker_name

    session = manager.get_session(session_key)
    if not session:
        session = manager.start_session(
            key=session_key,
            worker_name=worker_name,
            cwd=cwd,
            title=f"Foreman Worker: [{worker_name}]"
        )
        if not session:
            return _launch_oneshot_visual_worker(
                cmd=cmd, cwd=cwd, title=title, worker_name=worker_name,
                task_id=task_id, role=role, timeout_sec=timeout_sec,
                show_progress=show_progress, progress_label=progress_label
            )

    session_dir = session.session_dir
    task_done_file = os.path.join(session_dir, "task_done")
    task_out_file = os.path.join(session_dir, "task_out.log")
    task_pid_file = os.path.join(session_dir, "task.pid")
    task_trigger = os.path.join(session_dir, "task_trigger")

    for p in [task_done_file, task_out_file]:
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    quoted_cmd = " ".join(shlex.quote(arg) for arg in cmd)
    display_cmd = quoted_cmd if len(quoted_cmd) <= 80 else quoted_cmd[:77] + "..."
    quoted_cwd = shlex.quote(cwd)

    task_script = f"""#!/bin/bash
cd {quoted_cwd} || exit 1
CYAN='\\033[1;36m'
YELLOW='\\033[1;33m'
WHITE='\\033[1;37m'
GRAY='\\033[90m'
RESET='\\033[0m'

echo -e "${{CYAN}}╔════════════════════════════════════════════════════════════════════════════╗${{RESET}}"
echo -e "${{CYAN}}║${{RESET}}  ${{YELLOW}}▶ [{role.upper()}]${{RESET}} Task #{task_id}: ${{WHITE}}{title[:45]}${{RESET}}"
echo -e "${{CYAN}}║${{RESET}}  Command : ${{GRAY}}{display_cmd}${{RESET}}"
echo -e "${{CYAN}}╚════════════════════════════════════════════════════════════════════════════╝${{RESET}}"
echo ""

({quoted_cmd}) 2>&1 | tee "{task_out_file}" &
MAIN_PID=$!
echo $MAIN_PID > "{task_pid_file}"
wait $MAIN_PID
EXIT_CODE=${{PIPESTATUS[0]}}
rm -f "{task_pid_file}"
exit $EXIT_CODE
"""
    task_sh = os.path.join(session_dir, "run_task.sh")
    with open(task_sh, "w", encoding="utf-8") as f:
        f.write(task_script)
    os.chmod(task_sh, stat.S_IRWXU)

    # Signal persistent session loop to execute task
    open(task_trigger, "w").close()

    start_time = time.time()
    lbl = progress_label or worker_name

    try:
        if show_progress and sys.stdout.isatty():
            spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            idx = 0
            while not os.path.exists(task_done_file):
                elapsed = time.time() - start_time
                if elapsed > timeout_sec:
                    break

                if not session.is_alive():
                    break

                cols = shutil.get_terminal_size(fallback=(80, 24)).columns
                spin_ch = spinner[idx % len(spinner)]

                last_act = ""
                if os.path.exists(task_out_file):
                    try:
                        with open(task_out_file, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(0, os.SEEK_END)
                            size = f.tell()
                            f.seek(max(0, size - 1024))
                            lines = [l.strip() for l in f.read().splitlines() if l.strip()]
                            if lines:
                                import re
                                last_act = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', lines[-1])
                    except Exception:
                        pass

                time_str = f"({elapsed:.1f}s)"
                if last_act:
                    snippet = last_act if len(last_act) <= 40 else last_act[:37] + "..."
                    full_desc = f"{time_str} › {snippet}"
                else:
                    full_desc = f"Running ({emulator.name})... {time_str}"

                prefix = f"{spin_ch} [{lbl}] "
                avail = cols - len(prefix) - 1
                if avail > 5 and len(full_desc) > avail:
                    full_desc = full_desc[:avail - 3] + "..."
                elif avail <= 5:
                    full_desc = time_str

                sys.stdout.write(f"\r\033[K\033[1;36m{spin_ch} [{lbl}]\033[0m {full_desc}")
                sys.stdout.flush()
                idx += 1
                time.sleep(0.1)

            elapsed = time.time() - start_time

            if not os.path.exists(task_done_file):
                if not session.is_alive():
                    sys.stdout.write(f"\r\033[K\033[1;33m! [{lbl}]\033[0m Visual window was closed by user.\n\n")
                    sys.stdout.flush()
                    manager.close_session(session_key)
                    return ExecutionResult(
                        success=False,
                        output="",
                        error="Visual worker terminal was closed.",
                        exit_code=130,
                        duration_sec=elapsed
                    )
                else:
                    session.kill_active_task()
                    sys.stdout.write(f"\r\033[K\033[1;31m✗ [{lbl}]\033[0m Timed out after {timeout_sec}s\n\n")
                    sys.stdout.flush()
                    return ExecutionResult(
                        success=False,
                        output="",
                        error=f"Visual worker timed out after {timeout_sec}s",
                        exit_code=-1,
                        duration_sec=elapsed
                    )

            exit_code = 0
            code_str = _read_file_text(task_done_file)
            if code_str:
                try:
                    exit_code = int(code_str)
                except Exception:
                    exit_code = 1

            output_content = ""
            if os.path.exists(task_out_file):
                try:
                    with open(task_out_file, "r", encoding="utf-8", errors="replace") as f:
                        output_content = f.read()
                except Exception:
                    pass

            if exit_code == 0:
                sys.stdout.write(f"\r\033[K\033[1;32m✓ [{lbl}]\033[0m Responded in {elapsed:.1f}s\n\n")
            else:
                sys.stdout.write(f"\r\033[K\033[1;31m✗ [{lbl}]\033[0m Finished with code {exit_code} ({elapsed:.1f}s)\n\n")
            sys.stdout.flush()

            return ExecutionResult(
                success=(exit_code == 0),
                output=output_content,
                error=None if exit_code == 0 else f"Visual worker exited with code {exit_code}",
                exit_code=exit_code,
                duration_sec=elapsed
            )

        else:
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                if os.path.exists(task_done_file):
                    break
                if not session.is_alive():
                    break
                time.sleep(0.1)

            elapsed = time.time() - start_time
            if not os.path.exists(task_done_file):
                if not session.is_alive():
                    manager.close_session(session_key)
                    return ExecutionResult(
                        success=False,
                        output="",
                        error="Visual worker terminal was closed.",
                        exit_code=130,
                        duration_sec=elapsed
                    )
                session.kill_active_task()
                return ExecutionResult(
                    success=False,
                    output="",
                    error=f"Visual worker timed out after {timeout_sec}s",
                    exit_code=-1,
                    duration_sec=elapsed
                )

            exit_code = 0
            code_str = _read_file_text(task_done_file)
            if code_str:
                try:
                    exit_code = int(code_str)
                except Exception:
                    exit_code = 1

            output_content = ""
            if os.path.exists(task_out_file):
                try:
                    with open(task_out_file, "r", encoding="utf-8", errors="replace") as f:
                        output_content = f.read()
                except Exception:
                    pass

            return ExecutionResult(
                success=(exit_code == 0),
                output=output_content,
                error=None if exit_code == 0 else f"Visual worker exited with code {exit_code}",
                exit_code=exit_code,
                duration_sec=elapsed
            )

    except Exception as e:
        return ExecutionResult(
            success=False,
            output="",
            error=f"Visual worker error: {str(e)}",
            exit_code=-1,
            duration_sec=time.time() - start_time
        )




# ---------------------------------------------------------------------------
# Multi-Terminal Interactive Spawner & Tracker
# ---------------------------------------------------------------------------

@dataclass
class SpawnedTerminal:
    id: str
    tool_name: str
    cwd: str
    pid: Optional[int]
    title: str
    created_at: float
    session_dir: str
    proc: Optional[subprocess.Popen]

    def is_alive(self) -> bool:
        if self.pid:
            try:
                os.kill(self.pid, 0)
                return True
            except OSError:
                pass
        if self.proc:
            return self.proc.poll() is None
        return False

    def close(self) -> None:
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        shutil.rmtree(self.session_dir, ignore_errors=True)


class SpawnedTerminalTracker:
    """Manages independent interactive desktop terminal windows spawned by user command."""

    _instance: Optional["SpawnedTerminalTracker"] = None

    def __init__(self):
        self._terminals: Dict[str, SpawnedTerminal] = {}

    @classmethod
    def get_instance(cls) -> "SpawnedTerminalTracker":
        if cls._instance is None:
            cls._instance = SpawnedTerminalTracker()
        return cls._instance

    def spawn_terminal(
        self,
        tool: str,
        cwd: str,
        title: Optional[str] = None
    ) -> Tuple[Optional[SpawnedTerminal], Optional[str]]:
        """
        Spawn a single interactive desktop terminal running `tool` inside `cwd`.
        Returns: (SpawnedTerminal, error_message)
        """
        emulator = detect_desktop_terminal()
        if not emulator:
            return None, "No desktop terminal emulator available (headless environment)."

        # Resolve directory
        target_dir = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(target_dir):
            return None, f"Directory does not exist: '{target_dir}'"

        # Resolve tool executable
        tool_clean = tool.lower().strip()
        display_name = tool_clean
        tool_bin = None

        if tool_clean in ("agy", "antigravity", "antigravity_cli", "এজিআই", "অ্যান্টিগ্রাভিটি"):
            tool_bin = find_tool_executable("antigravity_cli") or find_tool_executable("agy") or shutil.which("agy")
            display_name = "antigravity_cli"
        elif tool_clean in ("claude", "claude_code", "ক্লড"):
            tool_bin = find_tool_executable("claude_code") or find_tool_executable("claude") or shutil.which("claude")
            display_name = "claude_code"
        elif tool_clean in ("opencode", "opencode_ai", "ওপেনকোড", "ওপেন কোড"):
            tool_bin = find_tool_executable("opencode") or shutil.which("opencode")
            display_name = "opencode"
        elif tool_clean in ("bash", "sh", "terminal", "shell"):
            tool_bin = os.environ.get("SHELL", "/bin/bash")
            display_name = "bash"
        else:
            tool_bin = shutil.which(tool) or tool
            display_name = tool

        if not tool_bin:
            return None, f"Tool '{tool}' is not installed or executable not found on this machine."

        term_id = f"term_{int(time.time())}_{len(self._terminals) + 1}"
        window_title = title or f"Foreman Worker: [{display_name}] ({os.path.basename(target_dir)})"
        session_dir = tempfile.mkdtemp(prefix=f"foreman_term_{display_name}_")

        pid_file = os.path.join(session_dir, "term.pid")
        launcher_sh = os.path.join(session_dir, "launcher.sh")

        quoted_cwd = shlex.quote(target_dir)
        quoted_bin = shlex.quote(tool_bin)

        script_content = f"""#!/bin/bash
cd {quoted_cwd} || exit 1
echo $$ > "{pid_file}"
exec {quoted_bin}
"""
        with open(launcher_sh, "w", encoding="utf-8") as f:
            f.write(script_content)
        os.chmod(launcher_sh, stat.S_IRWXU)

        builder = emulator.nowait_args_builder or emulator.args_builder
        term_cmd = builder(window_title, launcher_sh)

        try:
            proc = subprocess.Popen(term_cmd, cwd=target_dir, start_new_session=True)
        except Exception as e:
            shutil.rmtree(session_dir, ignore_errors=True)
            return None, f"Failed to launch terminal window ({emulator.name}): {e}"

        # Wait briefly for pid_file
        deadline = time.time() + 1.5
        pid_val = None
        while time.time() < deadline:
            val = _read_file_text(pid_file)
            if val:
                try:
                    pid_val = int(val)
                    break
                except Exception:
                    pass
            time.sleep(0.05)

        term_obj = SpawnedTerminal(
            id=term_id,
            tool_name=display_name,
            cwd=target_dir,
            pid=pid_val or proc.pid,
            title=window_title,
            created_at=time.time(),
            session_dir=session_dir,
            proc=proc
        )
        self._terminals[term_id] = term_obj
        return term_obj, None

    def spawn_batch(self, specs: List[Dict[str, Any]]) -> Tuple[List[SpawnedTerminal], List[str]]:
        """
        Spawn multiple terminals based on specifications list.
        Each spec: {"provider": str, "count": int, "dir": str}
        Enforces SystemGuard checks before launching.
        """
        from orchestrator.system_guard import SystemGuard

        total_requested = sum(max(1, int(s.get("count", 1))) for s in specs)
        target_dir = specs[0].get("dir", ".") if specs else "."

        # System Guard check!
        can_spawn, guard_msg = SystemGuard.can_spawn(total_requested, target_dir=target_dir)
        if not can_spawn:
            return [], [guard_msg or "Spawning blocked by System Resource Guard."]

        spawned = []
        errors = []
        if guard_msg:
            errors.append(guard_msg)

        idx = 1
        for s in specs:
            tool = s.get("provider", "antigravity_cli")
            count = max(1, int(s.get("count", 1)))
            cwd = s.get("dir", ".")
            for _ in range(count):
                title = f"Foreman Worker #{idx}: [{tool}] ({os.path.basename(os.path.abspath(os.path.expanduser(cwd)))})"
                term, err = self.spawn_terminal(tool=tool, cwd=cwd, title=title)
                if term:
                    spawned.append(term)
                    idx += 1
                elif err:
                    errors.append(err)

        return spawned, errors

    def list_active(self) -> List[SpawnedTerminal]:
        """Return list of active, alive spawned terminals."""
        alive = []
        for t_id, term in list(self._terminals.items()):
            if term.is_alive():
                alive.append(term)
            else:
                self._terminals.pop(t_id, None)
        return alive

    def close_all(self) -> None:
        """Close all spawned terminals."""
        for term in list(self._terminals.values()):
            term.close()
        self._terminals.clear()


def spawn_interactive_terminal(tool: str, cwd: str, title: Optional[str] = None) -> Tuple[Optional[SpawnedTerminal], Optional[str]]:
    return SpawnedTerminalTracker.get_instance().spawn_terminal(tool, cwd, title)


def spawn_terminals_batch(specs: List[Dict[str, Any]]) -> Tuple[List[SpawnedTerminal], List[str]]:
    return SpawnedTerminalTracker.get_instance().spawn_batch(specs)


def list_active_terminals() -> List[SpawnedTerminal]:
    return SpawnedTerminalTracker.get_instance().list_active()


def close_all_spawned_terminals() -> None:
    SpawnedTerminalTracker.get_instance().close_all()


get_active_spawned_terminals = list_active_terminals


def play_notification_sound(sound_name: str = "complete") -> None:
    """
    Play a brief desktop audio chime on task/sprint completion.
    Non-blocking, non-failing fallback across canberra-gtk-play, paplay, pw-play, aplay.
    """
    try:
        # 1. Try canberra-gtk-play (standard desktop theme sound)
        canberra = shutil.which("canberra-gtk-play")
        if canberra and os.path.isfile(canberra):
            subprocess.Popen(
                [canberra, "-i", sound_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return

        # 2. Try freedesktop sound files via paplay / pw-play / aplay
        sound_path = f"/usr/share/sounds/freedesktop/stereo/{sound_name}.oga"
        if not os.path.exists(sound_path):
            sound_path = "/usr/share/sounds/freedesktop/stereo/complete.oga"

        if os.path.exists(sound_path):
            for player in ["paplay", "pw-play", "aplay"]:
                player_bin = shutil.which(player)
                if player_bin and os.path.isfile(player_bin):
                    subprocess.Popen(
                        [player_bin, sound_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    return
    except Exception:
        pass


def send_desktop_notification(
    title: str,
    message: str,
    urgency: str = "normal",
    sound: bool = False,
    sound_name: str = "complete"
) -> None:
    """
    Send subtle desktop notification using notify-send on Linux/X11/Wayland if available.
    Optionally play an audio chime on completion.
    Does not block or fail if desktop display or notify-send is absent.
    """
    if sound:
        play_notification_sound(sound_name)

    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not has_display:
        return
    notify_bin = shutil.which("notify-send")
    if notify_bin and os.path.isfile(notify_bin):
        try:
            clean_title = str(title).replace("\n", " ").strip()
            clean_msg = str(message).strip()
            urg = urgency if urgency in ("low", "normal", "critical") else "normal"
            subprocess.Popen(
                [notify_bin, "-u", urg, "-a", "Foreman Orchestrator", clean_title, clean_msg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            pass


atexit.register(close_all_spawned_terminals)
