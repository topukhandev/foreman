"""CI entry point: the whole suite, with a watchdog, and failures reported as GitHub annotations.

    python tests/ci_run.py

A test that hangs makes the run print every thread's stack (and which test it was) and fail after WATCHDOG seconds,
instead of holding the job until GitHub's 6-hour limit. On GitHub Actions each failure is also printed as an
annotation, so it shows on the run's page (and through the public API) without downloading logs.
"""

import faulthandler
import os
import sys
import tempfile
import threading
import unittest

WATCHDOG = int(os.environ.get("FOREMAN_TEST_WATCHDOG", "1200"))
GITHUB = bool(os.environ.get("GITHUB_ACTIONS"))


def _data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _prop(text: str) -> str:
    return _data(text).replace(":", "%3A").replace(",", "%2C")


def annotate(title: str, message: str) -> None:
    if GITHUB:
        print(f"::error title={_prop(title)}::{_data(message[-4000:])}", flush=True)


class Result(unittest.TextTestResult):
    current = ""

    def startTest(self, test):
        Result.current = test.id()
        super().startTest(test)


def hung() -> None:
    with tempfile.TemporaryFile("w+") as f:
        faulthandler.dump_traceback(file=f, all_threads=True)
        f.seek(0)
        stacks = f.read()
    sys.stderr.write(f"\nWATCHDOG: no result after {WATCHDOG}s, in {Result.current}\n{stacks}\n")
    annotate(f"hang in {Result.current}", stacks)
    sys.stderr.flush()
    os._exit(1)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows pipes default to the ANSI code page: "✔" in a report would crash
        stream.reconfigure(encoding="utf-8", errors="replace")
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    timer = threading.Timer(WATCHDOG, hung)
    timer.daemon = True
    timer.start()
    try:
        suite = unittest.defaultTestLoader.discover(here)
        result = unittest.TextTestRunner(resultclass=Result, verbosity=1).run(suite)
    except BaseException as e:  # the runner itself failed: say so where it can be seen
        annotate("the test run crashed", f"{type(e).__name__}: {e}")
        raise
    timer.cancel()
    problems = result.failures + result.errors
    if problems:  # first: GitHub shows only the first few annotations of a step
        annotate(f"{len(result.failures)} failed, {len(result.errors)} errors of {result.testsRun} tests",
                 "\n".join(t.id() if hasattr(t, "id") else str(t) for t, _ in problems))
    for test, trace in problems[:40]:
        annotate(test.id() if hasattr(test, "id") else str(test), trace)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
