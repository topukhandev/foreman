"""Test fixtures: a throwaway multi-project workspace and a scripted provider."""

import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from orchestrator.brain import DOC_SCHEMA, PLAN_SCHEMA, REVIEW_SCHEMA, STEP_SCHEMA  # noqa: E402
from orchestrator.config import load_config  # noqa: E402
from orchestrator.engine import Engine  # noqa: E402
from orchestrator.providers.base import RunRequest, RunResult  # noqa: E402
from orchestrator.providers.mock import MockProvider  # noqa: E402
from orchestrator.registry import Registry  # noqa: E402
from orchestrator.store import Store  # noqa: E402

PASSING_TEST = "const assert = require('assert');\nassert.ok(true);\nconsole.log('ok');\n"


def git(path: str, *args: str) -> str:
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, check=True).stdout


def make_repo(root: str, name: str, files: Dict[str, str]) -> str:
    path = os.path.join(root, name)
    os.makedirs(path)
    for rel, content in files.items():
        fp = os.path.join(path, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w") as f:
            f.write(content)
    git(path, "init", "-q")
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return path


def node_pkg(name: str, test: str = "node test.js") -> str:
    return json.dumps({"name": name, "version": "1.0.0", "scripts": {"test": test}})


class Workspace:
    """admin-ui (frontend) -> orders-api (backend) <- e2e-tests (integration); notes-app unrelated."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="foreman_test_")
        self.root = os.path.join(self.tmp, "projects")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.root)
        os.makedirs(self.home)
        self.api = make_repo(self.root, "orders-api", {
            "package.json": node_pkg("orders-api"),
            "README.md": "# orders-api\n\nOrders microservice. Serves GET /orders with id, total.\n",
            "server.js": "module.exports = () => [{id: 1, total: 10}];\n",
            "test.js": PASSING_TEST,
        })
        self.ui = make_repo(self.root, "admin-ui", {
            "package.json": node_pkg("admin-ui"),
            "README.md": f"# admin-ui\n\nAdmin panel UI. Orders page reads the orders API from {self.api}.\n",
            "src/orders.js": "export const show = (o) => o.total;\n",
            "test.js": PASSING_TEST,
        })
        self.e2e = make_repo(self.root, "e2e-tests", {
            "package.json": node_pkg("e2e-tests"),
            "README.md": "# e2e-tests\n\nIntegration scenarios for the admin panel and the orders service.\n",
            "test.js": PASSING_TEST,
        })
        self.notes = make_repo(self.root, "notes-app", {
            "pyproject.toml": "[project]\nname='notes'\n",
            "README.md": "# notes\n\nA personal notes app.\n",
        })
        with open(os.path.join(self.home, "config.yaml"), "w") as f:
            f.write(f"roots: [{self.root}]\nbrain: claude\nproviders:\n  claude: {{command: claude}}\n"
                    "  opencode: {command: opencode}\nlimits: {max_parallel: 4, max_fix_rounds: 2}\n"
                    "ui: {display: headless, notify: true}\n")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Script:
    """Scripted brain + workers. Each hook receives the RunRequest and returns structured data."""

    def __init__(self):
        self.plan: Callable[[RunRequest], Dict] = lambda req: {"intent": "change", "title": "t", "projects": [],
                                                               "steps": [], "questions": []}
        self.step: Callable[[RunRequest], Dict] = lambda req: {"status": "done", "summary": "did it"}
        self.review: Callable[[RunRequest], Dict] = lambda req: {"verdict": "approve", "summary": "looks right", "issues": []}
        self.document: Callable[[RunRequest], Dict] = lambda req: {"title": "Doc", "document": "# Doc\n\nBody.",
                                                                   "summary": "Wrote the document."}
        self.fail_steps = False
        self.delay = 0.0
        self.log: List[str] = []
        self._ids = itertools.count(1)

    def handler(self, provider_name: str):
        def run(req: RunRequest) -> RunResult:
            if req.schema is PLAN_SCHEMA:
                self.log.append(f"{provider_name}:plan")
                return RunResult(True, "", session_id=f"{provider_name}-brain", data=self.plan(req))
            if req.schema is DOC_SCHEMA:
                self.log.append(f"{provider_name}:document")
                return RunResult(True, "", session_id=f"{provider_name}-doc", data=self.document(req))
            if req.schema is REVIEW_SCHEMA:
                self.log.append(f"{provider_name}:review")
                return RunResult(True, "", session_id=f"{provider_name}-rev", data=self.review(req))
            assert req.schema is STEP_SCHEMA
            self.log.append(f"{provider_name}:step:{os.path.basename(req.cwd)}")
            if self.delay:
                time.sleep(self.delay)
            if self.fail_steps is True or (callable(self.fail_steps) and self.fail_steps(provider_name, req)):
                return RunResult(False, error=f"{provider_name} crashed")
            data = self.step(req)
            sid = req.session_id if (req.session_id and not req.fork) else f"{provider_name}-s{next(self._ids)}"
            return RunResult(True, data.get("summary", ""), session_id=sid, data=data)
        return run


def make_engine(ws: Workspace, script: Script, providers: Optional[List[str]] = None, sessions=None,
                notes: Optional[List] = None) -> Engine:
    cfg = load_config(ws.home)
    provs = {}
    for name in providers or ["claude", "opencode"]:
        provs[name] = MockProvider(name=name, handler=script.handler(name),
                                   sessions=(sessions or {}).get(name, []))
    notes = notes if notes is not None else []
    return Engine(cfg, provs, Registry(cfg.home, cfg.roots), Store(cfg.home), out=lambda m: None, visual=False,
                  notifier=lambda t, b: notes.append((t, b)))


def write(path: str, rel: str, content: str) -> None:
    fp = os.path.join(path, rel)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w") as f:
        f.write(content)
