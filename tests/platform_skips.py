"""The one list of tests that are skipped on an operating system, each with its reason.

Foreman targets Linux, macOS and Windows, and CI runs every test on all three. A test may only be skipped on a
platform through this list, and the list must not grow (test_platform checks MAX_SKIPS): fix the test or the code
instead, and lower MAX_SKIPS when an entry goes. Tests of the platform layer itself (test_platform) are never skipped.
"""

import sys
import unittest

# module or test id -> {platform: reason}. Platforms as in sys.platform: "win32", "darwin", "linux".
SKIPS = {
    "test_paste": {"win32": "drives the chat's line editor through a pseudo-terminal (pty, termios); the terminal "
                            "chat is not supported on Windows (decision D10)"},
}
MAX_SKIPS = 1


def reason(test_id: str) -> str:
    """Why `test_id` is skipped on this platform, or "" when it runs here."""
    return SKIPS.get(test_id, {}).get(sys.platform, "")


def skip_module(name: str) -> None:
    """Call first thing in a test module that can't run on this platform (before platform-specific imports)."""
    why = reason(name)
    if why:
        raise unittest.SkipTest(why)
