"""The front-end protocol (src/orchestrator/surface.py): every operation is in the core and reaches both the CLI
and the desktop app, or the table says why not. A new API method, CLI command or app call that isn't accounted for
fails here."""

import os
import re
import unittest

import helpers  # noqa: F401 (puts src on the path)

from orchestrator import surface
from orchestrator.chat import COMMANDS as CHAT_COMMANDS
from orchestrator.cli import SUBCOMMANDS
from orchestrator.rpc_server import METHODS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_SRC = os.path.join(ROOT, "desktop", "src")
STREAMING = {"events.subscribe", "events.unsubscribe"}  # handled by the RPC server itself, not in METHODS


def app_source() -> str:
    parts = []
    for folder, _, files in os.walk(APP_SRC):
        for f in files:
            if f.endswith((".js", ".svelte")) and f != "fake.js":  # the browser demo's fake server doesn't count
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    parts.append(fh.read())
    return "\n".join(parts)


class ProtocolTest(unittest.TestCase):
    def test_every_api_method_is_listed_and_nothing_else(self):
        self.assertEqual(set(surface.OPERATIONS), set(METHODS) | STREAMING)

    def test_every_cli_form_exists(self):
        slash = {c for c, _ in CHAT_COMMANDS}
        for method, (cli, _) in surface.OPERATIONS.items():
            if cli.startswith("-:"):
                continue
            if cli.startswith("chat /"):
                self.assertIn(cli.split()[1], slash, f"{method}: the chat has no {cli.split()[1]}")
                continue
            self.assertTrue(cli.startswith("foreman "), f"{method}: write the CLI form as `foreman <command>`")
            self.assertIn(cli.split()[1], SUBCOMMANDS, f"{method}: `{cli}` is not a CLI command")

    def test_every_cli_command_is_an_operation_or_explained(self):
        offered = {cli.split()[1] for cli, _ in surface.OPERATIONS.values() if cli.startswith("foreman ")}
        unexplained = set(SUBCOMMANDS) - offered - set(surface.CLI_ONLY)
        self.assertEqual(unexplained, set(), "add these commands to surface.OPERATIONS or surface.CLI_ONLY")

    def test_the_app_calls_what_it_offers(self):
        if not os.path.isdir(APP_SRC):
            self.skipTest("no desktop app in this checkout")
        src = app_source()
        called = set(re.findall(r"""["']((?:system|tasks|projects|lifecycle|sessions|conversations|suggestions|config|models|events)\.[a-z_]+)["']""", src))
        for method, (_, app) in surface.OPERATIONS.items():
            if app.startswith("-:"):
                continue
            if app.startswith("via "):
                other = app.split()[1].rstrip(":")
                self.assertIn(other, called, f"{method}: offered via {other}, which the app doesn't call")
                continue
            self.assertIn(method, called, f"{method}: listed as offered by the app ({app}), but the app never calls it")
        self.assertEqual(called - set(surface.OPERATIONS), set(), "the app calls methods the protocol doesn't list")

    def test_reasons_are_written(self):
        for method, forms in surface.OPERATIONS.items():
            for form in forms:
                if form.startswith("-:"):
                    self.assertGreater(len(form), 10, f"{method}: say why it is left out")
