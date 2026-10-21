"""Live streaming visual runner for AI worker terminals.
Provides real-time token/text streaming, live spinning timers, workspace file watch notifications,
and clean output persistence so terminal windows never appear frozen or static.
"""

import argparse
import json
import os
import re
import shlex
import sys
import threading
import time
from typing import Dict, List, Optional, Set
import subprocess

current_dir = os.path.dirname(os.path.realpath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)


class LiveWorkerStreamer:
    """Runs a worker command inside the visual terminal, streaming output and dynamic activity in real time."""

    SPINNER_CHARS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(
        self,
        cmd: List[str],
        cwd: str,
        out_file: str,
        pid_file: Optional[str] = None,
        title: str = "Worker Task",
        worker_name: str = "worker",
        role: str = "worker",
        task_id: str = "1"
    ):
        self.cmd = cmd
        self.cwd = os.path.abspath(cwd)
        self.out_file = out_file
        self.pid_file = pid_file
        self.title = title
        self.worker_name = worker_name
        self.role = role
        self.task_id = task_id

        self.start_time = 0.0
        self.process: Optional[subprocess.Popen] = None
        self.exit_code = 0
        self.accumulated_output: List[str] = []
        self.last_stream_time = 0.0
        self.has_active_text = False
        self.is_running = True
        self.known_files: Dict[str, float] = {}

    def _init_workspace_files(self) -> None:
        """Snapshot current files in cwd to detect newly created or modified files."""
        try:
            for root, dirs, files in os.walk(self.cwd):
                # Ignore noisy directories
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "hub", "dist", ".cache", ".next", ".turbo")]
                for f in files:
                    if f.endswith((".log", ".pid", ".tmp")):
                        continue
                    full_p = os.path.join(root, f)
                    try:
                        self.known_files[full_p] = os.path.getmtime(full_p)
                    except OSError:
                        pass
        except Exception:
            pass

    def _check_workspace_updates(self) -> List[str]:
        """Detect any newly created or recently modified files in the workspace."""
        updated: List[str] = []
        try:
            for root, dirs, files in os.walk(self.cwd):
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "hub", "dist", ".cache", ".next", ".turbo")]
                for f in files:
                    if f.endswith((".log", ".pid", ".tmp")):
                        continue
                    full_p = os.path.join(root, f)
                    try:
                        mtime = os.path.getmtime(full_p)
                        if full_p not in self.known_files:
                            self.known_files[full_p] = mtime
                            rel_p = os.path.relpath(full_p, self.cwd)
                            updated.append(f"created {rel_p}")
                        elif mtime > self.known_files[full_p] + 0.1:
                            self.known_files[full_p] = mtime
                            rel_p = os.path.relpath(full_p, self.cwd)
                            updated.append(f"modified {rel_p}")
                    except OSError:
                        pass
        except Exception:
            pass
        return updated

    def _print_header(self) -> None:
        """Render modern header card in terminal."""
        cyan = "\033[1;36m"
        yellow = "\033[1;33m"
        white = "\033[1;37m"
        gray = "\033[90m"
        green = "\033[1;32m"
        reset = "\033[0m"

        display_cmd = " ".join(shlex.quote(c) for c in self.cmd)
        if len(display_cmd) > 75:
            display_cmd = display_cmd[:72] + "..."

        sys.stdout.write(f"{cyan}╔════════════════════════════════════════════════════════════════════════════╗{reset}\n")
        sys.stdout.write(f"{cyan}║{reset}  {yellow}▶ [{self.role.upper()}]{reset} Task #{self.task_id}: {white}{self.title[:45]}{reset}\n")
        sys.stdout.write(f"{cyan}║{reset}  Worker   : {white}[{self.worker_name}]{reset}  {gray}│ Directory: {self.cwd}{reset}\n")
        sys.stdout.write(f"{cyan}║{reset}  Command  : {gray}{display_cmd}{reset}\n")
        sys.stdout.write(f"{cyan}╚════════════════════════════════════════════════════════════════════════════╝{reset}\n")
        sys.stdout.write(f"{green}⚡ Live Stream Active:{reset} {gray}AI generation, token streams, and workspace file activity below:{reset}\n\n")
        sys.stdout.flush()

    def _handle_stdout_line(self, line: str) -> None:
        """Parse raw line or JSON stream event and render to screen in real time."""
        self.last_stream_time = time.time()
        line_clean = line.rstrip("\r\n")

        # Check for NDJSON stream event (e.g. from agy --output-format stream-json)
        if line_clean.startswith("{") and line_clean.endswith("}"):
            try:
                data = json.loads(line_clean)
                event = data.get("event")

                if event == "step_update":
                    update = data.get("step_update", {})
                    delta = update.get("text_delta")
                    if delta:
                        # Clear any lingering spinner before printing text
                        if self.has_active_text:
                            sys.stdout.write("\r\033[K")
                            self.has_active_text = False
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                        self.accumulated_output.append(delta)
                        return

                    step_type = update.get("step_type", "")
                    if "tool" in step_type.lower():
                        tool_name = update.get("tool_name", "tool")
                        if self.has_active_text:
                            sys.stdout.write("\r\033[K")
                            self.has_active_text = False
                        sys.stdout.write(f"\n\033[1;36m⏺ Tool Action:\033[0m \033[1;33m{tool_name}\033[0m\n")
                        sys.stdout.flush()
                    return

                elif event == "result":
                    res = data.get("result", {})
                    final_resp = res.get("response", "")
                    if final_resp and not self.accumulated_output:
                        self.accumulated_output.append(final_resp)
                        sys.stdout.write(f"\n{final_resp}\n")
                        sys.stdout.flush()
                    return

                elif event in ("init", "step_update", "ping"):
                    return

            except Exception:
                pass

        # Standard text line (or unparsed JSON)
        if self.has_active_text:
            sys.stdout.write("\r\033[K")
            self.has_active_text = False

        sys.stdout.write(line)
        sys.stdout.flush()
        self.accumulated_output.append(line)

    def _spinner_loop(self) -> None:
        """Background ticker showing rotating spinner, timer, and disk activity when idle."""
        idx = 0
        last_file_check = 0.0

        while self.is_running:
            now = time.time()
            elapsed = now - self.start_time

            # Check workspace updates every 1.5 seconds
            if now - last_file_check >= 1.5:
                last_file_check = now
                updates = self._check_workspace_updates()
                for up in updates:
                    sys.stdout.write(f"\r\033[K\033[1;36m  ↳ 📄 Workspace:\033[0m \033[1;37m{up}\033[0m\n")
                    sys.stdout.flush()

            # If no streaming text has arrived recently, show live active spinner + elapsed timer
            if now - self.last_stream_time >= 0.15:
                char = self.SPINNER_CHARS[idx % len(self.SPINNER_CHARS)]
                status_text = (
                    f"\r\033[K\033[1;36m{char}\033[0m "
                    f"\033[1;33m[{self.worker_name}]\033[0m "
                    f"\033[90m⏳ Thinking & generating... \033[1;37m({elapsed:.1f}s)\033[0m"
                )
                sys.stdout.write(status_text)
                sys.stdout.flush()
                self.has_active_text = True
                idx += 1

            time.sleep(0.08)

    def run(self) -> int:
        """Execute the worker process with live streaming and visual activity."""
        self._init_workspace_files()
        self._print_header()
        self.start_time = time.time()
        self.last_stream_time = self.start_time

        env = os.environ.copy()
        home = os.path.expanduser("~")
        path_extras = [
            os.path.join(home, ".local/bin"),
            os.path.join(home, ".nvm/versions/node/v24.14.1/bin"),
        ]
        current_path = env.get("PATH", "")
        env["PATH"] = ":".join(path_extras + [current_path])
        env["PYTHONUNBUFFERED"] = "1"

        try:
            self.process = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env
            )
        except Exception as ex:
            sys.stdout.write(f"\n\033[1;31m✗ Failed to launch command: {ex}\033[0m\n")
            sys.stdout.flush()
            return 1

        if self.pid_file:
            try:
                with open(self.pid_file, "w", encoding="utf-8") as f:
                    f.write(str(self.process.pid))
            except Exception:
                pass

        # Start background spinner & workspace monitor
        spinner_thread = threading.Thread(target=self._spinner_loop, daemon=True)
        spinner_thread.start()

        # Read stdout line by line as it is emitted
        try:
            for line in iter(self.process.stdout.readline, ""):
                if not line:
                    break
                self._handle_stdout_line(line)
        except Exception:
            pass

        self.process.wait()
        self.exit_code = self.process.returncode or 0
        self.is_running = False
        spinner_thread.join(timeout=0.2)

        elapsed = time.time() - self.start_time
        if self.has_active_text:
            sys.stdout.write("\r\033[K")
            self.has_active_text = False

        # Persist full output to out_file
        full_text = "".join(self.accumulated_output)
        try:
            with open(self.out_file, "w", encoding="utf-8") as f:
                f.write(full_text)
        except Exception:
            pass

        if self.pid_file and os.path.exists(self.pid_file):
            try:
                os.remove(self.pid_file)
            except Exception:
                pass

        # Final completion banner
        if self.exit_code == 0:
            sys.stdout.write(f"\n\033[1;32m✓ Task #{self.task_id} completed successfully in {elapsed:.1f}s\033[0m\n")
        else:
            sys.stdout.write(f"\n\033[1;31m✗ Task #{self.task_id} exited with code {self.exit_code} ({elapsed:.1f}s)\033[0m\n")
        sys.stdout.flush()

        return self.exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Live worker terminal streaming runner")
    parser.add_argument("--out", required=True, help="Path to write full output log")
    parser.add_argument("--cwd", required=True, help="Working directory")
    parser.add_argument("--pid-file", default=None, help="Path to write process PID")
    parser.add_argument("--title", default="Task Execution", help="Task title")
    parser.add_argument("--worker", default="worker", help="Worker name")
    parser.add_argument("--role", default="worker", help="Role name")
    parser.add_argument("--task-id", default="1", help="Task ID")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Worker command and arguments")

    args = parser.parse_args()
    raw_cmd = args.command
    if raw_cmd and raw_cmd[0] == "--":
        raw_cmd = raw_cmd[1:]

    if not raw_cmd:
        sys.stderr.write("Error: No command specified to run\n")
        sys.exit(1)

    runner = LiveWorkerStreamer(
        cmd=raw_cmd,
        cwd=args.cwd,
        out_file=args.out,
        pid_file=args.pid_file,
        title=args.title,
        worker_name=args.worker,
        role=args.role,
        task_id=args.task_id
    )
    code = runner.run()
    sys.exit(code)


if __name__ == "__main__":
    main()
