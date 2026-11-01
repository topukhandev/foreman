"""The settings people change, defined once: the terminal menu (menus.config_menu, `foreman config`, /config) and the
application API (config.get / config.set, models.list / models.set, used by the desktop app) both read and write
them here, so the two never offer different things.

Each setting is a yes/no toggle or a choice among options, saved to ~/.foreman/config.yaml with config.set_option.
Models and effort levels are not listed here: they come from the provider CLIs (Provider.list_models)."""

from typing import Any, Dict, List, Optional, Tuple

from orchestrator.brain import OUTPUT_STYLES
from orchestrator.config import ON_CLOSE, set_first_provider, set_option
from orchestrator.providers.base import ModelInfo

# key: (label, where it applies, read from the config, the config key and the values it saves for on / off)
TOGGLES: Dict[str, Tuple[str, str, Any, str, Any, Any]] = {
    "display": ("Show workers in terminal windows", "all", lambda c: c.display == "terminal", "ui.display", "terminal",
                "headless"),
    "auto_switch": ("Switch provider automatically at a limit", "all", lambda c: c.auto_switch, "auto_switch", True,
                    False),
    "prompt_suggestions": ("Suggest your next message (Tab takes it)", "chat", lambda c: c.prompt_suggestions,
                           "ui.prompt_suggestions", True, False),
    "status_line": ("Status line under the input", "chat", lambda c: c.status_line, "ui.status_line", True, False),
    "notify": ("Desktop notification when a task finishes", "all", lambda c: c.notify, "ui.notify", True, False),
    "reuse_sessions": ("Start from your recent sessions", "all", lambda c: c.reuse_sessions == "fork", "sessions.reuse",
                       "fork", "off"),
    "copy_on_select": ("Copy selected text in the chat", "chat", lambda c: c.copy_on_select, "ui.copy_on_select", True,
                       False),
}
CHOICES = {
    "planner": ("Planner (plans and reviews)", "The planner scopes requests, answers questions and reviews work."),
    "first_worker": ("First worker", "Workers make the changes; the first ready one in the list is used."),
    "output_style": ("Output style (how answers are written)", "How answers and documents are written for you. "
                                                               "Applies from your next request."),
    "on_close": ("Desktop app closed while tasks run", "Tasks run in their own processes, so closing the app doesn't "
                                                       "have to stop them."),
}
# The order the settings are shown in (the models row goes before the planner).
ORDER = ["display", "auto_switch", "planner", "first_worker", "output_style", "prompt_suggestions", "status_line",
         "notify", "reuse_sessions", "copy_on_select", "on_close"]
ON_CLOSE_OPTIONS = {"ask": ("Ask", "keep them running or stop them"),
                    "keep": ("Keep running", "you get the usual notification"),
                    "stop": ("Stop them", "continue them later")}
assert set(ON_CLOSE_OPTIONS) == set(ON_CLOSE)


def first_worker(eng) -> str:
    return next(iter(eng.providers), "")


def _options(eng, key: str) -> List[Dict[str, Any]]:
    if key in ("planner", "first_worker"):
        out = []
        for n in eng.providers:
            if not eng.cfg.providers[n].enabled:
                continue
            ok, info = eng.providers[n].available()
            out.append({"value": n, "label": n, "description": "ready" if ok else "not installed", "available": ok})
        return out
    if key == "output_style":
        return [{"value": k, "label": v[0], "description": v[1], "available": True} for k, v in OUTPUT_STYLES.items()]
    return [{"value": k, "label": v[0], "description": v[1], "available": True} for k, v in ON_CLOSE_OPTIONS.items()]


def value(eng, key: str) -> Any:
    cfg = eng.cfg
    if key in TOGGLES:
        return bool(TOGGLES[key][2](cfg))
    return {"planner": cfg.brain, "first_worker": first_worker(eng), "output_style": cfg.output_style,
            "on_close": cfg.on_close}[key]


def describe(eng) -> List[Dict[str, Any]]:
    """Every setting with its current value; choices with their options. `applies`: all, or chat (the terminal chat
    only)."""
    out = []
    for key in ORDER:
        if key in TOGGLES:
            label, applies = TOGGLES[key][:2]
            out.append({"key": key, "label": label, "kind": "toggle", "applies": applies, "value": value(eng, key)})
        else:
            label, about = CHOICES[key]
            out.append({"key": key, "label": label, "kind": "choice", "applies": "all", "about": about,
                        "value": value(eng, key), "options": _options(eng, key)})
    return out


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if str(v).lower() in ("on", "true", "yes", "1"):
        return True
    if str(v).lower() in ("off", "false", "no", "0"):
        return False
    raise ValueError(f"expected on or off, not {v!r}")


def change(eng, key: str, new: Any) -> None:
    """Save one setting. ValueError (a sentence for the user) when the key or value isn't allowed."""
    home = eng.cfg.home
    if key in TOGGLES:
        _, _, _, cfg_key, on, off = TOGGLES[key]
        set_option(home, cfg_key, on if _bool(new) else off)
    elif key in CHOICES:
        opts = {o["value"]: o for o in _options(eng, key)}
        if new not in opts:
            raise ValueError(f"{CHOICES[key][0]}: choose one of {', '.join(opts)}.")
        if not opts[new]["available"]:
            raise ValueError(f"{new} isn't installed; kept the previous choice.")
        if key == "planner":
            set_option(home, "brain", new)
        elif key == "first_worker":
            set_first_provider(home, new)
        else:
            set_option(home, {"output_style": "ui.output_style", "on_close": "ui.on_close"}[key], new)
    else:
        raise ValueError(f"No setting '{key}'.")
    eng.reload_config()


def flip(eng, key: str) -> None:
    change(eng, key, not value(eng, key))


# ---------------------------------------------------------------------------------------------- models
def levels_for_typed(models: List[ModelInfo]) -> List[str]:
    """A model typed by hand: its effort levels when every listed model shares one list (Claude), else none."""
    sets = {tuple(m.levels) for m in models}
    return list(next(iter(sets))) if len(sets) == 1 else []


def default_effort(levels: List[str]) -> Optional[str]:
    return ("high" if "high" in levels else levels[-1]) if levels else None


def save_model(eng, name: str, model: str, effort: Optional[str], levels: List[str]) -> None:
    base = f"providers.{name}"
    set_option(eng.cfg.home, base + ".model", model)
    set_option(eng.cfg.home, base + ".effort", effort)
    set_option(eng.cfg.home, base + ".levels", levels)
    eng.reload_config()


def choose_model(eng, name: str, model: str, effort: Optional[str] = None,
                 models: Optional[List[ModelInfo]] = None) -> Tuple[str, Optional[str]]:
    """Save a provider's model ("default": the CLI decides) and effort. The effort must be one the model supports
    (as the CLI lists them); without one, "high" or the highest it has. Returns what was saved. ValueError when it
    can't be."""
    if name not in eng.providers or not eng.cfg.providers[name].enabled:
        raise ValueError(f"No provider '{name}'.")
    model = str(model or "").strip()
    if not model:
        raise ValueError("Give a model name, or default.")
    if model == "default":
        save_model(eng, name, "default", None, [])
        return "default", None
    if not eng.providers[name].available()[0]:
        raise ValueError(f"{name} isn't installed, so there is nothing to choose.")
    models = eng.providers[name].list_models() if models is None else models
    info = next((m for m in models if m.id == model), None)
    levels = list(info.levels) if info else levels_for_typed(models)
    if effort and levels and effort not in levels:
        raise ValueError(f"{model} on {name} supports effort {', '.join(levels)}; not {effort}.")
    effort = (effort or default_effort(levels)) if levels else None
    save_model(eng, name, model, effort, levels)
    return model, effort
