"""Command-line surface: version, shell completion and its agreement with the real subcommands."""

import contextlib
import io
import subprocess
import unittest

from orchestrator import __version__, completion
from orchestrator.cli import SUBCOMMANDS, main


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(list(argv))
    return code, buf.getvalue()


class VersionTest(unittest.TestCase):
    def test_version_flag_and_command(self):
        for arg in ("--version", "-V", "version"):
            code, text = run(arg)
            self.assertEqual(code, 0)
            self.assertTrue(text.startswith(f"foreman {__version__}"), text)


class CompletionTest(unittest.TestCase):
    INTERNAL = {"run-task", "-h", "--help"}

    def test_every_public_subcommand_is_completed(self):
        listed = {c for c, _ in completion.COMMANDS}
        self.assertEqual(listed, SUBCOMMANDS - self.INTERNAL)

    def test_scripts_are_valid_and_offer_commands_and_modes(self):
        for shell in completion.SHELLS:
            code, text = run("completion", shell)
            self.assertEqual(code, 0)
            for word in ("lifecycle", "mcp", "deployment"):
                self.assertIn(word, text)
        bash = completion.bash()
        self.assertEqual(subprocess.run(["bash", "-n"], input=bash, text=True).returncode, 0)

    def test_zsh_descriptions_are_quote_safe(self):
        for _, desc in completion.COMMANDS:
            self.assertNotIn("'", desc)


if __name__ == "__main__":
    unittest.main()
