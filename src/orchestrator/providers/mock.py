"""Scripted provider for tests and offline dry runs. Behaviour is supplied as a handler function."""

import threading
import time
from typing import Callable, List, Optional

from orchestrator.config import ProviderSettings
from orchestrator.providers.base import Capabilities, Provider, RunRequest, RunResult, SessionInfo

Handler = Callable[[RunRequest], RunResult]


class MockProvider(Provider):
    name = "mock"

    def __init__(self, settings: Optional[ProviderSettings] = None, handler: Optional[Handler] = None,
                 name: str = "mock", sessions: Optional[List[SessionInfo]] = None,
                 capabilities: Optional[Capabilities] = None):
        super().__init__(settings or ProviderSettings(name=name, command=""))
        self.name = name
        self.handler = handler or (lambda req: RunResult(True, "ok", session_id="mock-session"))
        self.sessions = sessions or []
        self.capabilities = capabilities or Capabilities(resume=True, fork=True, structured_output=True,
                                                         session_discovery=True, enforced_read_only=True)
        self.calls: List[RunRequest] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def available(self):
        return True, "mock"

    def run(self, req: RunRequest) -> RunResult:
        with self._lock:
            self.calls.append(req)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        start = time.time()
        try:
            res = self.handler(req)
        finally:
            with self._lock:
                self.active -= 1
        res.duration = res.duration or (time.time() - start)
        return res

    def discover_sessions(self, cwd: str) -> List[SessionInfo]:
        return [s for s in self.sessions if s.cwd == cwd]
