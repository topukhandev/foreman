"""Which model and reasoning effort each run gets: built-in profiles per provider and kind of work, the planner's
size rating per step, the user's per-task choices, and what the CLIs are actually asked for."""

import io
import os
import unittest
from contextlib import redirect_stdout

import yaml

from helpers import Script, Workspace, make_engine
from test_lifecycle import with_mode
from test_scenarios import editing_worker, plan, step

from orchestrator import cli
from orchestrator.brain import PLAN_SCHEMA, REVIEW_SCHEMA, STEP_SCHEMA
from orchestrator.config import Choice, load_config
from orchestrator.providers.antigravity import AntigravityProvider
from orchestrator.providers.base import RunRequest, RunResult
from orchestrator.providers.claude import ClaudeProvider
from orchestrator.providers.mock import MockProvider
from orchestrator.providers.opencode import OpenCodeProvider
from orchestrator.report import render_report


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.cleanup()

    def write_config(self, extra: str):
        path = os.path.join(self.ws.home, "config.yaml")
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(text + extra)

    def test_built_in_profiles_apply_when_the_config_says_nothing(self):
        cfg = load_config(self.ws.home)
        claude = cfg.providers["claude"]
        self.assertEqual(claude.choice("standard"), Choice("claude-sonnet-5-5", "high"))
        self.assertEqual(claude.choice("heavy"), Choice("claude-sonnet-5-5", "high"), "high at most, never max")
        self.assertEqual(claude.choice("light").effort, "medium")
        self.assertEqual(claude.choice("plan").effort, "medium")
        self.assertEqual(claude.choice("review").effort, "high")
        self.assertEqual(cfg.providers["opencode"].choice("standard"), Choice(), "no profile: the CLI's own default")

    def test_the_default_config_file_gets_agy_flash_profiles(self):
        with open(os.path.join(self.ws.home, "config.yaml"), "w") as f:
            f.write("roots: []\n")
        agy = load_config(self.ws.home).providers["antigravity"]
        self.assertEqual(agy.choice("standard"), Choice("gemini-3.8-flash-high"))
        self.assertEqual(agy.choice("light"), Choice("gemini-3.8-flash-medium"))
        self.assertEqual(agy.choice("review"), Choice("gemini-3.8-flash-high"))

    def test_the_config_can_override_one_part_and_keeps_the_rest(self):
        with open(os.path.join(self.ws.home, "config.yaml"), "w") as f:
            f.write("roots: []\nproviders:\n  claude:\n    command: claude\n    profiles:\n"
                    "      heavy: {model: claude-opus-5-5}\n      light: {effort: low}\n"
                    "      standard: {effort: max-ish}\n      review: {effort: null}\n")
        claude = load_config(self.ws.home).providers["claude"]
        self.assertEqual(claude.choice("heavy"), Choice("claude-opus-5-5", "high"))
        self.assertEqual(claude.choice("light"), Choice("claude-sonnet-5-5", "low"))
        self.assertEqual(claude.choice("standard").effort, "high", "an unknown effort is ignored")
        self.assertEqual(claude.choice("review"), Choice("claude-sonnet-5-5", None), "null removes the effort")

    def test_an_explicit_model_in_the_config_still_forces_the_model(self):
        with open(os.path.join(self.ws.home, "config.yaml"), "w") as f:
            f.write("roots: []\nproviders:\n  claude:\n    command: claude\n    model: opus\n    brain_model: haiku\n")
        claude = load_config(self.ws.home).providers["claude"]
        self.assertEqual(claude.choice("light"), Choice("opus", "medium"))
        self.assertEqual(claude.choice("plan"), Choice("haiku", "medium"))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Script()
        self.weights = {}
        self.s.plan = lambda req: plan([("orders-api", "inspect")],
                                       [{**step(i, "orders-api", kind="investigate"), "weight": w}
                                        for i, w in self.weights.items()] or [step("1", "orders-api", kind="investigate")])
        self.eng = make_engine(self.ws, self.s)
        self.seen = []
        for name in self.eng.providers:
            inner = self.s.handler(name)
            self.eng.providers[name] = MockProvider(name=name, handler=self.recorder(name, inner))

    def tearDown(self):
        self.ws.cleanup()

    def recorder(self, name, inner):
        def run(req):
            kind = "plan" if req.schema is PLAN_SCHEMA else "review" if req.schema is REVIEW_SCHEMA else \
                "step" if req.schema is STEP_SCHEMA else "other"
            self.seen.append((name, kind, req.model, req.effort))
            return inner(req)
        return run

    def steps(self):
        return [x for x in self.seen if x[1] == "step"]

    def test_the_planner_rates_each_step_and_foreman_maps_it_to_a_profile(self):
        self.weights = {"1": "light", "2": "heavy", "3": "standard"}
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual({s["id"].split(".")[-1]: s["weight"] for s in task["steps"]},
                         {"1": "light", "2": "heavy", "3": "standard"})
        by_effort = sorted(e for _, _, _, e in self.steps())
        self.assertEqual(by_effort, ["high", "high", "medium"])
        self.assertTrue(all(m == "claude-sonnet-5-5" for _, _, m, _ in self.steps()))

    def test_planning_and_review_use_their_own_profiles(self):
        self.s.plan = lambda req: plan([("orders-api", "modify")], [step("1", "orders-api")])
        self.s.step = editing_worker()
        self.eng.handle("add currency", self.ws.api)
        plans = [x for x in self.seen if x[1] == "plan"]
        self.assertEqual((plans[0][2], plans[0][3]), ("claude-sonnet-5-5", "medium"))
        reviews = [x for x in self.seen if x[1] == "review"]
        self.assertEqual(reviews[0][:2], ("opencode", "review"), "an independent reviewer: not the implementer")
        self.assertEqual(reviews[0][2:], (None, None), "opencode has no profile: its CLI default")
        self.eng.providers.pop("opencode")
        self.seen.clear()
        self.s.step = editing_worker("// totals\n")
        self.eng.handle("add totals", self.ws.api)
        review = next(x for x in self.seen if x[1] == "review")
        self.assertEqual(review, ("claude", "review", "claude-sonnet-5-5", "high"))

    def test_a_missing_weight_means_standard(self):
        self.eng.handle("look into currency", self.ws.api)
        self.assertEqual(self.steps()[0][2:], ("claude-sonnet-5-5", "high"))

    def test_effort_for_a_task_applies_to_its_workers_not_to_planning(self):
        self.weights = {"1": "light"}
        self.eng.handle("look into currency", self.ws.api, effort="max")
        self.assertEqual(self.steps()[0][3], "max")
        self.assertEqual([x for x in self.seen if x[1] == "plan"][0][3], "medium")

    def test_a_model_for_a_task_goes_to_the_preferred_provider_only(self):
        task = self.eng.handle("look into currency", self.ws.api, model="opus")["task"]
        self.assertEqual(task["model_for"], {"claude": "opus"})
        self.assertEqual(self.steps()[0][:3], ("claude", "step", "opus"))
        self.seen.clear()
        task = self.eng.handle("look into totals", self.ws.api, model="opencode:big-model")["task"]
        self.assertEqual(task["model_for"], {"opencode": "big-model"})
        self.assertEqual(self.steps()[0][:3], ("claude", "step", "claude-sonnet-5-5"), "claude is not given opencode's")

    def test_after_a_handoff_the_other_provider_uses_its_own_profile(self):
        self.eng.cfg.providers["opencode"].profiles["standard"] = Choice("oc-model", None)
        orig = self.eng.providers["claude"].handler
        self.eng.providers["claude"].handler = lambda req: RunResult(False, error="You've hit your session limit") \
            if req.schema is STEP_SCHEMA else orig(req)
        task = self.eng.handle("look into currency", self.ws.api, model="opus")["task"]
        self.assertEqual([(a["provider"], a["model"]) for a in task["steps"][0]["history"]],
                         [("claude", "opus"), ("opencode", "oc-model")])

    def test_the_report_and_the_events_say_what_ran(self):
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertIn("Models    claude claude-sonnet-5-5 (high) ×1", render_report(task))
        start = next(e for e in self.eng.store.events(task["id"]) if e["event"] == "step_start")
        self.assertEqual((start["model"], start["effort"], start["weight"]), ("claude-sonnet-5-5", "high", "standard"))

    def test_a_follow_up_step_keeps_the_choices_of_its_task(self):
        self.s.plan = lambda req: with_mode(plan([("orders-api", "modify")], [step("1", "orders-api")]), "development")
        self.s.step = editing_worker()
        task = self.eng.handle("add currency", self.ws.api, effort="medium", model="sonnet")["task"]
        nxt = self.eng.prepare_next(self.ws.api)[0]
        self.assertEqual((nxt or {}).get("effort"), "medium")
        self.assertEqual((nxt or {}).get("model_for"), task["model_for"])


class CommandTest(unittest.TestCase):
    def test_what_each_cli_is_asked_for(self):
        claude = ClaudeProvider(load_config(Workspace().home).providers["claude"])
        cmd = claude.build_command(RunRequest("hi", "/x", model="claude-sonnet-5-5", effort="high"))
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertNotIn("--effort", claude.build_command(RunRequest("hi", "/x")))
        agy = AntigravityProvider(load_config(Workspace().home).providers["claude"])
        cmd = agy.build_command(RunRequest("hi", "/x", model="gemini-3.8-flash-high", effort="high"))
        self.assertIn("--model=gemini-3.8-flash-high", cmd)
        self.assertIn("--effort=high", cmd)
        oc = OpenCodeProvider(load_config(Workspace().home).providers["claude"])
        cmd = oc.build_command(RunRequest("hi", "/x", model="p/m", effort="high"))
        self.assertEqual(cmd[cmd.index("-m") + 1], "p/m")
        self.assertFalse(any("effort" in c for c in cmd), "OpenCode has no effort option")

    def test_foreman_models_shows_and_changes_the_profiles(self):
        ws = Workspace()
        try:
            os.environ["FOREMAN_HOME"] = ws.home

            def models(*argv):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(["models", *argv])
                return code, out.getvalue()
            code, text = models()
            self.assertEqual(code, 0)
            self.assertIn("standard  claude-sonnet-5-5 effort high", text)
            self.assertIn("opencode", text)
            code, text = models("claude", "heavy", "claude-opus-5-5", "high")
            self.assertIn("heavy     claude-opus-5-5 effort high", text)
            with open(os.path.join(ws.home, "config.yaml")) as f:
                saved = yaml.safe_load(f)
            self.assertEqual(saved["providers"]["claude"]["profiles"]["heavy"],
                             {"model": "claude-opus-5-5", "effort": "high"})
            self.assertEqual(load_config(ws.home).providers["claude"].choice("heavy").model, "claude-opus-5-5")
            code, text = models("claude", "light", "default", "default")
            self.assertIn("light     CLI default", text)
            self.assertEqual(models("nope", "light", "x")[0], 1)
        finally:
            os.environ.pop("FOREMAN_HOME", None)
            ws.cleanup()


if __name__ == "__main__":
    unittest.main()
