"""A stand-in for an AI CLI, run as a real separate process (unlike MockProvider, which runs in-process). It
exercises what differs between operating systems: process start, output decoding, process trees and their
termination.

    fake_cli.py stream               JSON lines like Claude's stream-json, with non-ASCII text, then exit 0
    fake_cli.py tree <pidfile>       start a grandchild that writes its pid to <pidfile>, then wait forever
    fake_cli.py hold-lock <path> <task>   take a FileLock, write holder details, print "ready", wait
"""

import json
import os
import subprocess
import sys
import time

TEXT = "✔ Größe — 日本語 — ünïcödé"


def main() -> int:
    mode = sys.argv[1]
    if mode == "stream":
        out = sys.stdout.buffer  # bytes in UTF-8, whatever this machine's code page is: what real CLIs print
        for event in ({"type": "system", "subtype": "init", "session_id": "fake-session-1"},
                      {"type": "assistant", "message": {"content": [{"type": "text", "text": TEXT}]}},
                      {"type": "result", "result": TEXT, "is_error": False}):
            out.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
            out.flush()
        return 0
    if mode == "tree":
        code = (f"import os, time\nwith open({sys.argv[2]!r} + '.tmp', 'w') as f: f.write(str(os.getpid()))\n"
                f"os.replace({sys.argv[2]!r} + '.tmp', {sys.argv[2]!r})\ntime.sleep(120)\n")
        subprocess.Popen([sys.executable, "-c", code])
        time.sleep(120)
        return 0
    if mode == "hold-lock":
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
        from orchestrator.oscompat import FileLock
        lock = FileLock(sys.argv[2])
        if not lock.try_acquire():
            print("busy", flush=True)
            return 1
        lock.write_info({"pid": os.getpid(), "task": sys.argv[3]})
        print("ready", flush=True)
        time.sleep(120)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
