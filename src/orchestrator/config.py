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

# Worker preference order. The first available provider is used unless a step needs another.
providers:
  claude:
    enabled: true
    command: claude
    model: null          # worker model (null = CLI default)
    brain_model: null    # model for planning/review (null = CLI default)
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
  max_parallel: 4          # concurrent worker sessions
  step_timeout_sec: 1200   # one worker step
  brain_timeout_sec: 600   # planning / review calls
  check_timeout_sec: 900   # one validation command
  max_fix_rounds: 3        # review/check bounces before escalating
  max_replans: 2           # plan expansions after new dependencies are found
  warn_at_percent: 80      # daily-run warning threshold (needs sign-off)

ui:
  display: terminal        # terminal = show workers in visible terminal windows; headless = under the hood
  notify: true             # one desktop notification when a task run finishes (never mid-run)
  alt_screen: true         # chat uses its own screen; your terminal is restored as it was on exit

sessions:
  reuse: fork              # fork | off  - continue context from your recent sessions
  reuse_max_age_hours: 48
"""


@dataclass
class ProviderSettings:
    name: str
    enabled: bool = True
    command: str = ""
    model: Optional[str] = None
    brain_model: Optional[str] = None
    max_runs_per_day: int = 300
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Limits:
    max_parallel: int = 4
    step_timeout_sec: int = 1200
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

    @property
    def provider_order(self) -> List[str]:
        return [n for n, p in self.providers.items() if p.enabled]


def foreman_home() -> str:
    """~/.foreman (or $FOREMAN_HOME). Data from the name Switchyard, ~/.switchyard, is moved here on first use."""
    env = os.environ.get("FOREMAN_HOME") or os.environ.get("SWITCHYARD_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    home, legacy = os.path.expanduser("~/.foreman"), os.path.expanduser("~/.switchyard")
    if not os.path.exists(home) and os.path.isdir(legacy):
        try:
            os.rename(legacy, home)
        except OSError:
            return legacy
    return home


def load_config(home: Optional[str] = None) -> Config:
    home = home or foreman_home()
    os.makedirs(home, exist_ok=True)
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
        known = {"enabled", "command", "model", "brain_model", "max_runs_per_day"}
        providers[name] = ProviderSettings(
            name=name,
            enabled=bool(p.get("enabled", True)),
            command=str(p.get("command") or name),
            model=p.get("model"),
            brain_model=p.get("brain_model"),
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
