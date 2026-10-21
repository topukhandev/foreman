"""Desktop visual terminal emulator manager and worker session launcher.
Spawns visible terminal windows (e.g. GNOME Terminal, x-terminal-emulator, Konsole)
so users can visually inspect AI CLI workers running in real time.
"""

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
from typing import Any, Callable, Dict, List, Optional
from orchestrator.finder import find_tool_executable


@dataclass
class TerminalEmulatorInfo:
    name: str
    executable: str
    args_builder: Callable[[str, str], List[str]]


def _build_gnome_terminal_args(title: str, script_path: str) -> List[str]:
    # gnome-terminal --title="..." --wait -- script_path
    return ["gnome-terminal", f"--title={title}", "--wait", "--", script_path]


def _build_generic_xterm_args(title: str, script_path: str) -> List[str]:
    # xterm / x-terminal-emulator -T "title" -e script_path
    return ["x-terminal-emulator", "-T", title, "-e", script_path]


def _build_konsole_args(title: str, script_path: str) -> List[str]:
    # konsole --nofork -e script_path
    return ["konsole", "--nofork", "-e", script_path]


def _build_xfce4_terminal_args(title: str, script_path: str) -> List[str]:
    # xfce4-terminal --title="..." --disable-server -x script_path
    return ["xfce4-terminal", f"--title={title}", "--disable-server", "-x", script_path]


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
        ("gnome-terminal", _build_gnome_terminal_args),
        ("x-terminal-emulator", _build_generic_xterm_args),
        ("konsole", _build_konsole_args),
        ("xfce4-terminal", _build_xfce4_terminal_args),
        ("alacritty", _build_alacritty_args),
        ("kitty", _build_kitty_args),
        ("xterm", _build_xterm_args),
    ]

    for bin_name, builder in emulators:
        path = shutil.which(bin_name)
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return TerminalEmulatorInfo(
                name=bin_name,
                executable=path,
                args_builder=builder
            )

    return None


def can_launch_visual_terminal() -> bool:
    """Return True if visual terminal execution is enabled and supported in this environment."""
    # Check env override
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

    # Ensure PATH inside the visual terminal includes user bin and NVM
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

# Execute command in background so we can record its PID and handle interrupts
({quoted_cmd}) 2>&1 | tee {quoted_out} &
MAIN_PID=$!
echo $MAIN_PID > {quoted_pid}

# Wait for worker process to complete
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


def launch_visual_worker(
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
    """
    Execute a worker command inside a visible desktop terminal window.
    Returns ExecutionResult or None if desktop terminal is not available.
    """
    emulator = detect_desktop_terminal()
    if not emulator:
        return None

    # Import ExecutionResult here to avoid circular dependencies
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
                sys.stdout.write(
                    f"\r\033[K\033[1;36m{spinner[idx % len(spinner)]} [{lbl}]\033[0m "
                    f"Running in visible terminal ({emulator.name})... ({elapsed:.1f}s)"
                )
                sys.stdout.flush()
                idx += 1
                time.sleep(0.1)

            # Check timeout
            if proc.poll() is None:
                # Terminate inner worker process using recorded PID
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

            # Read exit code from code_file
            exit_code = 0
            if os.path.exists(code_file):
                try:
                    exit_code = int(open(code_file).read().strip())
                except Exception:
                    exit_code = 1
            else:
                # Window was closed manually before exit code written
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
            # Non-interactive / headless stdout
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
        # Cleanup temporary files
        for fpath in [script_path, out_file, code_file, pid_file]:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass
