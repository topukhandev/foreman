"""Base adapter for running AI CLI worker commands securely with process group isolation."""

from dataclasses import dataclass
import os
import shlex
import signal
import subprocess
import time
from typing import List, Optional


@dataclass
class ExecutionResult:
    success: bool
    output: str
    error: Optional[str] = None
    exit_code: int = 0
    duration_sec: float = 0.0


class BaseCLIAdapter:
    """Base class for spawning local AI CLI tools with security, timeout, and process group guards."""

    def __init__(self, name: str, base_command: str, timeout_sec: int = 300):
        self.name = name
        self.base_command = base_command
        self.timeout_sec = timeout_sec

    def build_command(self, prompt: str, session_id: Optional[str] = None, extra_flags: Optional[List[str]] = None) -> List[str]:
        """Convert base_command and prompt into argument list."""
        cmd_parts = shlex.split(self.base_command)
        if extra_flags:
            cmd_parts.extend(extra_flags)
        cmd_parts.append(prompt)
        return cmd_parts

    def _kill_process_tree(self, process: subprocess.Popen) -> None:
        """Kill entire process group to prevent zombie or orphaned subprocesses."""
        try:
            if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                try:
                    pgid = os.getpgid(process.pid)
                    os.killpg(pgid, signal.SIGTERM)
                    time.sleep(0.1)
                    os.killpg(pgid, signal.SIGKILL)
                    return
                except (ProcessLookupError, PermissionError):
                    pass
            process.kill()
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def run(
        self,
        prompt: str,
        cwd: str,
        task_id: str = "general",
        session_id: Optional[str] = None,
        extra_flags: Optional[List[str]] = None,
        show_progress: bool = False,
        progress_label: Optional[str] = None
    ) -> ExecutionResult:
        """
        Execute the CLI command securely inside cwd with strict timeout.
        Uses process group isolation and direct argument arrays (no shell=True).
        """
        if not os.path.isdir(cwd):
            return ExecutionResult(
                success=False,
                output="",
                error=f"Working directory does not exist: {cwd}",
                exit_code=-1
            )

        cmd = self.build_command(prompt, session_id=session_id, extra_flags=extra_flags)
        start_time = time.time()

        try:
            # Start in new session/process group on Unix to allow killing child trees
            popen_kwargs = {
                "cwd": cwd,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "env": os.environ.copy()
            }
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True

            process = subprocess.Popen(cmd, **popen_kwargs)

            if show_progress and sys.stdout.isatty():
                spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
                lbl = progress_label or self.name
                idx = 0
                while process.poll() is None:
                    elapsed = time.time() - start_time
                    if elapsed > self.timeout_sec:
                        break
                    sys.stdout.write(f"\r\033[K\033[1;36m{spinner[idx % len(spinner)]} [{lbl}]\033[0m Thinking... ({elapsed:.1f}s)")
                    sys.stdout.flush()
                    idx += 1
                    time.sleep(0.1)

                if process.poll() is None:
                    self._kill_process_tree(process)
                    stdout, stderr = process.communicate()
                    sys.stdout.write(f"\r\033[K\033[1;31m✗ [{lbl}]\033[0m Timed out after {self.timeout_sec}s\n")
                    sys.stdout.flush()
                    return ExecutionResult(
                        success=False,
                        output=stdout or "",
                        error=f"Execution timed out after {self.timeout_sec} seconds. Process tree terminated.",
                        exit_code=-1,
                        duration_sec=time.time() - start_time
                    )

                stdout, stderr = process.communicate()
                exit_code = process.returncode
                duration = time.time() - start_time
                if exit_code == 0:
                    sys.stdout.write(f"\r\033[K\033[1;32m✓ [{lbl}]\033[0m Responded in {duration:.1f}s\n\n")
                else:
                    sys.stdout.write(f"\r\033[K\033[1;31m✗ [{lbl}]\033[0m Error after {duration:.1f}s (code {exit_code})\n\n")
                sys.stdout.flush()
            else:
                try:
                    stdout, stderr = process.communicate(timeout=self.timeout_sec)
                    exit_code = process.returncode
                except subprocess.TimeoutExpired:
                    self._kill_process_tree(process)
                    stdout, stderr = process.communicate()
                    return ExecutionResult(
                        success=False,
                        output=stdout or "",
                        error=f"Execution timed out after {self.timeout_sec} seconds. Process tree terminated.",
                        exit_code=-1,
                        duration_sec=time.time() - start_time
                    )

            duration = time.time() - start_time
            success = (exit_code == 0)
            err_msg = stderr.strip() if not success else None

            # Cap output buffer to prevent OOM on accidental massive output
            MAX_OUTPUT_CHARS = 2_000_000  # ~2MB text
            out_str = stdout.strip() if stdout else ""
            if len(out_str) > MAX_OUTPUT_CHARS:
                out_str = out_str[:MAX_OUTPUT_CHARS] + f"\n... [Output truncated at {MAX_OUTPUT_CHARS} characters]"

            return ExecutionResult(
                success=success,
                output=out_str,
                error=err_msg,
                exit_code=exit_code,
                duration_sec=duration
            )

        except FileNotFoundError as fnf:
            return ExecutionResult(
                success=False,
                output="",
                error=f"Command executable not found: {cmd[0]} ({fnf})",
                exit_code=-1,
                duration_sec=time.time() - start_time
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                output="",
                error=f"Subprocess execution error: {str(e)}",
                exit_code=-1,
                duration_sec=time.time() - start_time
            )
