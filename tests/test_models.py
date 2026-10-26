"""Which model and effort each run gets (one choice per provider, light work one level lower), what the CLIs are asked
for and how they list their models, the interactive `models` / `config` menus, and the auto-switch setting."""

import os
import time
import unittest
from unittest import mock

import yaml

from helpers import Script, Workspace, make_engine
from test_scenarios import editing_worker, plan, step

from orchestrator import menus
from orchestrator.brain import PLAN_SCHEMA, REVIEW_SCHEMA, STEP_SCHEMA
from orchestrator.config import Choice, load_config
from orchestrator.providers.antigravity import AntigravityProvider
from orchestrator.providers.base import LIMIT, ModelInfo, RunRequest, RunResult
from orchestrator.providers.claude import ClaudeProvider
from orchestrator.providers.mock import MockProvider
from orchestrator.providers.opencode import OpenCodeProvider
from orchestrator.report import render_report
from orchestrator.util import ProcResult

CONFIG = """roots: [{root}]
brain: claude
providers:
  claude: {{command: claude}}
  antigravity: {{command: agy}}
  opencode: {{command: opencode}}
limits: {{max_parallel: 4, max_fix_rounds: 2, limit_retry_sec: 0.05, limit_retries: 3}}
ui: {{display: headless, notify: true}}
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.write_config("")

    def tearDown(self):
        self.ws.cleanup()

    def write_config(self, extra: str):
        with open(os.path.join(self.ws.home, "config.yaml"), "w") as f:
            f.write(CONFIG.format(root=self.ws.root) + extra)

    def saved(self):
        with open(os.path.join(self.ws.home, "config.yaml")) as f:
            return yaml.safe_load(f)


class ChoiceTest(Base):
    def test_built_in_choices_are_the_middle_tier_at_high_effort(self):
        cfg = load_config(self.ws.home)
        claude = cfg.providers["claude"]
        self.assertEqual(claude.chosen(), Choice("claude-sonnet-5-5", "high"))
        self.assertEqual(claude.choice("standard"), Choice("claude-sonnet-5-5", "high"))
        self.assertEqual(claude.choice("review"), Choice("claude-sonnet-5-5", "high"))
        self.assertEqual(claude.choice("light"), Choice("claude-sonnet-5-5", "medium"), "one level lower")
        self.assertEqual(claude.choice("plan"), Choice("claude-sonnet-5-5", "medium"))
        agy = cfg.providers["antigravity"]
        self.assertEqual((agy.choice("standard"), agy.choice("light")),
                         (Choice("gemini-3.8-flash", "high"), Choice("gemini-3.8-flash", "medium")))
        self.assertEqual(cfg.providers["opencode"].choice("standard"), Choice(), "the CLI's own default")

    def test_a_chosen_model_and_its_levels_decide_how_far_light_work_drops(self):
        cfg = {"roots": [], "providers": {
            "opencode": {"command": "opencode", "model": "p/m", "effort": "high", "levels": ["low", "high", "max"]},
            "claude": {"command": "claude", "model": "opus", "effort": "low", "levels": ["low", "medium"]},
            "antigravity": {"command": "agy", "model": "default"}}}
        with open(os.path.join(self.ws.home, "config.yaml"), "w") as f:
            yaml.safe_dump(cfg, f)
        c = load_config(self.ws.home).providers
        self.assertEqual(c["opencode"].choice("light"), Choice("p/m", "low"), "the level below high in THIS model's list")
        self.assertEqual(c["opencode"].choice("standard"), Choice("p/m", "high"))
        self.assertEqual(c["claude"].choice("light"), Choice("opus", "low"), "already the lowest")
        self.assertEqual(c["antigravity"].choice("light"), Choice(), "default = the CLI decides")

    def test_auto_switch_is_a_setting_that_defaults_to_on(self):
        self.assertTrue(load_config(self.ws.home).auto_switch)
        self.write_config("auto_switch: false\n")
        self.assertFalse(load_config(self.ws.home).auto_switch)


class RunTest(Base):
    def setUp(self):
        super().setUp()
        self.s = Script()
        self.weights = {}
        self.s.plan = lambda req: plan([("orders-api", "inspect")],
                                       [{**step(i, "orders-api", kind="investigate"), "weight": w}
                                        for i, w in self.weights.items()] or [step("1", "orders-api", kind="investigate")])
        self.eng = make_engine(self.ws, self.s, providers=["claude", "antigravity", "opencode"])
        self.seen = []
        for name in self.eng.providers:
            self.eng.providers[name] = MockProvider(name=name, handler=self.recorder(name, self.s.handler(name)))

    def recorder(self, name, inner):
        def run(req):
            kind = "plan" if req.schema is PLAN_SCHEMA else "review" if req.schema is REVIEW_SCHEMA else \
                "step" if req.schema is STEP_SCHEMA else "other"
            self.seen.append((name, kind, req.model, req.effort))
            return inner(req)
        return run

    def steps(self):
        return [x for x in self.seen if x[1] == "step"]

    def test_the_planner_rates_steps_and_light_ones_run_one_level_lower(self):
        self.weights = {"1": "light", "2": "standard", "3": "heavy"}  # an old "heavy" rating counts as standard
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual(sorted(e for _, _, _, e in self.steps()), ["high", "high", "medium"])
        self.assertTrue(all(m == "claude-sonnet-5-5" for _, _, m, _ in self.steps()))
        self.assertEqual(sorted(s["weight"] for s in task["steps"]), ["light", "standard", "standard"])

    def test_planning_runs_one_level_lower_and_review_uses_the_chosen_effort(self):
        self.s.plan = lambda req: plan([("orders-api", "modify")], [step("1", "orders-api")])
        self.s.step = editing_worker()
        self.eng.providers.pop("opencode")
        self.eng.providers.pop("antigravity")
        self.eng.handle("add currency", self.ws.api)
        plans = [x for x in self.seen if x[1] == "plan"]
        self.assertEqual((plans[0][2], plans[0][3]), ("claude-sonnet-5-5", "medium"))
        review = next(x for x in self.seen if x[1] == "review")
        self.assertEqual(review, ("claude", "review", "claude-sonnet-5-5", "high"))

    def test_after_a_handoff_the_other_provider_uses_its_own_choice(self):
        orig = self.eng.providers["claude"].handler
        self.eng.providers["claude"].handler = lambda req: RunResult(False, error="You've hit your session limit") \
            if req.schema is STEP_SCHEMA else orig(req)
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        hist = task["steps"][0]["history"]
        self.assertEqual([(a["provider"], a["model"], a["effort"]) for a in hist],
                         [("claude", "claude-sonnet-5-5", "high"), ("antigravity", "gemini-3.8-flash", "high")])

    def test_the_report_and_the_events_say_what_ran(self):
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertIn("Models    claude claude-sonnet-5-5 (high) ×1", render_report(task))
        start = next(e for e in self.eng.store.events(task["id"]) if e["event"] == "step_start")
        self.assertEqual((start["model"], start["effort"], start["weight"]), ("claude-sonnet-5-5", "high", "standard"))

    def test_a_menu_change_reaches_the_running_engine(self):
        with mock.patch("orchestrator.menus.ui.out"):
            menus._save(self.eng, "claude", "opus", "xhigh", ["low", "high", "xhigh"])
        self.eng.handle("look into currency", self.ws.api)
        self.assertEqual(self.steps()[0][2:], ("opus", "xhigh"))


class CommandTest(unittest.TestCase):
    def cfg(self, name):
        ws = Workspace()
        self.addCleanup(ws.cleanup)
        return load_config(ws.home).providers[name]

    def test_what_each_cli_is_asked_for(self):
        claude = ClaudeProvider(self.cfg("claude"))
        cmd = claude.build_command(RunRequest("hi", "/x", model="claude-sonnet-5-5", effort="high"))
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        bare = claude.build_command(RunRequest("hi", "/x"))
        self.assertFalse({"--model", "--effort"} & set(bare), "nothing chosen: the CLI decides")
        agy = AntigravityProvider(self.cfg("claude"))
        cmd = agy.build_command(RunRequest("hi", "/x", model="gemini-3.8-flash", effort="high"))
        self.assertIn("--model=gemini-3.8-flash-high", cmd, "the effort is a variant of the model id")
        self.assertFalse(any(c.startswith("--effort") for c in cmd))
        self.assertIn("--model=gpt-oss-120b-medium", agy.build_command(RunRequest("hi", "/x", model="gpt-oss-120b-medium")))
        oc = OpenCodeProvider(self.cfg("claude"))
        cmd = oc.build_command(RunRequest("hi", "/x", model="opencode/beam-free", effort="low"))
        self.assertEqual((cmd[cmd.index("-m") + 1], cmd[cmd.index("--variant") + 1]), ("opencode/beam-free", "low"))

    def test_claude_models_and_levels_are_read_from_its_help(self):
        help_text = ("  --effort <level>                      Effort level for the current session\n"
                     "                                        (low, medium, high, xhigh, max)\n"
                     "  --model <model>                       Model for the current session. Provide\n"
                     "                                        an alias for the latest model (e.g.\n"
                     "                                        'fable', 'opus', or 'sonnet') or a\n"
                     "                                        model's full name.\n"
                     "  -n, --name <name>                     Set a display name\n")
        with mock.patch("orchestrator.providers.claude.run_process", return_value=ProcResult(0, help_text, "", False, 0)):
            models = ClaudeProvider(self.cfg("claude")).list_models()
        self.assertEqual([m.id for m in models], ["fable", "opus", "sonnet"])
        self.assertEqual(models[0].levels, ["low", "medium", "high", "xhigh", "max"])

    def test_agy_variants_become_one_model_with_levels(self):
        out = ("Fetching available models...\n"
               "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\ngemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
               "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\nclaude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
               "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n")
        with mock.patch("orchestrator.providers.antigravity.run_process", return_value=ProcResult(0, out, "", False, 0)):
            models = AntigravityProvider(self.cfg("claude")).list_models()
        self.assertEqual([(m.id, m.name, m.levels) for m in models],
                         [("gemini-3.8-flash", "Gemini 3.8 Flash", ["low", "medium", "high"]),
                          ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)", []),
                          ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)", [])])

    def test_opencode_models_come_with_their_own_levels_and_backend(self):
        out = ('opencode/beam-free\n{\n  "id": "beam-free",\n  "name": "Beam Free",\n  "variants": {\n'
               '    "low": {},\n    "medium": {},\n    "high": {}\n  }\n}\n'
               'opencode-go/glm-5.3\n{\n  "id": "glm-5.3",\n  "name": "GLM 5.3",\n  "variants": {\n'
               '    "low": {},\n    "high": {},\n    "max": {}\n  }\n}\n'
               'openrouter/plain\n{\n  "id": "plain",\n  "name": "Plain"\n}\n')
        p = OpenCodeProvider(self.cfg("claude"))
        p._binary = "/bin/opencode"
        with mock.patch("orchestrator.providers.opencode.run_process", return_value=ProcResult(0, out, "", False, 0)):
            models = p.list_models()
        self.assertEqual([(m.id, m.group, m.levels) for m in models],
                         [("opencode/beam-free", "opencode", ["low", "medium", "high"]),
                          ("opencode-go/glm-5.3", "opencode-go", ["low", "high", "max"]),
                          ("openrouter/plain", "openrouter", [])])


class MenuTest(Base):
    """The menus asked in order, answered from a script: each answer picks the option that starts with that text."""

    def setUp(self):
        super().setUp()
        self.eng = make_engine(self.ws, Script(), providers=["claude", "antigravity", "opencode"])
        self.asked = []
        self.models = {
            "claude": [ModelInfo(m, f"{m} (latest)", ["low", "medium", "high", "xhigh", "max"]) for m in ("opus", "sonnet")],
            "antigravity": [ModelInfo("gemini-3.8-flash", "Gemini 3.8 Flash", ["low", "medium", "high"]),
                            ModelInfo("claude-sonnet-4-6", "Claude Sonnet 4.6")],
            "opencode": [ModelInfo(f"openrouter/m{i}", f"M{i}", ["low", "high"], "openrouter") for i in range(40)]
            + [ModelInfo("opencode/beam-free", "Beam Free", ["low", "medium", "high", "xhigh", "max"], "opencode")],
        }
        for name, ms in self.models.items():
            self.eng.providers[name].list_models = lambda ms=ms: ms

    def run_menu(self, fn, answers):
        script = list(answers)

        def choose(title, body, options, allow_text=True, start=0, **kw):
            self.asked.append((title, list(options), start))
            want = script.pop(0)
            if want is None:
                return None
            for o in options:
                if o.startswith(want):
                    return o
            if allow_text:
                return want
            raise AssertionError(f"{want!r} is not one of {options}")
        with mock.patch("orchestrator.menus.ui.choose", side_effect=choose), mock.patch("orchestrator.menus.ui.out"):
            result = fn(self.eng)
        self.assertEqual(script, [], "every scripted answer was used")
        return result

    def test_claude_provider_then_model_then_the_levels_it_has(self):
        text = self.run_menu(menus.models_menu, ["claude", "opus", "xhigh"])
        self.assertIn("claude: opus · effort xhigh (saved)", text)
        self.assertIn("Light steps and planning use effort high", text)
        title, options, _ = self.asked[2]
        self.assertEqual(title, "claude: effort for opus")
        self.assertEqual([o.split()[0] for o in options], ["low", "medium", "high", "xhigh", "max"])
        saved = self.saved()["providers"]["claude"]
        self.assertEqual((saved["model"], saved["effort"], saved["levels"]),
                         ("opus", "xhigh", ["low", "medium", "high", "xhigh", "max"]))
        self.assertEqual(self.eng.cfg.providers["claude"].chosen(), Choice("opus", "xhigh"))

    def test_antigravity_offers_three_levels_for_flash_and_none_for_a_fixed_model(self):
        self.run_menu(menus.models_menu, ["antigravity", "gemini", "low"])
        self.assertEqual(len(self.asked[2][1]), 3)
        self.assertEqual(self.eng.cfg.providers["antigravity"].chosen(), Choice("gemini-3.8-flash", "low"))
        self.asked.clear()
        text = self.run_menu(menus.models_menu, ["antigravity", "claude-sonnet-4-6"])
        self.assertEqual(len(self.asked), 2, "no effort question for a model with a single setting")
        self.assertIn("single fixed setting", text)
        self.assertEqual(self.eng.cfg.providers["antigravity"].chosen(), Choice("claude-sonnet-4-6", None))

    def test_opencode_asks_for_the_backend_first_and_can_narrow_a_long_list(self):
        self.run_menu(menus.models_menu, ["opencode", "opencode  (1", "opencode/beam-free", "medium"])
        self.assertEqual(self.asked[1][0], "opencode: backend")
        self.assertEqual(self.eng.cfg.providers["opencode"].chosen(), Choice("opencode/beam-free", "medium"))
        self.asked.clear()
        text = self.run_menu(menus.models_menu, ["opencode", "openrouter", "M[3]$", "openrouter/m3", "high"])
        titles = [t for t, _, _ in self.asked]
        self.assertEqual(titles, ["Provider", "opencode: backend", "Find a model", "opencode: model",
                                  "opencode: effort for openrouter/m3"])
        self.assertTrue(self.asked[3][1][1].startswith("openrouter/m3"), self.asked[3][1])
        self.assertIn("opencode: openrouter/m3 · effort high", text)
        self.assertEqual(self.eng.cfg.providers["opencode"].effort_levels(), ["low", "high"])

    def test_the_model_search_is_a_regex(self):
        ms = [ModelInfo("openrouter/deepseek/deepseek-v4:free", "DeepSeek V4 (free)"),
              ModelInfo("openrouter/deepseek/deepseek-r2", "DeepSeek R2"),
              ModelInfo("openrouter/meta/llama-4-free", "Llama 4 Free")]
        ids = lambda found: [m.id.split("/")[-1] for m in found]  # noqa: E731
        self.assertEqual(ids(menus.search(ms, "deepseek.*free")), ["deepseek-v4:free"])
        self.assertEqual(ids(menus.search(ms, "DEEPSEEK")), ["deepseek-v4:free", "deepseek-r2"], "not case sensitive")
        self.assertEqual(ids(menus.search(ms, "(llama|r2)")), ["deepseek-r2", "llama-4-free"])
        self.assertEqual(ids(menus.search(ms, "^openrouter/meta")), ["llama-4-free"])
        self.assertEqual(menus.search(ms, "nothing"), [])
        self.assertIsNone(menus.search(ms, "(unclosed"), "an invalid regex is reported, not a crash")

    def test_cli_default_and_cancelling(self):
        text = self.run_menu(menus.models_menu, ["claude", "CLI default"])
        self.assertIn("its CLI decides", text)
        self.assertEqual(self.eng.cfg.providers["claude"].choice("light"), Choice())
        before = self.saved()
        self.assertIsNone(self.run_menu(menus.models_menu, ["claude", None]))
        self.assertIsNone(self.run_menu(menus.models_menu, ["claude", "opus", None]))
        self.assertIsNone(self.run_menu(menus.models_menu, [None]))
        self.assertEqual(self.saved(), before, "cancelling writes nothing")

    def test_a_typed_model_name_gets_claudes_levels(self):
        self.run_menu(menus.models_menu, ["claude", "claude-opus-5-5", "high"])
        self.assertEqual(self.eng.cfg.providers["claude"].chosen(), Choice("claude-opus-5-5", "high"))

    def test_the_settings_menu_changes_display_and_auto_switch(self):
        self.eng._fixed_visual = None  # as when started without --visual/--headless: the setting decides
        text = self.run_menu(menus.config_menu, ["Show workers", "Yes", "Switch provider", "No", None])
        self.assertIn("Workers open in terminal windows", text)
        self.assertIn("Foreman waits for a provider's limit", text)
        saved = self.saved()
        self.assertEqual((saved["ui"]["display"], saved["auto_switch"]), ("terminal", False))
        self.assertEqual((self.eng.cfg.display, self.eng.cfg.auto_switch, self.eng.visual), ("terminal", False, True))
        rows = self.asked[-1][1]
        self.assertTrue(rows[0].startswith("Show workers in terminal windows") and rows[0].endswith("yes"), rows[0])
        self.assertTrue(rows[1].endswith("no (wait for it)"), rows[1])
        self.assertIn("claude claude-sonnet-5-5 · effort high", rows[2])

    def test_the_settings_menu_reaches_models_and_the_first_worker(self):
        self.run_menu(menus.config_menu, ["Models", "claude", "sonnet", "max", "First worker", "antigravity", None])
        self.assertEqual(self.eng.cfg.providers["claude"].chosen(), Choice("sonnet", "max"))
        self.assertEqual(next(iter(self.eng.providers)), "antigravity")

    def test_without_a_terminal_there_is_a_plain_summary(self):
        text = menus.models_summary(self.eng)
        self.assertIn("claude       claude-sonnet-5-5 · effort high   light work and planning: "
                      "claude-sonnet-5-5 effort medium", text)
        self.assertIn("opencode     CLI default", text)


class AutoSwitchTest(Base):
    def setUp(self):
        super().setUp()
        self.s = Script()
        self.s.plan = lambda req: plan([("orders-api", "inspect")], [step("1", "orders-api", kind="investigate")])
        self.eng = make_engine(self.ws, self.s, providers=["claude", "opencode"])
        self.calls = {"claude": [], "opencode": []}
        self.limit_once = {"left": 1}
        for name in self.calls:
            self.eng.providers[name] = MockProvider(name=name, handler=self.runner(name, self.s.handler(name)))

    def runner(self, name, inner):
        def run(req):
            if req.schema is STEP_SCHEMA:
                self.calls[name].append(req)
                if name == "claude" and self.limit_once["left"]:
                    self.limit_once["left"] -= 1
                    return RunResult(False, error="You've hit your session limit", kind=LIMIT,
                                     retry_at=time.time() + 0.3, session_id="c-1")
            return inner(req)
        return run

    def test_on_another_provider_takes_over(self):
        self.eng.cfg.auto_switch = True
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual((len(self.calls["claude"]), len(self.calls["opencode"])), (1, 1))
        self.assertEqual(task["status"], "done")

    def test_off_foreman_waits_for_the_same_provider_and_resumes_its_session(self):
        self.eng.cfg.auto_switch = False
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(len(self.calls["opencode"]), 0, "no hand-over")
        self.assertEqual([r.session_id for r in self.calls["claude"]], [None, "c-1"])
        self.assertIn("waiting", [e["event"] for e in self.eng.store.events(task["id"])])
        self.assertTrue(any("automatic switching is off" in n for n in task["notices"]))

    def test_off_a_provider_already_resting_is_waited_for_before_the_step_starts(self):
        self.eng.cfg.auto_switch = False
        self.limit_once["left"] = 0
        self.eng._provider_failed("claude", LIMIT, "limit", time.time() + 0.3)
        task = self.eng.handle("look into currency", self.ws.api)["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual((len(self.calls["claude"]), len(self.calls["opencode"])), (1, 0))

    def test_off_planning_waits_too(self):
        self.eng.cfg.auto_switch = False
        self.limit_once["left"] = 0
        self.eng._provider_failed("claude", LIMIT, "limit", time.time() + 0.3)
        self.eng.handle("look into currency", self.ws.api)
        self.assertNotIn("opencode:plan", self.s.log)
        self.assertIn("claude:plan", self.s.log)


if __name__ == "__main__":
    unittest.main()
