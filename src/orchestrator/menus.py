"""Interactive settings: `foreman config` / `/config` and `foreman models` / `/models`.

Menus only ask and save (config.set_option); what the models and effort levels are comes from the provider CLIs
themselves (Provider.list_models), so a new model or level shows up without a Foreman release. Each function returns
a short text for the caller to show ("" / None when nothing changed). Needs a terminal: callers print a plain summary
instead when there isn't one."""

import re
from typing import List, Optional

from orchestrator import ui
from orchestrator.config import set_first_provider, set_option
from orchestrator.providers.base import ModelInfo
from orchestrator.util import short

MANY = 30  # a model list longer than this is narrowed by typing part of a name first


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


def models_menu(eng) -> Optional[str]:
    """Provider -> model -> effort (only the levels that model supports). Saved to the config; None if cancelled."""
    names = [n for n, p in eng.providers.items() if eng.cfg.providers[n].enabled]
    labels = []
    for n in names:
        ok, info = eng.providers[n].available()
        labels.append(f"{n:<12} {current_text(eng, n) if ok else 'not installed'}")
    pick = ui.choose("Provider", ["Choose the provider whose model and effort you want to change."], labels,
                     allow_text=False, color=ui.FRAME, esc="cancel")
    if not pick:
        return None
    name = names[labels.index(pick)]
    provider = eng.providers[name]
    if not provider.available()[0]:
        return f"{name} isn't installed, so there is nothing to choose."
    ui.out(ui.c(f"  Asking {name} which models it offers…", ui.GREY))
    models = provider.list_models()
    model = _pick_model(eng, name, models)
    if model is None:
        return None
    chosen_id, info = model
    if chosen_id == "default":
        _save(eng, name, "default", None, [])
        return f"{name}: its CLI decides the model and effort (saved)."
    levels = list(info.levels) if info else _levels_for_typed(models)
    effort = None
    if levels:
        effort = _pick_effort(eng, name, chosen_id, levels)
        if effort is None:
            return None
    _save(eng, name, chosen_id, effort or None, levels)
    p = eng.cfg.providers[name]
    light = p.choice("light")
    text = f"{name}: {current_text(eng, name)} (saved)."
    if light != p.chosen():
        text += f" Light steps and planning use effort {light.effort}, one level lower."
    elif not levels:
        text += " This model has a single fixed setting, so there is no effort to choose."
    return text


def _pick_model(eng, name: str, models: List[ModelInfo]):
    """(model id or "default", its ModelInfo or None); None when cancelled."""
    current = eng.cfg.providers[name].chosen().model
    if not models:
        ui.out(ui.c(f"  {name} can't list its models. Type one, or keep its CLI default.", ui.GREY))
        typed = ui.choose("Model", [f"Type the model name for {name}."], [f"CLI default (let {name} decide)"],
                          color=ui.FRAME, esc="cancel")
        if typed is None:
            return None
        return ("default", None) if typed.startswith("CLI default") or not typed else (typed, None)
    pool = models
    groups = sorted({m.group for m in models if m.group})
    browse = len(groups) > 1
    while len(pool) > MANY:  # a long list: narrow it by typing words, or browse one backend at a time
        options = (["Browse by backend"] if browse else []) + [f"Show all {len(pool)}"]
        term = ui.choose("Find a model", [f"{len(pool)} models. Type words to narrow the list: each is a regex and all "
                                          "must match, e.g. deepseek free, or qwen.*coder (not case sensitive; "
                                          "matches the id, which starts with the backend, and the name)."],
                         options, color=ui.FRAME, esc="cancel")
        if term is None:
            return None
        if term == "Browse by backend":
            counts = {g: sum(1 for m in pool if m.group == g) for g in groups}
            mine = next((m.group for m in models if m.id == current), None)
            glabels = [f"{g}  ({counts[g]} models)" for g in groups]
            g = ui.choose(f"{name}: backend", [f"{name} offers {len(models)} models through {len(groups)} backends."],
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
                ui.out(ui.c(f"  '{term}' is not a valid regex.", ui.YELLOW))
            elif hits:
                pool = hits
            else:
                ui.out(ui.c(f"  No model matches '{term}'. Try fewer words, or browse by backend.", ui.YELLOW))
    labels = [f"CLI default (let {name} decide)"]
    for m in pool:
        lv = f"  [{', '.join(m.levels)}]" if m.levels else ""
        labels.append(f"{m.id}" + (f"  {short(m.name, 36)}" if m.name and m.name != m.id else "") + lv
                      + ("   ← current" if m.id == current else ""))
    start = 1 + next((i for i, m in enumerate(pool) if m.id == current), -1) if current else 0
    pick = ui.choose(f"{name}: model", ["Type a model name to use one that isn't listed."], labels,
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
    pick = ui.choose(f"{name}: effort for {model}", body, labels, allow_text=False, start=start, color=ui.FRAME,
                     esc="cancel")
    return levels[labels.index(pick)] if pick else None


def _save(eng, name: str, model: str, effort: Optional[str], levels: List[str]) -> None:
    base = f"providers.{name}"
    set_option(eng.cfg.home, base + ".model", model)
    set_option(eng.cfg.home, base + ".effort", effort)
    set_option(eng.cfg.home, base + ".levels", levels)
    eng.reload_config()


# ---------------------------------------------------------------------------------------------- settings
def _yes_no(title: str, body: str, current: bool) -> Optional[bool]:
    pick = ui.choose(title, [body], ["Yes", "No"], allow_text=False, start=0 if current else 1, color=ui.FRAME,
                     esc="keep it as it is")
    return None if pick is None else pick == "Yes"


def config_menu(eng) -> Optional[str]:
    """A list of settings with their current values; pick one to change it. Esc closes. Returns what changed."""
    changed: List[str] = []
    while True:
        cfg = eng.cfg
        first = next(iter(eng.providers), "?")
        rows = [
            ("display", "Show workers in terminal windows", "yes" if cfg.display == "terminal" else "no (headless)"),
            ("switch", "Switch provider automatically at a limit", "yes" if cfg.auto_switch else "no (wait for it)"),
            ("models", "Models and effort", " · ".join(f"{n} {current_text(eng, n)}" for n in eng.providers
                                                      if cfg.providers[n].enabled)),
            ("planner", "Planner (plans and reviews)", cfg.brain),
            ("first", "First worker", first),
            ("notify", "Desktop notification when a task finishes", "on" if cfg.notify else "off"),
            ("reuse", "Start from your recent sessions", "yes" if cfg.reuse_sessions == "fork" else "no"),
            ("copy", "Copy selected text in the chat", "on" if cfg.copy_on_select else "off"),
        ]
        labels = [ui.clip(f"{name:<42} {value}", ui.width() - 8) for _, name, value in rows]
        pick = ui.choose("Settings", ["Pick a setting to change it. Saved to ~/.foreman/config.yaml.",
                                      "Esc closes this menu."], labels, allow_text=False, color=ui.FRAME, esc="close")
        if not pick:
            return "\n".join(changed) if changed else None
        key = rows[labels.index(pick)][0]
        note = _change(eng, key)
        if note:
            changed.append(note)
        eng.reload_config()


def _change(eng, key: str) -> Optional[str]:
    cfg, home = eng.cfg, eng.cfg.home
    if key == "models":
        return models_menu(eng)
    if key == "display":
        v = _yes_no("Terminal windows", "Open each worker in a visible terminal window (instead of running under the "
                    "hood)?", cfg.display == "terminal")
        if v is None:
            return None
        set_option(home, "ui.display", "terminal" if v else "headless")
        return f"Workers {'open in terminal windows' if v else 'run headless'} (saved)."
    if key == "switch":
        v = _yes_no("Switch automatically", "When a provider reaches its usage limit, hand the step to another provider? "
                    "(No: wait for that provider's reset and continue in the same session.)", cfg.auto_switch)
        if v is None:
            return None
        set_option(home, "auto_switch", v)
        return "Providers switch automatically at a limit (saved)." if v else \
            "Foreman waits for a provider's limit to reset instead of switching (saved)."
    if key == "notify":
        v = _yes_no("Notification", "Show one desktop notification when a task run finishes?", cfg.notify)
        if v is None:
            return None
        set_option(home, "ui.notify", v)
        return f"Notifications {'on' if v else 'off'} (saved)."
    if key == "reuse":
        v = _yes_no("Recent sessions", "Let workers continue from your recent sessions in a project (forked, yours stay "
                    "untouched)?", cfg.reuse_sessions == "fork")
        if v is None:
            return None
        set_option(home, "sessions.reuse", "fork" if v else "off")
        return f"Session reuse {'on' if v else 'off'} (saved)."
    if key == "copy":
        v = _yes_no("Copy on select", "Copy text to the clipboard when you select it with the mouse in the chat?",
                    cfg.copy_on_select)
        if v is None:
            return None
        set_option(home, "ui.copy_on_select", v)
        return f"Copy on select {'on' if v else 'off'} (saved; applies the next time you open the chat)."
    if key in ("planner", "first"):
        names = [n for n in eng.providers if cfg.providers[n].enabled]
        labels = []
        for n in names:
            ok, _ = eng.providers[n].available()
            labels.append(f"{n:<12} {'ready' if ok else 'not installed'}")
        current = cfg.brain if key == "planner" else next(iter(eng.providers), "")
        title, body = ("Planner", "The planner scopes requests, answers questions and reviews work.") if key == "planner" \
            else ("First worker", "Workers make the changes; the first ready one in the list is used.")
        pick = ui.choose(title, [body], labels, allow_text=False, start=names.index(current) if current in names else 0,
                         color=ui.FRAME, esc="cancel")
        if not pick:
            return None
        name = names[labels.index(pick)]
        if not eng.providers[name].available()[0]:
            return f"{name} isn't installed; kept the previous choice."
        if key == "planner":
            set_option(home, "brain", name)
            return f"Planner: {name} (saved)."
        set_first_provider(home, name)
        return f"First worker: {name} (saved)."
    return None
