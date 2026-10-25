"""Command-line interface (thin). `orchestrator "<request>"` is the main entry; subcommands cover state and setup."""

import argparse
import json
import os
import signal
import sys
from typing import List, Optional

from orchestrator.app import build_engine
from orchestrator.config import load_config, set_option

SUBCOMMANDS = {"chat", "ask", "status", "resume", "answer", "tasks", "show", "projects", "sessions", "suggestions",
               "accept", "dismiss", "config", "doctor", "mcp", "run-task", "help", "-h", "--help"}


def _progress(msg: str) -> None:
    print(f"\033[2m{msg}\033[0m" if sys.stderr.isatty() else msg, file=sys.stderr, flush=True)


def _engine(args):
    visual = None
    if getattr(args, "visual", False):
        visual = True
    if getattr(args, "headless", False):
        visual = False
    return build_engine(visual=visual, out=_progress)


def _stop_task(signum, frame) -> None:
    """Stop request for a running task: kill worker processes, then unwind so the task is marked interrupted."""
    from orchestrator.engine import Interrupted
    from orchestrator.util import kill_all_children
    kill_all_children()
    raise Interrupted()


def _print_result(eng, result) -> None:
    print(result["report"])
    task = result.get("task")
    if task and task.get("suggestions") and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            choice = input("\nAct on a suggestion now? [id / Enter to skip] ").strip()
        except EOFError:
            choice = ""
        if choice:
            s = eng.store.set_suggestion(choice, "accepted")
            if s:
                proj = eng.registry.projects.get(s["project"])
                _print_result(eng, eng.execute(eng.prepare_new(s["text"], proj.path if proj else task["cwd"])))


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    chat_flags = {"--visual", "--headless", "-c", "--continue", "-r", "--resume"}
    if not argv or all(a in chat_flags for a in argv):
        if not sys.stdin.isatty():
            print(HELP)
            return 0
        argv = ["chat"] + argv
    if argv[0] not in SUBCOMMANDS:
        argv = ["ask"] + argv

    ap = argparse.ArgumentParser(prog="orchestrator", description="Foreman: orchestrate AI sessions across projects")
    sub = ap.add_subparsers(dest="cmd")

    def add(name, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--visual", action="store_true", help="show workers in terminal windows for this run")
        p.add_argument("--headless", action="store_true", help="run workers under the hood for this run")
        return p

    p = add("chat", "interactive chat (default when run without arguments)")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the latest chat here")
    p.add_argument("-r", "--resume", action="store_true", help="pick an earlier chat in this project")
    p = add("ask", "submit a request (default when no subcommand is given)")
    p.add_argument("request", nargs="+")
    p.add_argument("--cwd", default=os.getcwd())
    p = add("status", "state of a task (no AI)")
    p.add_argument("task", nargs="*")
    p.add_argument("--cwd", default=os.getcwd())
    p = add("resume", "continue a task")
    p.add_argument("task", nargs="?")
    p.add_argument("note", nargs="*")
    p.add_argument("--cwd", default=os.getcwd())
    p = add("answer", "answer a task's open question")
    p.add_argument("task")
    p.add_argument("text", nargs="+")
    p = sub.add_parser("tasks", help="list tasks")
    p.add_argument("-n", type=int, default=15)
    p = sub.add_parser("show", help="task details")
    p.add_argument("task")
    p.add_argument("--json", action="store_true")
    p.add_argument("--events", action="store_true")
    p = sub.add_parser("projects", help="project registry")
    p.add_argument("action", nargs="?", default="list", choices=["list", "scan", "show", "add", "link", "unlink", "test"])
    p.add_argument("args", nargs="*")
    p = sub.add_parser("sessions", help="existing AI sessions for a project")
    p.add_argument("project", nargs="?")
    sub.add_parser("suggestions", help="open suggestions")
    p = add("accept", "act on a suggestion")
    p.add_argument("id")
    p = sub.add_parser("dismiss", help="dismiss a suggestion")
    p.add_argument("id")
    p = sub.add_parser("config", help="show or change settings")
    p.add_argument("key", nargs="?", choices=["display", "notify", "reuse", "planner", "first-worker"])
    p.add_argument("value", nargs="?")
    sub.add_parser("doctor", help="check providers, terminal, MCP registration")
    p = sub.add_parser("mcp", help="run the MCP server (stdio), or `mcp install [claude|antigravity|codex|all]`")
    p.add_argument("action", nargs="?", default="serve", choices=["serve", "install", "status"])
    p.add_argument("targets", nargs="*")
    p = add("run-task", argparse.SUPPRESS)
    p.add_argument("task")
    sub.add_parser("help")

    args = ap.parse_args(argv)
    cmd = args.cmd

    if cmd in (None, "help"):
        print(HELP)
        return 0

    if cmd == "mcp":
        if args.action == "install":
            from orchestrator.integrations import install
            for target, res in install(args.targets or ["claude", "antigravity", "codex"]).items():
                print(f"{target:<12} {res}")
            return 0
        if args.action == "status":
            from orchestrator.integrations import status
            for target, res in status().items():
                print(f"{target:<12} {res}")
            return 0
        from orchestrator.mcp_server import MCPServer
        MCPServer().serve()
        return 0

    if cmd == "config":
        cfg = load_config()
        if not args.key:
            print(f"config file: {os.path.join(cfg.home, 'config.yaml')}")
            print(f"planner: {cfg.brain}   workers: {', '.join(cfg.provider_order)}")
            print(f"display: {cfg.display}   notify: {cfg.notify}   session reuse: {cfg.reuse_sessions}")
            print(f"roots: {', '.join(cfg.roots)}")
            return 0
        value = args.value or ""
        if args.key == "display" and value in ("terminal", "headless"):
            set_option(cfg.home, "ui.display", value)
        elif args.key == "notify" and value in ("on", "off"):
            set_option(cfg.home, "ui.notify", value == "on")
        elif args.key == "reuse" and value in ("fork", "off"):
            set_option(cfg.home, "sessions.reuse", value)
        elif args.key in ("planner", "first-worker") and {"agy": "antigravity"}.get(value, value) in cfg.providers:
            value = {"agy": "antigravity"}.get(value, value)
            if args.key == "planner":
                set_option(cfg.home, "brain", value)
            else:
                from orchestrator.config import set_first_provider
                set_first_provider(cfg.home, value)
        else:
            print("usage: orchestrator config display terminal|headless | notify on|off | reuse fork|off | "
                  "planner <provider> | first-worker <provider>")
            return 1
        print(f"{args.key} = {value}")
        return 0

    if cmd == "doctor":
        return doctor()

    eng = _engine(args)

    if cmd == "chat":
        from orchestrator.chat import Chat
        visual = True if args.visual else (False if args.headless else None)
        Chat(args.cwd, visual=visual, engine=eng).run(resume="last" if args.cont else "pick" if args.resume else None)
        return 0
    if cmd == "ask":
        _print_result(eng, eng.handle(" ".join(args.request), args.cwd))
        return 0
    if cmd == "status":
        print(eng.status(" ".join(args.task) or None, args.cwd)["report"])
        return 0
    if cmd == "resume":
        _print_result(eng, eng.resume(args.task, " ".join(args.note), args.cwd))
        return 0
    if cmd == "answer":
        _print_result(eng, eng.answer(args.task, " ".join(args.text)))
        return 0
    if cmd == "run-task":
        task = eng.store.load(args.task)
        if not task:
            print(f"no task {args.task}", file=sys.stderr)
            return 1
        signal.signal(signal.SIGTERM, _stop_task)
        signal.signal(signal.SIGINT, _stop_task)
        eng.execute(task)
        return 0
    if cmd == "tasks":
        for t in eng.store.all()[: args.n]:
            print(f"#{t['id']:<4} {t['status']:<12} {t['title']}  ({t['updated_at'][:16]})")
        return 0
    if cmd == "show":
        cands = eng.store.resolve(args.task, fallback=False)
        if not cands:
            print("not found")
            return 1
        t = cands[0]
        if args.json:
            print(json.dumps(t, indent=2, ensure_ascii=False))
        elif args.events:
            for e in eng.store.events(t["id"]):
                print(json.dumps(e, ensure_ascii=False)[:400])
        else:
            print(_show(t, eng))
        return 0
    if cmd == "projects":
        return projects_cmd(eng, args.action, args.args)
    if cmd == "sessions":
        cwd = os.getcwd()
        if args.project:
            p = eng.registry.find(args.project)
            cwd = p.path if p else os.path.abspath(args.project)
        for prov in eng.providers.values():
            for s in prov.discover_sessions(cwd)[:8]:
                from datetime import datetime
                print(f"{prov.name:<12} {s.session_id}  {datetime.fromtimestamp(s.updated_at):%Y-%m-%d %H:%M}  {s.title[:70]}")
        return 0
    if cmd == "suggestions":
        items = [s for s in eng.store.suggestions() if s["status"] == "open"]
        for s in items:
            print(f"[{s['id']}] ({s['project'] or 'general'}) {s['text']} — {s['why']}")
        if not items:
            print("No open suggestions.")
        return 0
    if cmd == "accept":
        s = eng.store.set_suggestion(args.id, "accepted")
        if not s:
            print("not found")
            return 1
        proj = eng.registry.projects.get(s["project"])
        _print_result(eng, eng.execute(eng.prepare_new(s["text"], proj.path if proj else os.getcwd())))
        return 0
    if cmd == "dismiss":
        print("dismissed" if eng.store.set_suggestion(args.id, "dismissed") else "not found")
        return 0
    return 1


def _show(t, eng) -> str:
    from orchestrator.report import render_report
    lines = [render_report(t, eng.store.suggestions()), "", "Steps:"]
    for s in t["steps"]:
        lines.append(f"  [{s['status']}] {s['id']} {s['kind']} {s['project']} via {s.get('provider') or '-'}: {s.get('summary') or ''}")
        lines.append(f"      instructions: {s['instructions'][:300]}")
    if t["decisions"]:
        lines.append("Decisions:")
        lines += [f"  - ({d['by']}) {d['text']}" for d in t["decisions"]]
    if t["evidence"]:
        lines.append("Evidence:")
        lines += [f"  - {e['kind']} {e.get('project', '')}: {e['summary']}" + (f"  [{e['log']}]" if e.get("log") else "")
                  for e in t["evidence"]]
    if t["sessions"]:
        lines.append("Sessions:")
        lines += [f"  - {s['provider']} {s['session_id']} ({s['project']}, step {s['step']})"
                  + (f" forked from {s['forked_from']}" if s.get("forked_from") else "") for s in t["sessions"]]
    lines.append(f"Logs: {eng.store.path(t['id'], 'logs')}")
    return "\n".join(lines)


def projects_cmd(eng, action: str, rest: List[str]) -> int:
    reg = eng.registry
    if action == "scan":
        names = reg.scan()
        print(f"{len(names)} projects, {len(reg.relations)} relations")
        return 0
    reg.ensure_scanned()
    if action == "list":
        for p in sorted(reg.projects.values(), key=lambda p: p.name):
            rels = len(reg.relations_of(p.name))
            print(f"{p.name:<32} {','.join(p.stack) or '-':<14} rel:{rels:<3} {p.path}")
        return 0
    if action == "show" and rest:
        p = reg.find(rest[0])
        if not p:
            print("not found")
            return 1
        print(reg._card(p))
        for r in reg.relations_of(p.name):
            print("   " + reg._rel_line(r, p.name))
        return 0
    if action == "add" and rest:
        p = reg.add_project(rest[0], rest[1] if len(rest) > 1 else None)
        print(f"added {p.name}: {p.path}")
        return 0
    if action == "link" and len(rest) >= 3:
        a, b = reg.find(rest[0]), reg.find(rest[1])
        if not a or not b:
            print("unknown project")
            return 1
        r = reg.link(a.name, b.name, rest[2], " ".join(rest[3:]))
        print(f"linked {r.src} --{r.type}--> {r.dst}")
        return 0
    if action == "unlink" and len(rest) >= 2:
        print(f"removed {reg.unlink(rest[0], rest[1])} relation(s)")
        return 0
    if action == "test" and len(rest) >= 2:
        p = reg.find(rest[0])
        if not p:
            print("unknown project")
            return 1
        p.test, p.source = " ".join(rest[1:]), "declared"
        reg.save()
        print(f"{p.name} test command: {p.test}")
        return 0
    print("usage: orchestrator projects [list|scan|show NAME|add PATH [NAME]|link A B TYPE [note]|unlink A B|test NAME CMD]")
    return 1


def doctor() -> int:
    from orchestrator.app import build_engine as be
    from orchestrator.integrations import status as mcp_status
    from orchestrator.terminal import terminal_available
    eng = be(out=lambda _m: None)
    cfg = eng.cfg
    print(f"Foreman home: {cfg.home}")
    print("Providers (preference order):")
    for name, prov in eng.providers.items():
        ok, info = prov.available()
        caps = [k for k, v in vars(prov.capabilities).items() if v]
        print(f"  {'✔' if ok else '✘'} {name:<12} {info}" + (f"   [{', '.join(caps)}]" if ok else ""))
        if ok:
            print(f"      runs today: {eng.budget.used(name)}/{cfg.providers[name].max_runs_per_day}")
    print(f"Planner (brain): {cfg.brain}")
    term = terminal_available()
    print(f"Display: {cfg.display}" + (f" (terminal: {term})" if cfg.display == "terminal" and term else
                                       " (no GUI terminal found - falls back to headless)" if cfg.display == "terminal" else ""))
    print(f"Notifications: {'on' if cfg.notify else 'off'} (one per finished task)")
    eng.registry.ensure_scanned()
    print(f"Projects: {len(eng.registry.projects)} under {', '.join(cfg.roots)}; relations: {len(eng.registry.relations)}")
    print("MCP registration:")
    for tool, st in mcp_status().items():
        print(f"  {tool:<12} {st}")
    return 0


HELP = """\
Foreman orchestrator

  orchestrator                        open the chat (plain language for everything)
  orchestrator -c | -r                continue the latest chat here | pick an earlier one
  orchestrator "<request>"            do something (plans, picks projects/sessions, executes, validates, reports)
  orchestrator status [task]          state of a task (instant, no AI)
  orchestrator resume [task] [note]   continue a task, re-verifying against current code
  orchestrator answer <task> <text>   answer a task's open question
  orchestrator tasks | show <task>    history and details (show --events for the audit log)
  orchestrator suggestions | accept <id> | dismiss <id>
  orchestrator projects [list|scan|show|add|link|unlink|test]
  orchestrator sessions [project]     existing Claude/OpenCode/Antigravity sessions for a project
  orchestrator config [display terminal|headless | notify on|off | reuse fork|off]
  orchestrator doctor                 providers, display, MCP registration
  orchestrator mcp [install [claude|antigravity|codex]]   MCP server for Claude Code, Antigravity, Codex…

  --visual / --headless               override the display mode for one run
"""


if __name__ == "__main__":
    sys.exit(main())
