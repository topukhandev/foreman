"""`foreman serve --stdio`: the application API (api.py) as JSON-RPC 2.0 over stdin/stdout, for the desktop app.

One JSON object per line, UTF-8, read and written as bytes. Requests are handled concurrently (a slow one, e.g. a
project scan, doesn't hold up the rest); responses carry the request id. Events from subscribed tasks are sent as
notifications. Nothing here runs AI work: the API starts task runners for that. The process ends when stdin closes.
Stdout carries the protocol only: anything else printed goes to stderr (serve_stdio).
"""

import inspect
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, BinaryIO, Dict, Optional

from orchestrator.api import Api, ApiError
from orchestrator.feed import Feed

METHODS = {
    "system.hello": "hello", "system.doctor": "doctor",
    "tasks.list": "tasks_list", "tasks.get": "tasks_get", "tasks.submit": "tasks_submit",
    "tasks.answer": "tasks_answer", "tasks.resume": "tasks_resume", "tasks.next": "tasks_next",
    "tasks.stop": "tasks_stop", "tasks.close": "tasks_close", "tasks.events": "tasks_events",
    "projects.list": "projects_list", "projects.get": "projects_get", "projects.scan": "projects_scan",
    "lifecycle.get": "lifecycle_get", "sessions.list": "sessions_list",
    "suggestions.list": "suggestions_list", "suggestions.accept": "suggestions_accept",
    "suggestions.dismiss": "suggestions_dismiss",
    "config.get": "config_get", "config.set_on_close": "config_set_on_close",
}
FEED_POLL = 0.25
API_ERROR, PARSE_ERROR, INVALID_REQUEST, NO_METHOD, INVALID_PARAMS = -32000, -32700, -32600, -32601, -32602


class RpcServer:
    def __init__(self, api: Api, out: BinaryIO, workers: int = 4):
        self.api = api
        self.feed = Feed(api.eng)
        self.out = out
        self.ready = False  # system.hello comes first
        self._write_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._stop = threading.Event()

    # ------------------------------------------------------------------ framing
    def send(self, obj: Dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8") + b"\n"
        with self._write_lock:
            self.out.write(data)
            self.out.flush()

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    # ------------------------------------------------------------------ requests
    def handle(self, msg: Any) -> Optional[Dict[str, Any]]:
        """The response to one request (None for a notification, which gets no answer)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "not a JSON-RPC 2.0 request",
                          "invalid")
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if not isinstance(params, dict):
            return _error(mid, INVALID_PARAMS, "params must be an object", "invalid")
        try:
            result = self.call(method, params)
        except ApiError as e:
            result = _error(mid, API_ERROR, str(e), e.kind, **e.data)
            return result if mid is not None else None
        except _NoMethod:
            return _error(mid, NO_METHOD, f"no method {method}", "invalid") if mid is not None else None
        except _BadParams as e:
            return _error(mid, INVALID_PARAMS, str(e), "invalid") if mid is not None else None
        except Exception as e:  # never let one request take the server down
            return _error(mid, API_ERROR, f"{type(e).__name__}: {e}", "internal") if mid is not None else None
        return {"jsonrpc": "2.0", "id": mid, "result": result} if mid is not None else None

    def call(self, method: str, params: Dict[str, Any]) -> Any:
        if method == "system.hello":
            result = self._invoke(self.api.hello, params)
            self.ready = True
            return result
        if not self.ready:
            raise ApiError("invalid", "call system.hello first")
        if method == "events.subscribe":
            return self._invoke(self.feed.subscribe, params)
        if method == "events.unsubscribe":
            return self.feed.unsubscribe()
        name = METHODS.get(method)
        if not name:
            raise _NoMethod(method)
        return self._invoke(getattr(self.api, name), params)

    @staticmethod
    def _invoke(fn, params: Dict[str, Any]) -> Any:
        try:
            inspect.signature(fn).bind(**params)
        except TypeError as e:
            raise _BadParams(str(e))
        return fn(**params)

    # ------------------------------------------------------------------ loop
    def _feed_loop(self) -> None:
        while not self._stop.wait(FEED_POLL):
            try:
                for method, params in self.feed.poll():
                    self.notify(method, params)
            except Exception as e:  # a failed poll is reported and the next one tries again
                print(f"feed: {type(e).__name__}: {e}", file=sys.stderr)

    def serve(self, inp: BinaryIO) -> None:
        feeder = threading.Thread(target=self._feed_loop, daemon=True)
        feeder.start()
        try:
            for raw in inp:
                if not raw.strip():
                    continue
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self.send(_error(None, PARSE_ERROR, "parse error", "invalid"))
                    continue
                self._pool.submit(self._answer, msg)
        finally:
            self._stop.set()
            self._pool.shutdown(wait=True)
            feeder.join(2)

    def _answer(self, msg: Any) -> None:
        response = self.handle(msg)
        if response is not None:
            self.send(response)


class _NoMethod(Exception):
    pass


class _BadParams(Exception):
    pass


def _error(mid: Any, code: int, message: str, kind: str, **data: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message, "data": {"kind": kind, **data}}}


def serve_stdio() -> None:
    """Entry point: protect the protocol stream, then serve until stdin closes."""
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())  # a stray print (ours or a library's) can't corrupt it
    sys.stdout = sys.stderr
    from orchestrator.app import build_engine
    api = Api(build_engine(out=lambda _m: None, actor="desktop"), actor="desktop")
    RpcServer(api, protocol).serve(sys.stdin.buffer)
