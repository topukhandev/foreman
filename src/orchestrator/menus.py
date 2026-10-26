"""Interactive settings: `foreman config` / `/config` and `foreman models` / `/models`.

Menus only ask and save (config.set_option); what the models and effort levels are comes from the provider CLIs
themselves (Provider.list_models), so a new model or level shows up without a Foreman release. Each function returns
a short text for the caller to show ("" / None when nothing changed). Needs a terminal: callers print a plain summary
instead when there isn't one."""

import re
from typing import Dict, List, Optional

from orchestrator import ui
from orchestrator.config import set_first_provider, set_option
from orchestrator.providers.base import ModelInfo
from orchestrator.util import short

MANY = 30  # a model list longer than this is narrowed by typing part of a name first


def _panel(title, body, options, **kw):
    """A settings panel: drawn at the bottom, gone when it closes, nothing left in the chat."""
    return ui.choose(title, body, options, transient=True, **kw)


def current_text(eng, name: str) -> str:
    """What the provider uses now, for menus and summaries: "claude-sonnet-5-5 · effort high"."""
    c = eng.cfg.providers[name].chosen()
    return " · ".join(x for x in (c.model or "CLI default", f"effort {c.effort}" if c.effort else "") if x)


def models_summary(eng) -> str:
    """Non-interactive view: the model and effort every provider uses, and what light work and planning get."""
    lines = ["Model and effort per provider (change with `foreman models`, or /models in the chat):"]
    for name, p in eng.cfg.providers.items():
        if not p.enabled:
            continue
        light = p.choice("light")
        extra = f"   light work and planning: {light.label()}" if light != p.chosen() else ""
        lines.append(f"  {name:<12} {current_text(eng, name)}{extra}")
    return "\n".join(lines)


def models_menu(eng):
    """Provider -> model -> effort (only the levels that model supports). Saved to the config. True when something was
    saved, a sentence when it couldn't be, None when cancelled."""
    names = [n for n, p in eng.providers.items() if eng.cfg.providers[n].enabled]
    labels = []
    for n in names:
        ok, info = eng.providers[n].available()
        labels.append(f"{n:<12} {current_text(eng, n) if ok else 'not installed'}")
    pick = _panel("Provider", ["Choose the provider whose model and effort you want to change."], labels,
                  allow_text=False, color=ui.FRAME, esc="close")
    if not pick:
        return None
    name = names[labels.index(pick)]
    provider = eng.providers[name]
    if not provider.available()[0]:
        return f"{name} isn't installed, so there is nothing to choose."
    with ui.busy(f"Asking {name} which models it offers…"):
        models = provider.list_models()
    model = _pick_model(eng, name, models)
    if model is None:
        return None
    chosen_id, info = model
    if chosen_id == "default":
        _save(eng, name, "default", None, [])
        return True
    levels = list(info.levels) if info else _levels_for_typed(models)
    effort = None
    if levels:
        effort = _pick_effort(eng, name, chosen_id, levels)
        if effort is None:
            return None
    _save(eng, name, chosen_id, effort or None, levels)
    return True


def _pick_model(eng, name: str, models: List[ModelInfo]):
    """(model id or "default", its ModelInfo or None); None when cancelled."""
    current = eng.cfg.providers[name].chosen().model
    if not models:
        typed = _panel("Model", [f"{name} can't list its models. Type the model name, or keep its CLI default."],
                       [f"CLI default (let {name} decide)"],
                          color=ui.FRAME, esc="cancel")
        if typed is None:
            return None
        return ("default", None) if typed.startswith("CLI default") or not typed else (typed, None)
    pool = models
    groups = sorted({m.group for m in models if m.group})
    browse = len(groups) > 1
    note = ""
    while len(pool) > MANY:  # a long list: narrow it by typing words, or browse one backend at a time
        options = (["Browse by backend"] if browse else []) + [f"Show all {len(pool)}"]
        term = _panel("Find a model", ([note] if note else []) + [
            f"{len(pool)} models. Type words to narrow the list: each is a regex and all must match, e.g. "
            "deepseek free, or qwen.*coder (not case sensitive; matches the id, which starts with the backend, "
            "and the name)."], options, color=ui.FRAME, esc="cancel")
        note = ""
        if term is None:
            return None
        if term == "Browse by backend":
            counts = {g: sum(1 for m in pool if m.group == g) for g in groups}
            mine = next((m.group for m in models if m.id == current), None)
            glabels = [f"{g}  ({counts[g]} models)" for g in groups]
            g = _panel(f"{name}: backend", [f"{name} offers {len(models)} models through {len(groups)} backends."],
                          glabels, allow_text=False, start=groups.index(mine) if mine in groups else 0,
                          color=ui.FRAME, esc="cancel")
            if not g:
                return None
            pool, browse = [m for m in pool if m.group == groups[glabels.index(g)]], False
        elif term.startswith("Show all") or not term:
            break
        else:
            hits = search(pool, term)
            if hits is None:
                note = f"'{term}' is not a valid regex."
            elif hits:
                pool = hits
            else:
                note = f"No model matches '{term}'. Try fewer words, or browse by backend."
    labels = [f"CLI default (let {name} decide)"]
    for m in pool:
        lv = f"  [{', '.join(m.levels)}]" if m.levels else ""
        labels.append(f"{m.id}" + (f"  {short(m.name, 36)}" if m.name and m.name != m.id else "") + lv
                      + ("   ← current" if m.id == current else ""))
    start = 1 + next((i for i, m in enumerate(pool) if m.id == current), -1) if current else 0
    pick = _panel(f"{name}: model", ["Type a model name to use one that isn't listed."], labels,
                     start=start, color=ui.FRAME, esc="cancel")
    if pick is None:
        return None
    if pick in labels:
        i = labels.index(pick)
        return ("default", None) if i == 0 else (pool[i - 1].id, pool[i - 1])
    return ("default", None) if not pick else (pick, next((m for m in models if m.id == pick), None))


def search(models: List[ModelInfo], pattern: str) -> Optional[List[ModelInfo]]:
    """Models matching every word of a search: each word is a regex (not case sensitive) tried on the id and the name.
    None when a word isn't a valid regex."""
    try:
        words = [re.compile(w, re.I) for w in pattern.split()]
    except re.error:
        return None
    return [m for m in models if all(w.search(m.id + " " + m.name) for w in words)]


def _levels_for_typed(models: List[ModelInfo]) -> List[str]:
    """A model typed by hand: its effort levels when every listed model shares one list (Claude), else none."""
    sets = {tuple(m.levels) for m in models}
    return list(next(iter(sets))) if len(sets) == 1 else []


def _pick_effort(eng, name: str, model: str, levels: List[str]) -> Optional[str]:
    c = eng.cfg.providers[name].chosen()
    start = levels.index(c.effort) if c.model == model and c.effort in levels else (
        levels.index("high") if "high" in levels else len(levels) - 1)
    body = [f"Effort levels {model} supports on {name}. Higher is slower and uses more of your limits.",
            "Light steps and planning run one level lower."]
    labels = [lv + ("   ← current" if c.model == model and lv == c.effort else "") for lv in levels]
    pick = _panel(f"{name}: effort for {model}", body, labels, allow_text=False, start=start, color=ui.FRAME,
                     esc="cancel")
    return levels[labels.index(pick)] if pick else None


def _save(eng, name: str, model: str, effort: Optional[str], levels: List[str]) -> None:
    base = f"providers.{name}"
    set_option(eng.cfg.home, base + ".model", model)
    set_option(eng.cfg.home, base + ".effort", effort)
    set_option(eng.cfg.home, base + ".levels", levels)
    eng.reload_config()


# ---------------------------------------------------------------------------------------------- settings
def snapshot(eng) -> Dict[str, str]:
    """Every setting the menus change, as short values (to report only what ended up different)."""
    cfg = eng.cfg
    on = lambda v: "on" if v else "off"  # noqa: E731
    out = {"terminal windows": on(cfg.display == "terminal"), "auto-switch at limits": on(cfg.auto_switch),
           "notifications": on(cfg.notify), "session reuse": on(cfg.reuse_sessions == "fork"),
           "copy on select": on(cfg.copy_on_select), "planner": cfg.brain, "first worker": next(iter(eng.providers), "?")}
    for n, p in cfg.providers.items():
        if p.enabled:
            c = p.chosen()
            out[f"{n} model"] = (c.model or "CLI default") + (f" ({c.effort})" if c.effort else "")
    return out


def changes(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """One short line per setting whose value differs from before ("Set auto-switch at limits to off")."""
    return [f"Set {k} to {v}" for k, v in after.items() if before.get(k) != v]


def models_loop(eng) -> Optional[str]:
    """The models menu until Esc: several providers can be changed in one go. Returns the lines to show."""
    before, problems = snapshot(eng), []
    while True:
        res = models_menu(eng)
        if res is None:
            break
        if isinstance(res, str):
            problems.append(res)
    return "\n".join(changes(before, snapshot(eng)) + problems) or None


def _toggle(eng, key: str) -> None:
    """Flip a true/false setting right away (Enter on its row) and save it."""
    cfg = eng.cfg
    if key == "display":
        set_option(cfg.home, "ui.display", "headless" if cfg.display == "terminal" else "terminal")
    elif key == "switch":
        set_option(cfg.home, "auto_switch", not cfg.auto_switch)
    elif key == "notify":
        set_option(cfg.home, "ui.notify", not cfg.notify)
    elif key == "reuse":
        set_option(cfg.home, "sessions.reuse", "off" if cfg.reuse_sessions == "fork" else "fork")
    else:  # "copy": applies the next time the chat opens
        set_option(cfg.home, "ui.copy_on_select", not cfg.copy_on_select)


TOGGLES = ("display", "switch", "notify", "reuse", "copy")


def config_menu(eng) -> Optional[str]:
    """A list of settings with their current values. Enter flips a yes/no setting on the spot and opens a panel for the
    others; Esc closes. Nothing is left in the chat while it is open. Returns what changed (one line each)."""
    before, at, problems = snapshot(eng), 0, []
    while True:
        cfg = eng.cfg
        first = next(iter(eng.providers), "?")
        yes = lambda v: "true" if v else "false"  # noqa: E731
        rows = [
            ("display", "Show workers in terminal windows", yes(cfg.display == "terminal")),
            ("switch", "Switch provider automatically at a limit", yes(cfg.auto_switch)),
            ("models", "Models and effort", " · ".join(f"{n} {current_text(eng, n)}" for n in eng.providers
                                                      if cfg.providers[n].enabled)),
            ("planner", "Planner (plans and reviews)", cfg.brain),
            ("first", "First worker", first),
            ("notify", "Desktop notification when a task finishes", yes(cfg.notify)),
            ("reuse", "Start from your recent sessions", yes(cfg.reuse_sessions == "fork")),
            ("copy", "Copy selected text in the chat", yes(cfg.copy_on_select)),
        ]
        labels = [ui.clip(f"{name:<42} {value}", ui.width() - 8) for _, name, value in rows]
        pick = _panel("Settings", ["Enter changes the selected setting (yes/no ones flip right away). Saved to "
                                   "~/.foreman/config.yaml.", "Esc closes this menu."], labels, allow_text=False,
                      start=at, color=ui.FRAME, esc="close", enter="change")
        if not pick:
            return "\n".join(changes(before, snapshot(eng)) + problems) or None
        at = labels.index(pick)
        key = rows[at][0]
        if key in TOGGLES:
            _toggle(eng, key)
        else:
            note = _change(eng, key)
            if note and key != "models":  # a sentence that is not a settings change: a problem to show
                problems.append(note)
        eng.reload_config()


def _change(eng, key: str) -> Optional[str]:
    cfg, home = eng.cfg, eng.cfg.home
    if key == "models":
        models_loop(eng)
        return None
    if key in ("planner", "first"):
        names = [n for n in eng.providers if cfg.providers[n].enabled]
        labels = []
        for n in names:
            ok, _ = eng.providers[n].available()
            labels.append(f"{n:<12} {'ready' if ok else 'not installed'}")
        current = cfg.brain if key == "planner" else next(iter(eng.providers), "")
        title, body = ("Planner", "The planner scopes requests, answers questions and reviews work.") if key == "planner" \
            else ("First worker", "Workers make the changes; the first ready one in the list is used.")
        pick = _panel(title, [body], labels, allow_text=False, start=names.index(current) if current in names else 0,
                         color=ui.FRAME, esc="cancel")
        if not pick:
            return None
        name = names[labels.index(pick)]
        if not eng.providers[name].available()[0]:
            return f"{name} isn't installed; kept the previous choice."
        if key == "planner":
            set_option(home, "brain", name)
        else:
            set_first_provider(home, name)
        return None
    return None
