"""Base adapter for running AI CLI worker commands securely."""

from dataclasses import dataclass
import os
import shlex
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
    """Base class for spawning local AI CLI tools with security and timeout guards."""

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

    def run(
        self,
        prompt: str,
        cwd: str,
        task_id: str = "general",
        session_id: Optional[str] = None,
        extra_flags: Optional[List[str]] = None
    ) -> ExecutionResult:
        """
        Execute the CLI command securely inside cwd with strict timeout.
        Never uses shell=True.
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
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=os.environ.copy()
            )

            try:
                stdout, stderr = process.communicate(timeout=self.timeout_sec)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                return ExecutionResult(
                    success=False,
                    output=stdout or "",
                    error=f"Execution timed out after {self.timeout_sec} seconds.",
                    exit_code=-1,
                    duration_sec=time.time() - start_time
                )

            duration = time.time() - start_time
            success = (exit_code == 0)
            err_msg = stderr.strip() if not success else None

            return ExecutionResult(
                success=success,
                output=stdout.strip() if stdout else "",
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
