"""Interactive chat: run `foreman` with no arguments. Claude Code-inspired terminal UI (see ui.py).

Plain language for everything: questions, changes, "status", "continue", answering a task's question,
accepting a suggestion. Each task runs in a detached process; the chat streams its progress from the
task's event log, so Ctrl+C can either stop the task or leave it running in the background.
"""

import json
import os
import re
import signal
import time
from typing import Any, Dict, List, Optional, Tuple

from orchestrator import ui
from orchestrator.app import build_engine, spawn_task
from orchestrator.config import set_first_provider, set_option
from orchestrator.engine import NO_RE, RESUME_RE, YES_RE, clean_text, is_task_status_query
from orchestrator.report import render_report
from orchestrator.store import ACTIVE
from orchestrator.util import short

COMMANDS = [
    ("/help", "how to use the chat"),
    ("/status", "state of the current task (instant)"),
    ("/tasks", "recent tasks"),
    ("/show", "full report of a task, e.g. /show 12"),
    ("/resume", "pick up an earlier conversation in this project"),
    ("/new", "start a new conversation"),
    ("/display", "terminal | headless: show workers in windows or run under the hood"),
    ("/projects", "projects related to this folder"),
    ("/suggestions", "open suggestions"),
    ("/providers", "choose the planner (and first worker) with ↑/↓"),
    ("/mode", "lifecycle mode: R&D, planning, development, QA, deployment, or auto"),
    ("/lifecycle", "where this project's features stand and what's next"),
    ("/doctor", "providers, health and setup"),
    ("/exit", "leave the chat (Ctrl+D)"),
]
HELP = """\
Just type what you need, for example:
  • what's the current state of this project?
  • add a currency to orders in the API and show it in the admin panel
  • what's the status of my task? · continue the previous task · yes / no
Shortcuts (optional):
  /status [task]  /tasks  /show <task>  /new (fresh topic)  /resume (old chats)
  /mode  /lifecycle  /providers  /display terminal|headless  /projects  /suggestions  /exit
Modes: I recommend one (R&D, planning, development, QA, deployment); /mode fixes it. "next" starts the next step.
Wheel or PgUp/PgDn scroll the chat; hold Shift to select text with the mouse.
While a task runs, Ctrl+C lets you stop it or keep it running in the background."""
ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}  # not "one": "the second one"
SECTION_TITLES = {"What changed", "Needs your decision", "Remaining", "Notes", "Worth considering (your call)"}


class Chat:
    def __init__(self, cwd: str, visual: Optional[bool] = None, engine=None):
        self.cwd = os.path.abspath(cwd)
        self.visual = visual
        self.eng = engine or build_engine(visual=visual, out=lambda _m: None)
        self.turns: List[Tuple[str, str]] = []
        self.focus: Optional[str] = None      # task the conversation is currently about
        self.background: List[str] = []      # tasks left running with Ctrl+C
        self._last_ctrl_c = 0.0
        self._last_where: Optional[str] = None
        self.queue: List[str] = []           # messages typed while a task was running
        self.history = ChatHistory(self.eng.cfg.home, self._project_key)
        self.last_providers: Optional[str] = None
        self.mode: Optional[str] = None       # lifecycle mode fixed with /mode (None = recommended per request)
        self._offered_next: set = set()

    # ------------------------------------------------------------------ loop
    def run(self, resume: Optional[str] = None) -> None:
        """resume: None (new chat), "last" (continue the latest chat here) or "pick" (choose from a list)."""
        ui.set_editor(ui.LineEditor(COMMANDS, os.path.join(self.eng.cfg.home, "chat_history")))
        with ui.FullScreen(self.eng.cfg.alt_screen):
            if not self.eng.registry.projects:
                with ui.Spinner("Indexing your projects", footer=False):
                    self.eng.registry.ensure_scanned()
            ui.out(self.banner())
            if resume == "last":
                chats = self.history.list()
                if chats:
                    ui.out(self._load_chat(chats[0]["id"]))
            elif resume == "pick":
                ui.out(self._resume_menu())
            self._loop()

    def _loop(self) -> None:
        while True:
            self._announce_background()
            if self.queue:  # typed while the previous task was running
                self._exchange(self.queue.pop(0))
                continue
            try:
                line = ui.read_input(*self._hints()).strip()
            except EOFError:
                return
            except KeyboardInterrupt:
                if time.time() - self._last_ctrl_c < 2:
                    return
                self._last_ctrl_c = time.time()
                ui.out(ui.c("  Press Ctrl+C again to exit", ui.GREY))
                continue
            if not line:
                continue
            if line in ("exit", "quit", "/exit", "/quit"):
                return
            self._exchange(line)

    def _exchange(self, line: str) -> None:
        """One user message: echo it, respond, remember it in this project's chat history, offer pending choices."""
        ui.out("\n" + ui.user_echo(line))
        out = self.respond(line)
        if out:
            ui.out("\n" + out + "\n")
        if not line.startswith(("/resume", "/new")):
            self.history.append(line, out or "", self.turns, self.focus)
        self._offer_choices()

    def banner(self) -> str:
        proj = self.eng.registry.project_for_path(self.cwd)
        cwd = ui.short_path(self.cwd, ui.width() - 16)
        skipped = self.eng.unhealthy()
        lines = [f"{ui.c('◈', ui.ACCENT, ui.BOLD)} {ui.c('Foreman', ui.BOLD, ui.ACCENT)}  {ui.c('· orchestrating your AI sessions', ui.GREY)}", "",
                 ui.c("  /help for help · Ctrl+D to exit", ui.GREY), "",
                 ui.c(f"  cwd: {cwd}", ui.GREY)]
        if proj:
            rel = {r.dst if r.src == proj.name else r.src for r in self.eng.registry.relations_of(proj.name)
                   if r.type != "mentions"}
            lines.append(ui.c(f"  project: {proj.name} ({', '.join(proj.stack) or '?'})"
                              + (f" · related: {', '.join(sorted(rel)[:4])}" if rel else ""), ui.GREY))
        lines.append(ui.c(f"  {self._providers_label(long=True)} · display: "
                          f"{'terminal windows' if self._visual() else 'headless'}", ui.GREY))
        for name, why in skipped.items():
            lines.append(ui.c(f"  skipping {name} for now: {short(why, 60)}", ui.YELLOW))
        out = [ui.box(lines, color=ui.FRAME)]
        if proj:
            for t in [t for t in self.eng.store.all() if t["status"] in ACTIVE and proj.name in t["projects"]][:3]:
                out.append(ui.c(f"  open task #{t['id']} · {short(t['title'], 60)} ({t['status'].replace('_', ' ')})", ui.GREY))
        out.append(ui.c("  Try: \"what's the current state of this project?\"", ui.GREY))
        return "\n" + "\n".join(out) + "\n"

    def _hints(self) -> Tuple[str, str]:
        focus = self.eng.store.load(self.focus) if self.focus else None
        if focus and any(not q.get("answer") for q in focus["questions"]):
            left = f"answering #{focus['id']} · /new for something else"
        elif self.background:
            left = f"{len(self.background)} task(s) running in background · \"status\" to check"
        else:
            left = "/help for shortcuts"
        proj = self.eng.registry.project_for_path(self.cwd)
        who = self.last_providers or f"planner: {self._planner()}"
        mode = self.eng.modes(self.cwd).get(self.mode) if self.mode else None
        right = f"{proj.name if proj else os.path.basename(self.cwd)} · " + (f"mode: {mode.title} · " if mode else "") \
                + f"{who} · {'terminal' if self._visual() else 'headless'}"
        return left, right

    def _resume_menu(self) -> str:
        chats = [c for c in self.history.list() if c["id"] != self.history.chat_id and c["entries"]]
        if not chats:
            return ui.bullet("No earlier conversations in this project yet.", ui.GREY)
        labels = [f"{short(c['title'] or '(untitled)', 52):<52}  {_ago(c['updated_at']):>8} · {len(c['entries'])} msg"
                  for c in chats[:20]]
        if not ui.TTY:
            return "\n".join(labels)
        choice = ui.choose("Resume a conversation", [f"Conversations in {self.history.project}"], labels,
                           allow_text=False, color=ui.FRAME, esc="cancel")
        if not choice:
            return ui.bullet("Kept the current conversation.", ui.GREY)
        return self._load_chat(chats[labels.index(choice)]["id"])

    def _load_chat(self, chat_id: str) -> str:
        data = self.history.load(chat_id)
        if not data:
            return ui.bullet("That conversation could not be loaded.", ui.YELLOW)
        self.turns = [tuple(t) for t in data.get("turns", [])]
        self.focus = data.get("focus")
        if ui.TTY:
            ui.clear_screen()
            ui.out(self.banner())
        shown = data["entries"][-200:]
        if len(data["entries"]) > len(shown):
            ui.out(ui.c(f"  … {len(data['entries']) - len(shown)} earlier messages", ui.GREY))
        for e in shown:
            if e["user"]:
                ui.out("\n" + ui.user_echo(e["user"]))
            if e["output"]:
                ui.out("\n" + e["output"] + "\n")
        return ui.bullet(f"Resumed \"{short(data.get('title', ''), 60)}\". Carry on where you left off.", ui.ACCENT)

    def _providers_menu(self, arg: str) -> str:
        aliases = {"agy": "antigravity", "antigravity": "antigravity", "claude": "claude", "opencode": "opencode"}
        rows = []
        for name, p in self.eng.providers.items():
            ok, info = p.available()
            bad = self.eng.unhealthy().get(name)
            status = "ready" if ok and not bad else f"skipped: {short(bad, 45)}" if bad else \
                "not installed" if "not found" in info else info
            rows.append((name, status))
        first = next((n for n in self.eng.providers if self.eng._usable(n)), None)
        if arg:
            name, role = aliases.get(arg.lower(), arg.lower()), "Planning only"
        else:
            labels = []
            for n, status in rows:
                tags = [t for t, on in (("planner", n == self.eng.cfg.brain), ("first worker", n == first)) if on]
                labels.append(f"{n:<12} {status}" + (f"   ← {', '.join(tags)}" if tags else ""))
            current = next((i for i, (n, _) in enumerate(rows) if n == self.eng.cfg.brain), 0)
            choice = ui.choose("Providers", ["The planner scopes requests, answers questions and reviews work.",
                                             "Workers make the changes; the first ready one in the list is used."],
                               labels, allow_text=False, start=current, color=ui.FRAME, esc="cancel")
            if not choice:
                return ui.bullet("Unchanged.", ui.GREY)
            name = rows[labels.index(choice)][0]
            role = ui.choose(f"Use {name} for", [f"{name} will be used from your next request."],
                             ["Planning only", "Planning and as the first worker"], allow_text=False,
                             color=ui.FRAME, esc="cancel")
            if not role:
                return ui.bullet("Unchanged.", ui.GREY)
        if name not in self.eng.providers:
            return ui.bullet(f"Unknown provider '{name}'. Available: {', '.join(self.eng.providers)}.", ui.YELLOW)
        if not self.eng._usable(name):
            status = dict(rows).get(name, "not ready")
            return ui.bullet(f"{name} isn't ready ({status}), so I kept {self.eng.cfg.brain}.", ui.YELLOW)
        set_option(self.eng.cfg.home, "brain", name)
        self.eng.cfg.brain = name
        msg = f"Planner: {name}"
        if role.startswith("Planning and"):
            set_first_provider(self.eng.cfg.home, name)
            self.eng.providers = {name: self.eng.providers[name],
                                  **{k: v for k, v in self.eng.providers.items() if k != name}}
            msg += f" · first worker: {name}"
        self.last_providers = None
        return ui.bullet(msg + " (saved)", ui.GREEN)

    def _project_key(self) -> str:
        self.eng.registry.ensure_scanned()
        proj = self.eng.registry.project_for_path(self.cwd)
        return proj.name if proj else self.cwd

    def _planner(self) -> str:
        usable = [n for n in self.eng.providers if self.eng._usable(n)]
        return self.eng.cfg.brain if self.eng.cfg.brain in usable else (usable[0] if usable else "none")

    def _used_providers(self, task_id: str) -> Optional[str]:
        """Who actually worked on a task (failed attempts excluded): workers, else the planner; plus the reviewer."""
        planner, review, workers = None, None, []
        for ev in self.eng.store.events(task_id):
            e, prov = ev.get("event"), ev.get("provider")
            if e == "thinking" and ev.get("purpose") == "review":
                review = prov
            elif e == "thinking" and planner is None:
                planner = prov
            elif e == "thinking_failed":
                planner = None if planner == prov else planner
                review = None if review == prov else review
            elif e == "step_start":
                workers.append(prov)
            elif e == "step_failed" and prov in workers:
                workers.remove(prov)
        names = {"antigravity": "agy"}
        main = [names.get(p, p) for p in dict.fromkeys(workers or [planner]) if p]
        if not main:
            return None
        return "last: " + ", ".join(main) + (f" · review: {names.get(review, review)}" if review else "")

    def _providers_label(self, long: bool = False) -> str:
        usable = [n for n in self.eng.providers if self.eng._usable(n)]
        brain = self.eng.cfg.brain if self.eng.cfg.brain in usable else (usable[0] if usable else "none")
        if long:
            return f"planner: {brain} · workers: {', '.join(usable) or 'none'}"
        short_names = {"antigravity": "agy"}
        return ", ".join(short_names.get(n, n) for n in usable) or "no providers"

    def _visual(self) -> bool:
        return self.eng.visual if self.visual is None else self.visual

    # ------------------------------------------------------------------ routing
    def respond(self, text: str) -> str:
        if text.startswith("/"):
            return self._slash(text)
        focus = self.eng.store.load(self.focus) if self.focus else None
        waiting = [q for q in (focus or {}).get("questions", []) if not q.get("answer")]
        if focus and waiting and not is_task_status_query(text):
            task, msg = self.eng.prepare_answer(focus["id"], text)
            return self._run(text, task, msg)
        handled = self._suggestion_reply(focus, text)
        if handled is not None:
            return handled
        if (YES_RE.match(text) or NO_RE.match(text)) and len(text.split()) <= 3 and not RESUME_RE.search(text):
            return ui.bullet("Nothing is waiting for a yes/no right now. What would you like to do?")
        task, msg = self.eng.route(text, self.cwd, self._conversation(), confirm_plan=True, mode=self.mode)
        return self._run(text, task, msg)

    def _mode_menu(self, arg: str) -> str:
        modes = self.eng.modes(self.cwd)
        if arg:
            if arg.lower() in ("auto", "off", "none"):
                self.mode = None
                return ui.bullet("Mode: auto. I'll recommend a mode for each request.")
            from orchestrator.modes import resolve
            name = resolve(arg, modes)
            if not name:
                return ui.bullet(f"Unknown mode '{arg}'. Modes: auto, " + ", ".join(m.title for m in modes.values()), ui.YELLOW)
            self.mode = name
            return ui.bullet(f"Mode: {modes[name].title}. {modes[name].summary} (/mode auto to go back)")
        if not ui.TTY:
            return "Modes: auto, " + ", ".join(f"{m.title} ({n})" for n, m in modes.items())
        labels = ["Auto: recommend a mode for each request"] + [f"{m.title}: {m.summary}" for m in modes.values()]
        current = 0 if not self.mode else 1 + list(modes).index(self.mode)
        choice = ui.choose("Lifecycle mode", ["Applies to your next requests in this chat."], labels,
                           allow_text=False, start=current, color=ui.FRAME, esc="keep the current mode")
        if choice is None:
            return ui.bullet("Mode unchanged.", ui.GREY)
        return self._mode_menu("auto" if choice.startswith("Auto") else list(modes)[labels.index(choice) - 1])

    def _suggestion_reply(self, focus, text: str) -> Optional[str]:
        if not focus or not focus.get("suggestions"):
            return None
        items = [s for s in self.eng.store.suggestions() if s["id"] in focus["suggestions"] and s["status"] == "open"]
        if not items:
            return None
        accept = YES_RE.match(text) or re.match(r"^\s*(accept|apply|do|let'?s do|go with)\b", text, re.I)
        dismiss = re.match(r"^\s*(dismiss|ignore|skip|no thanks|not now|no)\b", text, re.I)
        if not (accept or dismiss):
            return None
        chosen = [s for s in items if re.search(rf"\b{s['id']}\b", text)]
        for word, idx in ORDINALS.items():
            if re.search(rf"\b{word}\b", text, re.I) and idx < len(items):
                chosen.append(items[idx])
        if not chosen:
            if dismiss or len(items) == 1:
                chosen = items
            else:
                return ui.bullet("Which one? " + ", ".join(f"{s['id']} ({short(s['text'], 50)})" for s in items))
        if dismiss:
            for s in chosen:
                self.eng.store.set_suggestion(s["id"], "dismissed")
            n = len(chosen)
            return ui.bullet(f"Dismissed {', '.join(s['id'] for s in chosen)}; I won't suggest {'it' if n == 1 else 'them'} again.")
        s = chosen[0]
        self.eng.store.set_suggestion(s["id"], "accepted")
        proj = self.eng.registry.projects.get(s["project"])
        task = self.eng.prepare_new(s["text"], proj.path if proj else self.cwd, self._conversation(), confirm_plan=True)
        return self._run(text, task, "")

    def _slash(self, text: str) -> str:
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        if cmd == "/help":
            return HELP
        if cmd == "/status":
            return self._style(self.eng.status(arg or self.focus, self.cwd)["report"])
        if cmd == "/tasks":
            rows = [f"#{t['id']:<4} {t['status'].replace('_', ' '):<12} {short(t['title'], 70)}"
                    for t in self.eng.store.all()[:12]]
            return "\n".join(rows) or "No tasks yet."
        if cmd == "/show":
            cands = self.eng.store.resolve(arg or self.focus, fallback=False)
            return self.render(cands[0]) if cands else "Not found."
        if cmd == "/new":
            self.focus, self.turns = None, []
            self.history.start_new()
            return ui.bullet("Started a new conversation. The previous one is in /resume.")
        if cmd == "/resume":
            return self._resume_menu()
        if cmd == "/display":
            if arg in ("terminal", "headless"):
                set_option(self.eng.cfg.home, "ui.display", arg)
                self.visual = arg == "terminal"
                return ui.bullet(f"Display: {arg} (saved).")
            return "Usage: /display terminal|headless"
        if cmd == "/providers":
            return self._providers_menu(arg)
        if cmd == "/mode":
            return self._mode_menu(arg)
        if cmd == "/lifecycle":
            return ui.bullet(self.eng.lifecycle_text(self.cwd))
        if cmd == "/doctor":
            lines = []
            for name, p in self.eng.providers.items():
                ok, info = p.available()
                bad = self.eng.unhealthy().get(name)
                mark = ui.c("✔", ui.GREEN) if ok and not bad else ui.c("✘", ui.RED)
                lines.append(f"  {mark} {name:<12} {('skipped: ' + bad) if bad else info}")
            return "\n".join(lines)
        if cmd == "/projects":
            return self.eng.registry.render_context(self.eng.registry.select("", self.cwd))
        if cmd == "/suggestions":
            items = [s for s in self.eng.store.suggestions() if s["status"] == "open"]
            return "\n".join(f"[{s['id']}] {s['text']}" for s in items) or "No open suggestions."
        return f"Unknown shortcut {cmd}. /help lists them."

    # ------------------------------------------------------------------ execution
    def _run(self, user_text: str, task: Optional[Dict[str, Any]], msg: str) -> str:
        if not task:
            self.turns.append((user_text, short(msg, 300)))
            return self._style(msg) if msg.lstrip()[:1] in "✔✘◐?…!" else ui.bullet(msg)
        self.focus = task["id"]
        self._execute(task)
        final = self.eng.store.load(task["id"])
        self.last_providers = self._used_providers(task["id"]) or self.last_providers
        self.turns.append((user_text, f"#{final['id']} {final['title']} — {final.get('outcome') or final['status']}: "
                                      f"{short(final.get('summary', ''), 200)}"))
        if task["id"] in self.background:
            return ui.bullet(f"#{task['id']} keeps running in the background. I'll show the result here when it "
                             "finishes (you also get a desktop notification).", ui.GREY)
        return self.render(final)

    def render(self, task: Dict[str, Any]) -> str:
        if task["status"] == "answered" and task.get("answer") and not task.get("document") and not any(
                s["kind"] == "implement" for s in task["steps"]):
            secs = _seconds(task)
            foot = f"{task.get('answered_by') or 'planner'}" + (f" · {secs}s" if secs else "")
            out = [ui.bullet(clean_text(task["answer"]))]
            new = [s for s in self.eng.store.suggestions() if s["id"] in task.get("suggestions", [])]
            if new:
                out += ["", "  " + ui.c("Worth considering (your call)", ui.BOLD)]
                out += [ui.wrap_block(f"[{s['id']}] {s['text']} — {s['why']}", indent="      ", first="    ") for s in new]
                out.append(ui.c(f"    → say \"do {new[0]['id']}\" or \"dismiss {new[0]['id']}\"", ui.GREY))
            return "\n".join(out) + "\n" + ui.c("  " + foot, ui.GREY)
        return self._style(render_report(task, self.eng.store.suggestions(), questions=not ui.TTY, chat=True))

    def _execute(self, task: Dict[str, Any]) -> None:
        """Run the task in a detached process and stream its progress (tests override this)."""
        proc = spawn_task(self.eng.cfg.home, task["id"], self.visual)
        self._watch(task["id"], proc)

    def _watch(self, task_id: str, proc) -> None:
        events = self.eng.store.path(task_id, "events.jsonl")
        pos = os.path.getsize(events) if os.path.exists(events) else 0
        proj = self.eng.registry.project_for_path(self.cwd)
        phase = Phase(self.eng.store.load(task_id) or {})
        spin = ui.Spinner(phase.verb, hint_right=f"{proj.name if proj else os.path.basename(self.cwd)} · "
                                                 f"{'terminal' if self._visual() else 'headless'}")
        keys = _KeyReader()
        spin.begin()
        stopping = False
        try:
            while True:
                try:
                    pos = self._drain(events, pos, spin, phase)
                    if proc.poll() is not None:
                        self._drain(events, pos, spin, phase)
                        return
                    data = keys.read(0.25)
                    if data:
                        spin.key(data)
                except KeyboardInterrupt:
                    if stopping:
                        continue
                    spin.pause()
                    title = short(self.eng.store.load(task_id)["title"], 60)
                    choice = ui.choose("Task is still running", [title],
                                       ["Keep it running in the background", "Stop it (you can continue it later)"],
                                       allow_text=False, color=ui.FRAME, esc="keep watching")
                    if choice and choice.startswith("Stop"):
                        stopping = True
                        try:
                            os.kill(proc.pid, signal.SIGTERM)
                        except OSError:
                            pass
                        spin.resume("Stopping")
                        continue
                    if choice is None:  # Esc: keep watching
                        spin.resume()
                        continue
                    self.background.append(task_id)
                    return
        finally:
            spin.end()
            keys.close()
            self.queue += spin.queued + ([spin.typed.strip()] if spin.typed.strip() else [])

    def _drain(self, path: str, pos: int, spin: ui.Spinner, phase: "Phase") -> int:
        if not os.path.exists(path):
            return pos
        with open(path, encoding="utf-8") as f:
            f.seek(pos)
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                msg = format_event(ev, self._last_where)
                if ev.get("event") in ("thinking", "step_start"):
                    self._last_where = ev.get("purpose") or ev.get("project")
                if msg:
                    spin.say(msg)
                if phase.update(ev):
                    spin.set(phase.verb, phase.who)
            return f.tell()

    def _announce_background(self) -> None:
        for tid in list(self.background):
            t = self.eng.store.load(tid)
            if t and t["status"] not in ("running", "queued", "planning") \
                    and not os.path.exists(self.eng.store.path(tid, "lock")):
                self.background.remove(tid)
                self.focus = tid
                text = ui.c("  ── background task finished ──", ui.GREY) + "\n" + self.render(t)
                ui.out("\n" + text + "\n")
                self.history.append("", text, self.turns, self.focus)
                self._offer_choices()

    def _offer_choices(self) -> None:
        """If the focused task waits on a decision, offer it as a menu (Esc = answer later in the chat)."""
        task = self.eng.store.load(self.focus) if self.focus else None
        open_q = [q for q in (task or {}).get("questions", []) if not q.get("answer")]
        if task and not open_q and ui.TTY:
            self._offer_next(task)
        if not open_q or not ui.TTY:
            return
        q = open_q[0]
        options = {"approval": ["Yes, go ahead", "No, cancel"], "budget": ["Yes, continue", "No, stop"]}.get(
            q["kind"], list(q.get("options") or []))
        lines = [l for l in q["text"].splitlines() if not l.startswith("Go ahead?")]
        title = "Plan needs your go-ahead" if q["kind"] == "approval" else "Needs your decision"
        choice = ui.choose(title, [ui.md(l) for l in lines], options)
        if choice is None:
            ui.out(ui.c("  Answer any time by typing it below.", ui.GREY))
            return
        if not choice:
            return
        answer = {"Yes, go ahead": "yes", "No, cancel": "no", "Yes, continue": "yes", "No, stop": "no"}.get(choice, choice)
        self._exchange(answer)

    def _offer_next(self, task: Dict[str, Any]) -> None:
        """After a lifecycle step, offer the recommended next one once (Esc or 'Not now' = keep chatting)."""
        nxt = task.get("next")
        if not nxt or task.get("next_started") or task["id"] in self._offered_next or task["id"] in self.background:
            return
        self._offered_next.add(task["id"])
        title = nxt.get("title") or nxt["mode"]
        choice = ui.choose("Next step", [f"{title}: {nxt.get('reason', '')}"], [f"Start {title}", "Not now"],
                           allow_text=False, color=ui.FRAME, esc="decide later")
        if choice and choice.startswith("Start"):
            self._exchange("next")
        else:
            ui.out(ui.c("  Say \"next\" whenever you want to continue.", ui.GREY))

    def _style(self, report: str) -> str:
        """Report text -> chat message: ◆ status line, bold section titles, coloured marks, wrapped text."""
        lines = report.rstrip().splitlines()
        if not lines:
            return ""
        head = lines[0]
        mark = head[:1]
        color = {"✔": ui.GREEN, "✘": ui.RED, "?": ui.YELLOW, "◐": ui.YELLOW, "!": ui.YELLOW}.get(mark, ui.ACCENT)
        body = head[1:].strip() if mark in "✔✘◐?…!" else head
        body = re.sub(r"^#\d+\s+", "", body)  # task numbers stay in the "Details: /show N" footer
        head_lines = ui.wrap_block(body, indent="  ").splitlines()
        out = [ui.c(ui.MARK, color) + " " + ui.c(head_lines[0], ui.BOLD)] + [ui.c(h, ui.BOLD) for h in head_lines[1:]]
        for line in lines[1:]:
            if line.startswith("Details:"):
                out.append(ui.c("  " + line, ui.GREY))
            elif line in SECTION_TITLES:
                out.append("  " + ui.c(line, ui.BOLD))
            elif not line.strip():
                out.append("")
            else:
                indent = "  " + line[: len(line) - len(line.lstrip())]
                row = re.match(r"^  (\S+)(\s{2,})(.*)$", line)  # "  Checks    ✔ …" key/value rows
                plain = line.strip()
                bullet_line = re.match(r"^\s*([-•*]|\d+\.)\s", line)
                cont = indent + (" " * (len(row.group(1)) + len(row.group(2))) if row else "  " if bullet_line else "")
                wrapped = ui.wrap_block(plain, indent=cont, first=indent) if len(indent + plain) > ui.width() else indent + plain
                out.append(ui.md(wrapped).replace("✔", ui.c("✔", ui.GREEN)).replace("✘", ui.c("✘", ui.RED)))
        return "\n".join(out)

    def _conversation(self) -> str:
        return "\n".join(f"User: {u}\nForeman: {a}" for u, a in self.turns[-6:])

PURPOSE = {"plan": ("Plan", "Planning"), "replan": ("Re-plan", "Re-planning"), "review": ("Review", "Reviewing"),
           "document": ("Write", "Writing")}


TOOL_VERBS = {"read": "Reading", "view": "Reading", "grep": "Searching for", "search": "Searching for",
              "glob": "Looking for files", "list": "Browsing", "ls": "Browsing", "edit": "Editing",
              "multiedit": "Editing", "patch": "Editing", "write": "Writing", "bash": "Running",
              "webfetch": "Reading", "websearch": "Searching the web for", "task": "Delegating",
              "agent": "Delegating", "todowrite": "Organising the work"}
QUESTION_RE = re.compile(r"\?\s*$|^\s*(what|how|where|why|which|who|when|is|are|does|do|can|could|should|explain|"
                         r"show me|tell me|list|find|check|review|compare)\b", re.I)


def intent_verb(task: Dict[str, Any]) -> str:
    """First spinner verb, from the request (before the planner has said anything)."""
    if any(s.get("status") in ("done", "failed", "blocked") for s in task.get("steps") or []):
        return "Picking up where it left off"
    return "Looking into it" if QUESTION_RE.search(task.get("request") or "") else "Planning the change"


class Phase:
    """Turns task events into the spinner's '<what is happening>' line and the active provider.
    Tool activity wins (Reading server.js, Running npm test); otherwise the phase (implementing, checks, review)."""

    def __init__(self, task: Dict[str, Any]):
        self.verb, self.who = intent_verb(task), ""
        self.running: Dict[str, Tuple[str, str]] = {}  # project -> (phase verb, provider)

    def update(self, ev: Dict[str, Any]) -> bool:
        """Apply one event; True when the line changed."""
        before = (self.verb, self.who)
        e, provider = ev.get("event"), ev.get("provider")
        if e == "thinking":
            purpose = ev.get("purpose")
            self.verb = {"replan": "Expanding the plan", "review": "Reviewing the changes",
                         "document": "Writing the document"}.get(purpose, self.verb)
            self.who = provider or self.who
        elif e == "impact":
            self.verb = "Checking the impact before editing"
        elif e == "plan":
            steps = (ev.get("plan") or {}).get("steps") or []
            projects = sorted({s.get("project") for s in steps if s.get("project")})
            self.verb = "Writing the answer" if not steps else \
                f"Starting work on {projects[0]}" if len(projects) == 1 else f"Starting work on {len(projects)} projects"
        elif e == "step_start":
            project = ev.get("project", "")
            verb = f"Investigating {project}" if ev.get("step_kind") == "investigate" else f"Implementing in {project}"
            self.running[project] = (verb, provider or "")
            self.verb, self.who = verb, provider or self.who
        elif e == "step_failed":  # another provider may retry the step; the next step_start says so
            self.verb = "Recovering from a failed attempt"
        elif e == "step_done":
            self.running.pop(ev.get("project", ""), None)
            if self.running:
                self.verb, self.who = next(iter(self.running.values()))
            else:
                self.verb = "Checking the results"
        elif e == "check":
            self.verb, self.who = "Running checks", ""
        elif e == "review":
            self.verb = "Writing the report"
        elif e == "activity":
            self.verb = self._activity(ev)
            self.who = provider or self.who
        elif e == "finished":
            self.verb = "Finishing"
        return (self.verb, self.who) != before

    def _activity(self, ev: Dict[str, Any]) -> str:
        text, where = ev.get("text", ""), ev.get("where") or ""
        in_project = where in self.running and len(self.running) > 1
        m = re.match(r"(\w+)\((.*)\)$", text)
        if not m:  # assistant text: the model is reasoning between tool calls
            return f"Thinking about {where}" if in_project else "Thinking"
        name, arg = m.group(1), m.group(2)
        action = TOOL_VERBS.get(name.lower(), f"Using {name}")
        if action.startswith(("Reading", "Editing", "Writing")) and "/" in arg and not arg.startswith("http"):
            arg = arg.rstrip("/").rsplit("/", 1)[-1]
        elif action == "Running":
            arg = arg.split("&&")[0].split("|")[0].strip()
        if action in ("Looking for files", "Browsing", "Organising the work", "Delegating"):
            arg = ""
        line = f"{action} {short(arg, 40)}".strip() if arg else action
        return line + (f" in {where}" if in_project else "")


def format_event(ev: Dict[str, Any], current: Optional[str] = None) -> Optional[str]:
    """Progress blocks: '◆ Name(arg) · provider' headers with '╰' lines under them. None = quiet."""
    e = ev.get("event")
    if e == "thinking":
        name = PURPOSE.get(ev.get("purpose"), ("Think", ""))[0]
        arg = {"review": "changes", "document": "document"}.get(ev.get("purpose"), "request")
        return ui.tool(name, arg, ev.get("provider", ""))
    if e == "activity":
        text = ev.get("text", "")
        prefix = f"[{ev['where']}] " if ev.get("where") and ev.get("where") != current else ""
        style = (ui.GREY, ui.ITAL) if text.startswith("· ") else (ui.GREY,)
        return ui.result(prefix + text, *style)
    if e == "plan":
        steps = (ev.get("plan") or {}).get("steps") or []
        if not steps:
            return ui.result("answered directly, no workers needed")
        return ui.result(f"{len(steps)} step{'s' if len(steps) != 1 else ''}: "
                         + "; ".join(f"{s.get('kind')} {s.get('project')}" for s in steps), ui.BLUE)
    if e == "mode":
        how = "your choice" if ev.get("source") == "user" else (ev.get("reason") or "recommended")
        return ui.tool("Mode", ev.get("title") or ev.get("mode", ""), how)
    if e == "impact":
        who = ", ".join(ev.get("consumers") or [])
        return ui.tool("Impact", f"{ev.get('risk', '?')} risk",
                       "checking " + (f"consumers {who}" if who else "what the change affects") + " before editing")
    if e == "document":
        return ui.result(f"saved {ev.get('project')}/{ev.get('path')}", ui.GREEN)
    if e == "replan":
        return ui.tool("Expand plan", "new dependency") + "\n" + ui.result("; ".join(ev.get("discoveries", [])))
    if e == "step_start":
        name = "Investigate" if ev.get("step_kind") == "investigate" else "Implement"
        return ui.tool(name, ev.get("project", ""), f"{ev.get('provider')}" + (f" · {ev['why']}" if ev.get("why") else ""))
    if e == "step_done":
        ok = ev.get("status") == "done"
        return ui.result(("✓ " if ok else "! ") + (ev.get("summary", "") or "done"), ui.GREEN if ok else ui.YELLOW)
    if e == "step_failed":
        return ui.result(f"{ev.get('provider')} failed: {ev.get('error') or ''}", ui.RED)
    if e == "check":
        ok = ev.get("ok")
        return ui.tool("Check", f"{ev.get('project')}: {ev.get('command')}", color=ui.GREEN if ok else ui.RED) + "\n" + \
            ui.result("passed" if ok else "failed", ui.GREEN if ok else ui.RED)
    if e == "review":
        r = ev.get("review") or {}
        return ui.result(f"{r.get('verdict')}: {r.get('summary', '')}", ui.GREEN if r.get("verdict") == "approve" else ui.YELLOW)
    if e == "thinking_failed":
        return ui.result(f"skipped {ev.get('provider')}: {ev.get('error', '')}", ui.YELLOW)
    if e == "plan_failed":
        return ui.result(f"planning failed: {ev.get('error', '')}", ui.RED)
    return None


def _seconds(task: Dict[str, Any]) -> int:
    from datetime import datetime
    try:
        return int((datetime.fromisoformat(task["updated_at"]) - datetime.fromisoformat(task["created_at"])).total_seconds())
    except (KeyError, ValueError):
        return 0


class _KeyReader:
    """Non-blocking keystrokes while a task runs (no echo; Ctrl+C still interrupts)."""

    def __init__(self):
        import sys
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        self.old = None
        if self.fd is not None:
            import termios
            self.old = termios.tcgetattr(self.fd)
            new = termios.tcgetattr(self.fd)
            new[3] &= ~(termios.ECHO | termios.ICANON)
            termios.tcsetattr(self.fd, termios.TCSADRAIN, new)

    def read(self, timeout: float) -> str:
        if self.fd is None:
            time.sleep(timeout)
            return ""
        import select
        r, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, 1024).decode("utf-8", "ignore") if r else ""

    def close(self):
        if self.old is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
            self.old = None


class ChatHistory:
    """Per-project conversation transcripts (~/.foreman/chats/<project>/<id>.json) for /resume."""

    def __init__(self, home: str, project_key):
        self.home = home
        self._key = project_key  # callable: resolved on first use, once the project index is loaded
        self.chat_id = ""
        self.start_new()

    @property
    def project(self) -> str:
        if callable(self._key):
            self._key = self._key()
        return self._key

    @property
    def dir(self) -> str:
        return os.path.join(self.home, "chats", re.sub(r"[^A-Za-z0-9._-]+", "-", self.project).strip("-") or "default")

    def start_new(self) -> None:
        self.chat_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
        self.data: Dict[str, Any] = {"id": self.chat_id, "title": "", "entries": [],
                                     "turns": [], "focus": None, "started_at": time.time(), "updated_at": time.time()}

    def append(self, user: str, output: str, turns, focus) -> None:
        from orchestrator.util import write_json
        self.data["project"] = self.project
        if user and not self.data["title"] and not user.startswith("/"):
            self.data["title"] = short(user, 80)
        self.data["entries"].append({"user": user, "output": output, "at": time.time()})
        self.data["entries"] = self.data["entries"][-200:]
        self.data.update(turns=[list(t) for t in turns][-12:], focus=focus, updated_at=time.time())
        write_json(os.path.join(self.dir, self.chat_id + ".json"), self.data)

    def load(self, chat_id: str) -> Optional[Dict[str, Any]]:
        from orchestrator.util import read_json
        data = read_json(os.path.join(self.dir, chat_id + ".json"))
        if data:
            self.chat_id, self.data = chat_id, data  # continue appending to the resumed conversation
        return data

    def list(self) -> List[Dict[str, Any]]:
        from orchestrator.util import read_json
        if not os.path.isdir(self.dir):
            return []
        items = [read_json(os.path.join(self.dir, f)) for f in os.listdir(self.dir) if f.endswith(".json")]
        return sorted([i for i in items if i], key=lambda i: i.get("updated_at", 0), reverse=True)


def _ago(ts: float) -> str:
    secs = max(0, int(time.time() - ts))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit} ago"
    return "just now"
