"""Base adapter for running AI CLI worker commands securely with process group isolation."""

from dataclasses import dataclass
import os
import shlex
import signal
import subprocess
import sys
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

    def __init__(self, name: str, base_command: str, timeout_sec: int = 300, visual_mode: bool = True, effort: str = "medium"):
        self.name = name
        self.base_command = base_command
        self.timeout_sec = timeout_sec
        self.visual_mode = visual_mode
        self.effort = effort if effort in ("low", "medium", "high") else "medium"

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
        progress_label: Optional[str] = None,
        stream_output: bool = False
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

        # 1. If visual mode is enabled and desktop GUI is available, launch visual terminal window!
        if getattr(self, "visual_mode", True):
            try:
                from orchestrator.terminal import can_launch_visual_terminal, launch_visual_worker
                if can_launch_visual_terminal():
                    role = task_id.split("_")[0] if "_" in task_id else "worker"
                    title = f"Foreman Worker: [{self.name}] Task #{task_id}"
                    lbl = progress_label or self.name
                    vis_res = launch_visual_worker(
                        cmd=cmd,
                        cwd=cwd,
                        title=title,
                        worker_name=self.name,
                        task_id=task_id,
                        role=role,
                        timeout_sec=self.timeout_sec,
                        show_progress=show_progress,
                        progress_label=lbl,
                        session_id=session_id
                    )
                    if vis_res is not None:
                        return vis_res
            except Exception:
                # If visual terminal spawn fails for any reason, fall through safely to headless
                pass

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

            if stream_output and sys.stdout.isatty():
                lbl = progress_label or self.name
                sys.stdout.write(f"\033[1;36m▶ [{lbl}]\033[0m Streaming live response...\n\n")
                sys.stdout.flush()

                collected_out: List[str] = []
                collected_err: List[str] = []

                if os.name != "nt":
                    import fcntl
                    for pipe in (process.stdout, process.stderr):
                        if pipe is not None:
                            try:
                                fl = fcntl.fcntl(pipe.fileno(), fcntl.F_GETFL)
                                fcntl.fcntl(pipe.fileno(), fcntl.F_SETFL, fl | os.O_NONBLOCK)
                            except Exception:
                                pass

                while True:
                    elapsed = time.time() - start_time
                    if elapsed > self.timeout_sec:
                        self._kill_process_tree(process)
                        sys.stdout.write(f"\n\033[1;31m✗ [{lbl}]\033[0m Timed out after {self.timeout_sec}s\n")
                        sys.stdout.flush()
                        return ExecutionResult(
                            success=False,
                            output="".join(collected_out),
                            error=f"Execution timed out after {self.timeout_sec} seconds. Process tree terminated.",
                            exit_code=-1,
                            duration_sec=elapsed
                        )

                    ret = process.poll()
                    has_data = False

                    if process.stdout is not None:
                        try:
                            chunk = os.read(process.stdout.fileno(), 4096)
                            if chunk:
                                has_data = True
                                chunk_str = chunk.decode("utf-8", errors="replace")
                                collected_out.append(chunk_str)
                                sys.stdout.write(chunk_str)
                                sys.stdout.flush()
                        except (BlockingIOError, InterruptedError):
                            pass
                        except Exception:
                            pass

                    if process.stderr is not None:
                        try:
                            err_chunk = os.read(process.stderr.fileno(), 4096)
                            if err_chunk:
                                has_data = True
                                collected_err.append(err_chunk.decode("utf-8", errors="replace"))
                        except (BlockingIOError, InterruptedError):
                            pass
                        except Exception:
                            pass

                    if ret is not None and not has_data:
                        break

                    if not has_data:
                        time.sleep(0.02)

                exit_code = process.returncode if process.returncode is not None else 0
                duration = time.time() - start_time
                if exit_code == 0:
                    sys.stdout.write(f"\n\n\033[1;32m✓ [{lbl}]\033[0m Responded in {duration:.1f}s\n\n")
                else:
                    sys.stdout.write(f"\n\n\033[1;31m✗ [{lbl}]\033[0m Exited with code {exit_code} ({duration:.1f}s)\n\n")
                sys.stdout.flush()

                out_str = "".join(collected_out).strip()
                err_str = "".join(collected_err).strip() if exit_code != 0 else None
                for pipe in (process.stdout, process.stderr):
                    if pipe:
                        try:
                            pipe.close()
                        except Exception:
                            pass
                return ExecutionResult(
                    success=(exit_code == 0),
                    output=out_str,
                    error=err_str,
                    exit_code=exit_code,
                    duration_sec=duration
                )

            elif show_progress and sys.stdout.isatty():
                spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
                lbl = progress_label or self.name
                idx = 0
                import shutil
                while process.poll() is None:
                    elapsed = time.time() - start_time
                    if elapsed > self.timeout_sec:
                        break
                    cols = shutil.get_terminal_size(fallback=(80, 24)).columns
                    spin_ch = spinner[idx % len(spinner)]
                    desc = f"Thinking... ({elapsed:.1f}s)"
                    if len(f"{spin_ch} [{lbl}] {desc}") >= cols - 1:
                        desc = f"({elapsed:.1f}s)"
                    sys.stdout.write(f"\r\033[K\033[1;36m{spin_ch} [{lbl}]\033[0m {desc}")
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
