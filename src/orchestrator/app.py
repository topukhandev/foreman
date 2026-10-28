"""Wiring shared by every interface (CLI, MCP). Interfaces stay thin; all behaviour lives in the engine."""

import os
import subprocess
import sys
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from orchestrator.config import load_config
from orchestrator.engine import Engine
from orchestrator.oscompat import python_command, spawn_detached
from orchestrator.providers import build_providers
from orchestrator.registry import Registry
from orchestrator.store import Store


def build_engine(visual: Optional[bool] = None, out: Callable[[str], None] = print,
                 home: Optional[str] = None, actor: str = "cli") -> Engine:
    cfg = load_config(home)
    return Engine(cfg, build_providers(cfg), Registry(cfg.home, cfg.roots), Store(cfg.home), out=out, visual=visual,
                  actor=actor)


def entry_command() -> List[str]:
    """Command that launches this orchestrator, usable from other tools' configs (MCP registration, background runs)."""
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "foreman")
    if os.path.isfile(script):
        return [sys.executable, script]
    return [sys.executable, "-m", "orchestrator.cli"]


def spawn_task(home: str, task_id: str, visual: Optional[bool] = None, run: Optional[str] = None,
               plan_only: bool = False) -> subprocess.Popen:
    """Execute a prepared task in a detached process (survives the caller, e.g. an MCP tool call timing out). `run`
    is the token the task was queued with: the runner runs the task only for that token, so a duplicate start
    can't run it twice. plan_only: a dry run (plan, change nothing)."""
    cmd = python_command() + entry_command()[1:] + ["run-task", task_id] + (["--run", run] if run else []) \
        + (["--plan-only"] if plan_only else [])
    if visual is not None:
        cmd.append("--visual" if visual else "--headless")
    log = open(os.path.join(home, "tasks", task_id, "runner.log"), "a")
    return spawn_detached(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, env={**os.environ, "FOREMAN_HOME": home})


# Variables that describe the shell itself, not the environment programs need.
_SHELL_ONLY = {"PWD", "OLDPWD", "SHLVL", "_", "PS1", "PS2", "PS4", "PROMPT", "RPROMPT", "TERM", "COLUMNS", "LINES",
               "HISTFILE", "HISTSIZE", "SAVEHIST", "ZDOTDIR"}


def login_shell_env(timeout: float = 10.0, shell: Optional[str] = None) -> Dict[str, str]:
    """macOS and Linux: the environment your login shell sets up (PATH from ~/.zshrc or nvm, keys you export there).
    An app started from the desktop doesn't get it, so the desktop app's Foreman asks the shell once at startup.
    Whatever the shell prints while starting is ignored (the environment comes between two markers), and a shell
    that doesn't answer within `timeout` is given up on. {} on Windows (programs get the user's environment there)
    or when it fails."""
    if os.name == "nt":
        return {}
    shell = shell or os.environ.get("SHELL") or "/bin/sh"
    marker = "FOREMAN_ENV_" + uuid.uuid4().hex
    script = f"printf '%s' {marker}; env -0; printf '%s' {marker}"
    try:
        res = subprocess.run([shell, "-l", "-i", "-c", script], stdin=subprocess.DEVNULL, capture_output=True,
                             timeout=timeout, start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        return {}
    parts = res.stdout.split(marker.encode())
    if len(parts) < 3:
        return {}
    env = {}
    for item in parts[1].split(b"\0"):
        key, sep, value = item.decode("utf-8", errors="replace").partition("=")
        if sep and key and key not in _SHELL_ONLY:
            env[key] = value
    return env


def apply_login_env(found: Dict[str, str], environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Merge the login shell's environment into this process's: its PATH entries come first (then the ones only this
    process had), and its other variables are added where this process has none. Returns what was added or changed."""
    environ = os.environ if environ is None else environ
    changed = {}
    if found.get("PATH"):
        mine = [p for p in environ.get("PATH", "").split(os.pathsep) if p]
        theirs = [p for p in found["PATH"].split(os.pathsep) if p]
        path = os.pathsep.join(dict.fromkeys(theirs + mine))
        if path != environ.get("PATH"):
            environ["PATH"] = changed["PATH"] = path
    for key, value in found.items():
        if key != "PATH" and key not in environ:
            environ[key] = changed[key] = value
    return changed


def wait_for(engine: Engine, task_id: str, seconds: int) -> Tuple[Dict, bool]:
    """Poll a background task until it stops running or the wait expires. Returns (task, finished)."""
    deadline = time.time() + max(0, seconds)
    started = time.time()
    while True:
        task = engine.reconcile(engine.store.load(task_id))
        running = task["status"] in ("queued", "running", "planning") or engine.is_running(task_id)
        if not running and time.time() - started > 0.5:
            return task, True
        if time.time() >= deadline:
            return task, False
        time.sleep(1.0)


def read_report(engine: Engine, task_id: str) -> str:
    try:
        with open(engine.store.path(task_id, "report.md"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
