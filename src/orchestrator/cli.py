"""Command-line interface (thin). `foreman "<request>"` is the main entry; subcommands cover state and setup."""

import argparse
import json
import os
import signal
import sys
from typing import List, Optional

from orchestrator import completion
from orchestrator.app import build_engine
from orchestrator.completion import SHELLS
from orchestrator.config import load_config, set_option
from orchestrator.report import task_summary

SUBCOMMANDS = {"chat", "ask", "status", "resume", "answer", "tasks", "show", "projects", "sessions", "suggestions",
               "accept", "dismiss", "config", "doctor", "mcp", "run-task", "help", "-h", "--help", "next", "lifecycle", "modes", "completion", "version"}


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


def _dump(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _print_result(eng, result, as_json: bool = False) -> None:
    task = result.get("task")
    if as_json:
        _dump({"task": task_summary(task) if task else None, "report": result["report"]})
        return
    print(result["report"])
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


def version_string() -> str:
    """`foreman 0.2.0`, plus the commit when running from a git checkout (`foreman 0.2.0+g1a2b3c4`)."""
    import subprocess
    from orchestrator import __version__
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if os.path.exists(os.path.join(root, ".git")):
        try:
            sha = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                 timeout=5).stdout.strip()
            if sha:
                return f"foreman {__version__}+g{sha}"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return f"foreman {__version__}"


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V", "version"):
        print(version_string())
        return 0
    chat_flags = {"--visual", "--headless", "-c", "--continue", "-r", "--resume"}
    if not argv or all(a in chat_flags for a in argv):
        if not sys.stdin.isatty():
            print(HELP)
            return 0
        argv = ["chat"] + argv
    flags = [a for a in argv[:2] if a in ("--visual", "--headless")]
    if flags and len(argv) > len(flags) and argv[len(flags)] in SUBCOMMANDS:  # `--headless answer 4 ...`
        argv = [argv[len(flags)]] + flags + argv[len(flags) + 1:]
    if argv[0] not in SUBCOMMANDS:
        argv = ["ask"] + argv

    ap = argparse.ArgumentParser(prog="foreman", description="Foreman: orchestrate AI sessions across projects")
    sub = ap.add_subparsers(dest="cmd")

    def add(name, help_, json_=True):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--visual", action="store_true", help="show workers in terminal windows for this run")
        p.add_argument("--headless", action="store_true", help="run workers under the hood for this run")
        if json_:
            p.add_argument("--json", action="store_true", help="print the result as JSON")
        return p

    p = add("chat", "interactive chat (default when run without arguments)", json_=False)
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the latest chat here")
    p.add_argument("-r", "--resume", action="store_true", help="pick an earlier chat in this project")
    p = add("ask", "submit a request (default when no subcommand is given)")
    p.add_argument("request", nargs="+")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--mode", help="lifecycle mode: research, planning, development, qa, deployment (default: recommended)")
    p.add_argument("--dry-run", action="store_true", help="plan only: show what would be done, change nothing")
    p = add("next", "start the recommended next lifecycle step for the latest work here")
    p.add_argument("--cwd", default=os.getcwd())
    p = sub.add_parser("lifecycle", help="where this project's features stand and what's next (no AI)")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--json", action="store_true")
    sub.add_parser("modes", help="list lifecycle modes and where their templates come from")
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
    p.add_argument("--json", action="store_true")
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
    p.add_argument("key", nargs="?", choices=["display", "notify", "reuse", "planner", "first-worker", "copy-on-select"])
    p.add_argument("value", nargs="?")
    p = sub.add_parser("doctor", help="check providers, terminal, MCP registration")
    p.add_argument("--json", action="store_true", help="print the findings as JSON")
    p.add_argument("--fix", action="store_true", help="register the MCP server where it is missing and rescan projects")
    sub.add_parser("version", help="print the version")
    p = sub.add_parser("completion", help="print a shell completion script (bash or zsh)")
    p.add_argument("shell", choices=SHELLS)
    p = sub.add_parser("mcp", help="run the MCP server (stdio), or `mcp install [claude|antigravity|codex|all]`")
    p.add_argument("action", nargs="?", default="serve", choices=["serve", "install", "status"])
    p.add_argument("targets", nargs="*")
    p = add("run-task", argparse.SUPPRESS, json_=False)
    p.add_argument("task")
    sub.add_parser("help")

    args = ap.parse_args(argv)
    cmd = args.cmd

    if cmd in (None, "help"):
        print(HELP)
        return 0

    if cmd == "completion":
        print(completion.script(args.shell), end="")
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
            print(f"display: {cfg.display}   notify: {cfg.notify}   session reuse: {cfg.reuse_sessions}   "
                  f"copy on select: {'on' if cfg.copy_on_select else 'off'}")
            print(f"roots: {', '.join(cfg.roots)}")
            return 0
        value = args.value or ""
        if args.key == "display" and value in ("terminal", "headless"):
            set_option(cfg.home, "ui.display", value)
        elif args.key == "notify" and value in ("on", "off"):
            set_option(cfg.home, "ui.notify", value == "on")
        elif args.key == "copy-on-select" and value in ("on", "off"):
            set_option(cfg.home, "ui.copy_on_select", value == "on")
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
            print("usage: foreman config display terminal|headless | notify on|off | reuse fork|off | "
                  "planner <provider> | first-worker <provider> | copy-on-select on|off")
            return 1
        print(f"{args.key} = {value}")
        return 0

    if cmd == "doctor":
        return doctor(as_json=args.json, fix=args.fix)

    eng = _engine(args)

    if cmd == "chat":
        from orchestrator.chat import Chat
        visual = True if args.visual else (False if args.headless else None)
        Chat(args.cwd, visual=visual, engine=eng).run(resume="last" if args.cont else "pick" if args.resume else None)
        return 0
    if cmd == "ask":
        from orchestrator.modes import resolve
        if args.mode and not resolve(args.mode, eng.modes(args.cwd)):
            print(f"unknown mode '{args.mode}'. Modes: " + ", ".join(eng.modes(args.cwd)), file=sys.stderr)
            return 1
        _print_result(eng, eng.handle(" ".join(args.request), args.cwd, mode=args.mode, dry_run=args.dry_run), args.json)
        return 0
    if cmd == "next":
        task, msg = eng.prepare_next(args.cwd)
        _print_result(eng, eng.execute(task) if task else {"task": None, "report": msg}, args.json)
        return 0
    if cmd == "lifecycle":
        if args.json:
            _dump(eng.lifecycle_data(args.cwd))
        else:
            print(eng.lifecycle_text(args.cwd))
        return 0
    if cmd == "modes":
        for name, m in eng.modes(os.getcwd()).items():
            print(f"{m.title:<12} {name:<12} [{m.source}] {m.summary}")
        print("\nOverride a template by copying it to ~/.foreman/modes/<name>.md or <project>/.foreman/modes/<name>.md")
        return 0
    if cmd == "status":
        res = eng.status(" ".join(args.task) or None, args.cwd)
        if args.json:
            _print_result(eng, res, as_json=True)
        else:
            print(res["report"])
        return 0
    if cmd == "resume":
        _print_result(eng, eng.resume(args.task, " ".join(args.note), args.cwd), args.json)
        return 0
    if cmd == "answer":
        _print_result(eng, eng.answer(args.task, " ".join(args.text)), args.json)
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
        if args.json:
            _dump([task_summary(t) for t in eng.store.all()[: args.n]])
            return 0
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
        _print_result(eng, eng.execute(eng.prepare_new(s["text"], proj.path if proj else os.getcwd())), args.json)
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
    print("usage: foreman projects [list|scan|show NAME|add PATH [NAME]|link A B TYPE [note]|unlink A B|test NAME CMD]")
    return 1


def doctor_data(eng) -> dict:
    """What `doctor` reports, as data: providers, planner, display, projects, MCP registration."""
    from orchestrator.integrations import status as mcp_status
    from orchestrator.terminal import terminal_available
    cfg = eng.cfg
    providers = {}
    for name, prov in eng.providers.items():
        ok, info = prov.available()
        providers[name] = {"available": ok, "info": info,
                           "capabilities": [k for k, v in vars(prov.capabilities).items() if v] if ok else [],
                           "runs_today": eng.budget.used(name) if ok else 0,
                           "max_runs_per_day": cfg.providers[name].max_runs_per_day}
    eng.registry.ensure_scanned()
    return {"version": version_string(), "home": cfg.home, "providers": providers, "planner": cfg.brain,
            "display": cfg.display, "terminal": terminal_available(), "notifications": cfg.notify,
            "roots": cfg.roots, "projects": len(eng.registry.projects), "relations": len(eng.registry.relations),
            "mcp": mcp_status()}


def doctor_fix(eng, data: dict) -> list:
    """Repairs that are safe and reversible: MCP registration where the tool is installed but Foreman is not
    registered, and a project rescan when none is known. Returns what was done."""
    from orchestrator.integrations import install
    actions = []
    missing = [t for t, st in data["mcp"].items() if st == "not registered"]
    if missing:
        for target, res in install(missing).items():
            actions.append(f"mcp install {target}: {res}")
    if not data["projects"]:
        names = eng.registry.scan()
        actions.append(f"scanned {', '.join(eng.cfg.roots)}: {len(names)} project(s) found")
    if not any(p["available"] for p in data["providers"].values()):
        actions.append("no AI CLI found: install claude, agy or opencode (or add a command under providers: in config.yaml)")
    return actions


def doctor(as_json: bool = False, fix: bool = False) -> int:
    from orchestrator.app import build_engine as be
    eng = be(out=lambda _m: None)
    data = doctor_data(eng)
    actions = doctor_fix(eng, data) if fix else []
    if actions:
        data = {**doctor_data(eng), "fixed": actions}
    if as_json:
        _dump(data)
        return 0
    print(f"Foreman home: {data['home']}")
    print("Providers (preference order):")
    for name, p in data["providers"].items():
        print(f"  {'✔' if p['available'] else '✘'} {name:<12} {p['info']}"
              + (f"   [{', '.join(p['capabilities'])}]" if p["available"] else ""))
        if p["available"]:
            print(f"      runs today: {p['runs_today']}/{p['max_runs_per_day']}")
    print(f"Planner (brain): {data['planner']}")
    term = data["terminal"]
    print(f"Display: {data['display']}" + (f" (terminal: {term})" if data["display"] == "terminal" and term else
                                          " (no GUI terminal found - falls back to headless)" if data["display"] == "terminal" else ""))
    print(f"Notifications: {'on' if data['notifications'] else 'off'} (one per finished task)")
    print(f"Projects: {data['projects']} under {', '.join(data['roots'])}; relations: {data['relations']}")
    print("MCP registration:")
    for tool, st in data["mcp"].items():
        print(f"  {tool:<12} {st}")
    if actions:
        print("Fixed:")
        print("\n".join(f"  - {a}" for a in actions))
    elif not fix and any(st == "not registered" for st in data["mcp"].values()):
        print("Tip: `foreman doctor --fix` registers Foreman with the tools above.")
    return 0


HELP = """\
Foreman

  foreman                          open the chat (plain language for everything)
  foreman -c | -r                  continue the latest chat here | pick an earlier one
  foreman "<request>"              do something (plans, picks projects/sessions, executes, validates, reports)
  foreman --mode qa "<request>"    same, in a chosen mode (research, planning, development, qa, deployment)
  foreman --dry-run "<request>"    plan only: what would be done, nothing runs; `foreman resume` then runs that plan
  foreman lifecycle | next         where this project's features stand | start the recommended next step
  foreman modes                    the mode templates and where each comes from
  foreman status [task]            state of a task (instant, no AI)
  foreman resume [task] [note]     continue a task, re-verifying against current code
  foreman answer <task> <text>     answer a task's open question
  foreman tasks | show <task>      history and details (show --events for the audit log)
  foreman suggestions | accept <id> | dismiss <id>
  foreman projects [list|scan|show|add|link|unlink|test]
  foreman sessions [project]       existing Claude/OpenCode/Antigravity sessions for a project
  foreman config [display terminal|headless | notify on|off | reuse fork|off]
  foreman doctor [--fix]           providers, display, MCP registration (--fix registers MCP where missing)
  --json                           machine-readable output on ask, status, tasks, show, lifecycle, next, resume, answer, doctor
  foreman completion bash|zsh      shell completion script (eval "$(foreman completion bash)")
  foreman --version                version (with the commit when run from a checkout)
  foreman mcp [install [claude|antigravity|codex]]   MCP server for Claude Code, Antigravity, Codex…

  --visual / --headless               override the display mode for one run
"""


if __name__ == "__main__":
    sys.exit(main())
