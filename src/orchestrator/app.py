"""Wiring shared by every interface (CLI, MCP). Interfaces stay thin; all behaviour lives in the engine."""

import os
import subprocess
import sys
import time
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
