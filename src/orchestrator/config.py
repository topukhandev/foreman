"""Global configuration (~/.foreman/config.yaml). One config per machine, shared by every project."""

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

DEFAULT_CONFIG = """\
# Foreman configuration (machine-local; never commit).
# Directories whose child folders are treated as projects.
roots:
  - ~/Desktop
  - ~/projects
  - /var/www/projects

# Provider used for the orchestrator's own reasoning (triage, planning, review).
brain: claude

# When a provider reaches its usage limit: true = another provider takes over the step; false = wait for its reset.
auto_switch: true

# Worker preference order. The first available provider is used unless a step needs another.
providers:
  claude:
    enabled: true
    command: claude
    model: null          # null = built-in default (Sonnet 5.5, high effort); choose with `foreman models`
    max_runs_per_day: 300
  opencode:
    enabled: true
    command: opencode
    model: null
    max_runs_per_day: 300
  antigravity:
    enabled: true
    command: agy
    model: null
    max_runs_per_day: 300
  # Any other AI CLI can be added without code (type: generic). Codex preset, not yet verified against an
  # installed codex; check `codex exec --help` and set enabled: true.
  codex:
    enabled: false
    type: generic
    command: codex
    read_args: [exec, --sandbox, read-only, --skip-git-repo-check, "{prompt}"]
    write_args: [exec, --sandbox, workspace-write, --skip-git-repo-check, "{prompt}"]
    read_only_enforced: true
    max_runs_per_day: 300

limits:
  max_parallel: 4          # concurrent worker sessions within one task
  max_global: 6            # AI CLI runs at once across every task and Foreman process (workers, QA, planning)
  step_timeout_sec: 3600   # one worker step (first attempt); planners can ask for more per step
  step_timeout_max_sec: 14400   # ceiling for any single attempt
  timeout_retries: 3       # after a timeout the same session is resumed, with double the time, this many times
  transient_retries: 3     # network drop / overload: resume the same session after 30 s, 2 min, 5 min
  limit_retry_sec: 300     # all providers out of limits: check again every 5 min (or wait for the stated reset)
  limit_retries: 10        # checks before the task pauses (waiting for a known reset time doesn't count)
  limit_wait_max_hours: 12 # a reset further away than this pauses the task right away
  pressure_percent: 90     # a provider at this share of its usage window gets no new work if another can do it
  brain_timeout_sec: 600   # planning / review calls
  check_timeout_sec: 900   # one validation command
  max_fix_rounds: 3        # review/check bounces before escalating
  max_replans: 2           # plan expansions after new dependencies are found
  warn_at_percent: 80      # daily-run warning threshold (needs sign-off)

ui:
  display: terminal        # terminal = show workers in visible terminal windows; headless = under the hood
  notify: true             # one desktop notification when a task run finishes (never mid-run)
  alt_screen: true         # chat uses its own screen; your terminal is restored as it was on exit
  copy_on_select: true     # selecting text with the mouse in the chat copies it to the clipboard
  prompt_suggestions: true # a suggested next message waits in grey in the empty input (Tab takes it)
  status_line: true        # a line under the input: project, mode, current task, models
  output_style: default    # default | concise | explanatory | learning | proactive (how answers are written)
  on_close: ask            # desktop app closed while tasks run: ask | keep (they go on) | stop (they stop)

sessions:
  reuse: fork              # fork | off  - continue context from your recent sessions
  reuse_max_age_hours: 48
"""


EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_LEVELS = ("low", "medium", "high", "xhigh", "max")
LIGHT_ROLES = ("light", "plan")  # these run one effort level below the chosen one (when the model has a lower one)


@dataclass
class Choice:
    """Model and reasoning effort for a run; None = the CLI's own default."""
    model: Optional[str] = None
    effort: Optional[str] = None

    def label(self) -> str:
        return " ".join(x for x in (self.model, f"effort {self.effort}" if self.effort else "") if x) or "CLI default"


# What a provider uses until the user picks a model with `foreman models` / `/models`: the middle tier at high effort
# (never max). Other CLIs: their own default.
DEFAULT_MODELS: Dict[str, Dict[str, Any]] = {
    "claude": {"model": "claude-sonnet-5-5", "effort": "high", "levels": list(DEFAULT_LEVELS)},
    "antigravity": {"model": "gemini-3.8-flash", "effort": "high", "levels": ["low", "medium", "high"]},
}


@dataclass
class ProviderSettings:
    name: str
    enabled: bool = True
    command: str = ""
    model: Optional[str] = None      # None = the built-in default; "default" = let the CLI decide
    effort: Optional[str] = None
    levels: List[str] = field(default_factory=list)  # effort levels of that model, lowest first (saved by the menu)
    max_runs_per_day: int = 300
    extra: Dict[str, Any] = field(default_factory=dict)

    def chosen(self) -> Choice:
        """The model and effort picked for this provider (or its built-in default)."""
        if self.model is None:
            d = DEFAULT_MODELS.get(self.name) or {}
            return Choice(d.get("model"), d.get("effort"))
        return Choice(None, None) if self.model == "default" else Choice(self.model, self.effort)

    def effort_levels(self) -> List[str]:
        return list(self.levels or (DEFAULT_MODELS.get(self.name, {}).get("levels") if self.model is None else None)
                    or [])

    def choice(self, role: str) -> Choice:
        """What to run for a kind of work: light steps and planning use one effort level below the chosen one (when
        the model has a lower level); standard steps and review use exactly what was chosen."""
        c = self.chosen()
        levels = self.effort_levels()
        if role in LIGHT_ROLES and c.effort in levels and levels.index(c.effort) > 0:
            return Choice(c.model, levels[levels.index(c.effort) - 1])
        return c


@dataclass
class Limits:
    max_parallel: int = 4
    max_global: int = 6               # AI CLI runs at once across all tasks and Foreman processes (oscompat slots)
    step_timeout_sec: int = 3600
    step_timeout_max_sec: int = 14400
    timeout_retries: int = 3
    transient_retries: int = 3        # network drop / overload: resume the same session after 30 s, 2 min, 5 min
    limit_retry_sec: int = 300        # every provider out of limits: check again this often
    limit_retries: int = 10           # ... this many times (waiting for a known reset time doesn't count), then pause
    limit_wait_max_hours: int = 12    # a known reset further away than this pauses the task right away
    pressure_percent: int = 90        # a provider reporting this much of its usage window: new work goes elsewhere
    brain_timeout_sec: int = 600
    check_timeout_sec: int = 900
    max_fix_rounds: int = 3
    max_replans: int = 2
    warn_at_percent: int = 80


@dataclass
class Config:
    home: str
    roots: List[str]
    brain: str
    providers: Dict[str, ProviderSettings]
    limits: Limits
    reuse_sessions: str = "fork"
    reuse_max_age_hours: int = 48
    display: str = "terminal"
    notify: bool = True
    alt_screen: bool = True
    copy_on_select: bool = True
    prompt_suggestions: bool = True
    status_line: bool = True
    output_style: str = "default"
    on_close: str = "ask"  # the desktop app is closed while tasks run: ask, keep them running, or stop them
    auto_switch: bool = True  # a provider that hits its usage limit hands its step to another one (off: wait for it)

    @property
    def provider_order(self) -> List[str]:
        return [n for n, p in self.providers.items() if p.enabled]


ON_CLOSE = ("ask", "keep", "stop")


def foreman_home() -> str:
    """~/.foreman (or $FOREMAN_HOME). Data from the name Switchyard, ~/.switchyard, is moved here on first use."""
    env = os.environ.get("FOREMAN_HOME") or os.environ.get("SWITCHYARD_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    home, legacy = (os.path.join(os.path.expanduser("~"), n) for n in (".foreman", ".switchyard"))
    if not os.path.exists(home) and os.path.isdir(legacy):
        try:
            os.rename(legacy, home)
        except OSError:
            return legacy
    return home


def secure_home(home: str) -> None:
    """Make the Foreman home readable by its owner only (0700). Everything Foreman stores sits below it, so this also
    covers files that older versions created with default permissions. POSIX only; a folder someone else owns is
    left alone."""
    if os.name == "nt":
        return
    try:
        st = os.stat(home)
        if st.st_uid == os.getuid() and st.st_mode & 0o077:
            os.chmod(home, 0o700)
    except OSError:
        pass


def load_config(home: Optional[str] = None) -> Config:
    home = home or foreman_home()
    os.makedirs(home, mode=0o700, exist_ok=True)
    secure_home(home)
    path = os.path.join(home, "config.yaml")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    providers: Dict[str, ProviderSettings] = {}
    if not data.get("providers"):  # a hand-written config without the section keeps the default providers
        data["providers"] = yaml.safe_load(DEFAULT_CONFIG)["providers"]
    for name, p in data["providers"].items():
        p = p or {}
        known = {"enabled", "command", "model", "brain_model", "effort", "levels", "max_runs_per_day", "profiles"}
        model = p.get("model")
        providers[name] = ProviderSettings(
            name=name,
            enabled=bool(p.get("enabled", True)),
            command=str(p.get("command") or name),
            model=str(model) if model else None,
            effort=str(p["effort"]).lower() if p.get("effort") else None,
            levels=[str(x) for x in p.get("levels") or []],
            max_runs_per_day=int(p.get("max_runs_per_day", 300)),
            extra={k: v for k, v in p.items() if k not in known},
        )

    lim = data.get("limits") or {}
    limits = Limits(**{k: int(v) for k, v in lim.items() if k in Limits.__dataclass_fields__})
    sess = data.get("sessions") or {}
    ui = data.get("ui") or {}
    roots = [os.path.abspath(os.path.expanduser(r)) for r in (data.get("roots") or [])]
    return Config(
        home=home,
        roots=roots,
        brain=str(data.get("brain") or "claude"),
        providers=providers,
        limits=limits,
        reuse_sessions=str(sess.get("reuse", "fork")),
        reuse_max_age_hours=int(sess.get("reuse_max_age_hours", 48)),
        display="headless" if str(ui.get("display", "terminal")).lower() == "headless" else "terminal",
        notify=bool(ui.get("notify", True)),
        alt_screen=bool(ui.get("alt_screen", True)),
        copy_on_select=bool(ui.get("copy_on_select", True)),
        prompt_suggestions=bool(ui.get("prompt_suggestions", True)),
        status_line=bool(ui.get("status_line", True)),
        output_style=str(ui.get("output_style") or "default").lower(),
        on_close=str(ui.get("on_close") or "ask").lower() if str(ui.get("on_close") or "ask").lower() in ON_CLOSE else "ask",
        auto_switch=bool(data.get("auto_switch", True)),
    )


def set_first_provider(home: str, name: str) -> None:
    """Move a provider to the front of the worker preference order."""
    path = os.path.join(home, "config.yaml")
    load_config(home)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    provs = data.get("providers") or {}
    if name in provs:
        data["providers"] = {name: provs[name], **{k: v for k, v in provs.items() if k != name}}
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Foreman configuration (machine-local; never commit).\n")
            yaml.safe_dump(data, f, sort_keys=False)


def set_option(home: str, dotted_key: str, value) -> None:
    """Update one config value in place (e.g. `ui.display`), preserving the rest of the file."""
    path = os.path.join(home, "config.yaml")
    load_config(home)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    node = data
    keys = dotted_key.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Foreman configuration (machine-local; never commit).\n")
        yaml.safe_dump(data, f, sort_keys=False)
