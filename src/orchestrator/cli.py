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

SUBCOMMANDS = {"chat", "ask", "status", "resume", "answer", "tasks", "show", "projects", "sessions", "suggestions",
               "accept", "dismiss", "close", "config", "doctor", "mcp", "serve", "run-task", "help", "-h", "--help", "next", "lifecycle", "modes", "models", "completion", "version", "stats",
               "stop", "output", "conversations", "open"}


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
    """Stop request for a running task, the same as a stop file (Engine.request_stop): kill the worker processes and
    let the run end as interrupted at its next checkpoint (raising here could cut a record or event write in half)."""
    from orchestrator.util import kill_all_children
    kill_all_children()


def _api(eng):
    """The application API over this engine: --json output uses its data shapes (docs/api-contract.md)."""
    from orchestrator.api import Api
    return Api(eng, actor=eng.actor)


def _dump(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _accept(eng, suggestion, cwd: str, as_json: bool = False) -> None:
    task, msg = eng.accept_suggestion(suggestion, cwd)
    _print_result(eng, eng.execute(task) if task else {"task": None, "report": msg}, as_json)


def _print_result(eng, result, as_json: bool = False) -> None:
    task = result.get("task")
    if as_json:
        _dump({"task": _api(eng).summary(task) if task else None, "report": result["report"]})
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
                _accept(eng, s, task["cwd"])


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
    p = add("close", "mark a task finished by hand (or --cancel it); nothing in the project changes")
    p.add_argument("task")
    p.add_argument("note", nargs="*")
    p.add_argument("--cancel", action="store_true", help="not wanted any more (instead of: finished another way)")
    p = sub.add_parser("models", help="choose each provider's model and effort (interactive), or set one: models PROVIDER MODEL [EFFORT]")
    p.add_argument("args", nargs="*", help="PROVIDER MODEL [EFFORT] (MODEL `default` lets the CLI decide)")
    p.add_argument("--json", action="store_true", help="print the current choices as JSON")
    p = sub.add_parser("stop", help="stop a running task (it ends as interrupted at its next checkpoint)")
    p.add_argument("task")
    p = sub.add_parser("output", help="raw output of a task's worker runs (redacted when written)")
    p.add_argument("task")
    p.add_argument("name", nargs="?", help="the run (from the list); without it, the runs are listed")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("conversations", help="conversations in every project (the chat's and the app's), or one's messages")
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("open", help="open a project folder (listed by the app and the chat from now on) and start the chat there")
    p.add_argument("path", nargs="?", default=os.getcwd())
    p.add_argument("--no-chat", action="store_true", help="only open it")
    p = sub.add_parser("tasks", help="list tasks")
    p.add_argument("-n", type=int, default=15)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("stats", help="totals across tasks: outcomes, model calls, retries, time, cost (no AI)")
    p.add_argument("--since", help="only tasks created on or after this date (YYYY-MM-DD)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="task details")
    p.add_argument("task")
    p.add_argument("--json", action="store_true")
    p.add_argument("--events", action="store_true")
    p = sub.add_parser("projects", help="project registry")
    p.add_argument("action", nargs="?", default="list", choices=["list", "scan", "show", "add", "link", "unlink", "test", "hide"])
    p.add_argument("args", nargs="*")
    p = sub.add_parser("sessions", help="existing AI sessions for a project")
    p.add_argument("project", nargs="?")
    sub.add_parser("suggestions", help="open suggestions")
    p = add("accept", "act on a suggestion")
    p.add_argument("id")
    p = sub.add_parser("dismiss", help="dismiss a suggestion")
    p.add_argument("id")
    p = sub.add_parser("config", help="show or change settings (the same list as the desktop app's Settings)")
    p.add_argument("key", nargs="?", help="a setting (foreman config --list shows them)")
    p.add_argument("value", nargs="?")
    p.add_argument("--list", action="store_true", help="the settings, their values and options")
    p = sub.add_parser("doctor", help="check providers, terminal, MCP registration")
    p.add_argument("--json", action="store_true", help="print the findings as JSON")
    p.add_argument("--fix", action="store_true", help="register the MCP server where it is missing and rescan projects")
    sub.add_parser("version", help="print the version")
    p = sub.add_parser("completion", help="print a shell completion script (bash or zsh)")
    p.add_argument("shell", choices=SHELLS)
    p = sub.add_parser("mcp", help="run the MCP server (stdio), or `mcp install [claude|antigravity|codex|all]`")
    p.add_argument("action", nargs="?", default="serve", choices=["serve", "install", "status"])
    p.add_argument("targets", nargs="*")
    p = sub.add_parser("serve", help="the application API for the desktop app (JSON-RPC over stdio)")
    p.add_argument("--stdio", action="store_true", help="speak JSON-RPC on stdin/stdout (the only transport so far)")
    p = add("run-task", argparse.SUPPRESS, json_=False)
    p.add_argument("task")
    p.add_argument("--run", help=argparse.SUPPRESS)  # the token of the run this process was started for
    p.add_argument("--plan-only", action="store_true", help=argparse.SUPPRESS)  # a dry run
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
        return config_cmd(args)

    if cmd == "doctor":
        return doctor(as_json=args.json, fix=args.fix)

    if cmd == "serve":
        if not args.stdio:
            print("usage: foreman serve --stdio   (JSON-RPC 2.0 on stdin/stdout; see docs/api-contract.md)", file=sys.stderr)
            return 1
        from orchestrator.rpc_server import serve_stdio
        serve_stdio()
        return 0

    eng = _engine(args)

    if cmd in ("stop", "output", "conversations", "open"):
        from orchestrator.api import ApiError
        try:
            return {"stop": stop_cmd, "output": output_cmd, "conversations": conversations_cmd, "open": open_cmd}[cmd](eng, args)
        except ApiError as e:
            print(str(e), file=sys.stderr)
            return 1
    if cmd == "chat":
        from orchestrator.chat import Chat
        eng.actor = "chat"
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
    if cmd == "models":
        return _models(eng, args)
    if cmd == "next":
        task, msg = eng.prepare_next(args.cwd)
        _print_result(eng, eng.execute(task) if task else {"task": None, "report": msg}, args.json)
        return 0
    if cmd == "lifecycle":
        if args.json:
            _dump(_api(eng).lifecycle_get(args.cwd))
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
    if cmd == "close":
        _print_result(eng, eng.close(args.task, " ".join(args.note), cancel=args.cancel), args.json)
        return 0
    if cmd == "run-task":
        task = eng.store.load(args.task)
        if not task:
            print(f"no task {args.task}", file=sys.stderr)
            return 1
        from orchestrator.oscompat import join_kill_on_close_job
        join_kill_on_close_job()  # Windows: every worker this runner starts ends with it
        signal.signal(signal.SIGTERM, _stop_task)
        signal.signal(signal.SIGINT, _stop_task)
        if args.run:
            task["run_request"] = args.run
        eng.plan_only(task) if args.plan_only else eng.execute(task)
        return 0
    if cmd == "tasks":
        broken = eng.store.unreadable()
        if args.json:
            _dump(_api(eng).tasks_list(limit=args.n)["tasks"]
                  + [{"id": i, "status": "unreadable", "record": eng.store.path(i, "task.json")} for i in broken])
            return 0
        for t in eng.store.all()[: args.n]:
            print(f"#{t['id']:<4} {t['status']:<12} {t['title']}  ({t['updated_at'][:16]})")
        for i in broken:
            print(f"#{i:<4} {'unreadable':<12} its record can't be read: {eng.store.path(i, 'task.json')}")
        return 0
    if cmd == "stats":
        from orchestrator.metrics import render_stats, stats
        s = stats(eng.store.all(), args.since)
        if args.json:
            _dump(s)
        else:
            print(render_stats(s))
        return 0
    if cmd == "show":
        cands = eng.store.resolve(args.task, fallback=False)
        if not cands:
            print("not found")
            return 1
        t = cands[0]
        if args.json:
            _dump(_api(eng).tasks_get(t["id"])["record"])
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
        _accept(eng, s, os.getcwd(), args.json)
        return 0
    if cmd == "dismiss":
        print("dismissed" if eng.store.set_suggestion(args.id, "dismissed") else "not found")
        return 0
    return 1


def _models(eng, args) -> int:
    """Interactive provider -> model -> effort menu on a terminal; otherwise the current choices. With
    PROVIDER MODEL [EFFORT], set them directly (models.set)."""
    from orchestrator import menus
    if args.args:
        from orchestrator.api import ApiError
        if len(args.args) not in (2, 3):
            print("usage: foreman models PROVIDER MODEL [EFFORT]   (MODEL `default` lets the CLI decide)", file=sys.stderr)
            return 1
        try:
            res = _api(eng).models_set(args.args[0], args.args[1], args.args[2] if len(args.args) == 3 else None)
        except ApiError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"{res['provider']}: {res['model']}" + (f" · effort {res['effort']}" if res["effort"] else ""))
        return 0
    if args.json:
        _dump({n: {"model": eng.cfg.providers[n].chosen().model, "effort": eng.cfg.providers[n].chosen().effort,
                   "levels": eng.cfg.providers[n].effort_levels()} for n in eng.providers if eng.cfg.providers[n].enabled})
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(menus.models_summary(eng))
        return 0
    note = menus.models_loop(eng)
    if note:
        print(note)
    return 0


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
            mark = "*" if isinstance(p.opened, (int, float)) and p.opened is not False else " "
            print(f"{mark} {p.name:<32} {','.join(p.stack) or '-':<14} rel:{rels:<3} {p.path}")
        print("* opened (listed by the app and the chat); `foreman open PATH` opens one, `foreman projects hide NAME` hides it")
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
    if action == "hide" and rest:
        p = reg.hide(rest[0])
        print(f"{p.name} is off the list until you open it again (`foreman open {p.path}`)" if p else "unknown project")
        return 0 if p else 1
    if action == "test" and len(rest) >= 2:
        p = reg.find(rest[0])
        if not p:
            print("unknown project")
            return 1
        p.test, p.source = " ".join(rest[1:]), "declared"
        reg.save()
        print(f"{p.name} test command: {p.test}")
        return 0
    print("usage: foreman projects [list|scan|show NAME|add PATH [NAME]|link A B TYPE [note]|unlink A B|test NAME CMD|hide NAME]")
    return 1


# Old names `foreman config` accepted, and their values, mapped onto settings.py's keys.
CONFIG_ALIASES = {"auto-switch": "auto_switch", "reuse": "reuse_sessions", "first-worker": "first_worker",
                  "copy-on-select": "copy_on_select", "on-close": "on_close", "output-style": "output_style",
                  "prompt-suggestions": "prompt_suggestions", "status-line": "status_line"}


def config_cmd(args) -> int:
    """`foreman config`: the settings (settings.py, the same list as the app's Settings and /config)."""
    from orchestrator import settings
    eng = build_engine(out=lambda _m: None)
    items = settings.describe(eng)
    if not args.key:
        if sys.stdin.isatty() and sys.stdout.isatty() and not args.list:  # a terminal: the interactive settings list
            from orchestrator import menus
            print(menus.config_menu(eng) or "No settings changed.")
            return 0
        print(f"config file: {os.path.join(eng.cfg.home, 'config.yaml')}")
        for it in items:
            value = ("on" if it["value"] else "off") if it["kind"] == "toggle" else it["value"]
            opts = "on|off" if it["kind"] == "toggle" else "|".join(o["value"] for o in it["options"])
            print(f"  {it['key']:<20} {str(value):<12} {it['label']}  [{opts}]" + (" (terminal chat)" if it["applies"] == "chat" else ""))
        for n, p in eng.cfg.providers.items():
            if p.enabled:
                print(f"  {n}: {p.chosen().label()}   (change: foreman models {n} MODEL [EFFORT])")
        print(f"roots: {', '.join(eng.cfg.roots)}")
        return 0
    key = CONFIG_ALIASES.get(args.key, args.key.replace("-", "_"))
    value = args.value or ""
    if key == "display" and value in ("terminal", "headless"):
        value = value == "terminal"
    elif key == "reuse_sessions" and value in ("fork", "off"):
        value = value == "fork"
    elif key in ("planner", "first_worker"):
        value = {"agy": "antigravity"}.get(value, value)
    try:
        settings.change(eng, key, value)
    except ValueError as e:
        print(f"{e}\nusage: foreman config KEY VALUE   (foreman config --list shows the settings)", file=sys.stderr)
        return 1
    it = next(x for x in settings.describe(eng) if x["key"] == key)
    print(f"{args.key} = " + (("on" if it["value"] else "off") if it["kind"] == "toggle" else str(it["value"])))
    return 0


def stop_cmd(eng, args) -> int:
    api = _api(eng)
    task = api.find(args.task)
    if api.tasks_stop(task["id"])["requested"]:
        print(f"Stop requested for #{task['id']}: it ends as interrupted at its next checkpoint; `foreman resume {task['id']}` continues it.")
    else:
        print(f"#{task['id']} is not running ({task['status']}).")
    return 0


def output_cmd(eng, args) -> int:
    api = _api(eng)
    task = api.find(args.task)
    outputs = api.tasks_outputs(task["id"])["outputs"]
    if not args.name:
        if args.json:
            _dump(outputs)
        elif not outputs:
            print(f"#{task['id']} has no recorded output.")
        else:
            for o in outputs:
                print(f"{o['name']:<40} {o['size']:>9} bytes")
            print(f"→ foreman output {task['id']} NAME")
        return 0
    offset = 0
    while True:
        page = api.tasks_output(task["id"], args.name, offset)
        sys.stdout.write(page["text"])
        offset = page["offset"]
        if page["eof"]:
            return 0


def conversations_cmd(eng, args) -> int:
    from datetime import datetime
    from orchestrator.api import ApiError
    api = _api(eng)
    convs = api.conversations_list()["conversations"]
    if not args.id:
        if args.json:
            _dump(convs)
            return 0
        for c in convs:
            when = datetime.fromtimestamp(c["updated_at"]).strftime("%Y-%m-%d %H:%M")
            tasks = f"  tasks: #{', #'.join(c['tasks'])}" if c["tasks"] else ""
            print(f"{c['id']}  {when}  {c['project'][:24]:<24} {c['title'][:60]}{tasks}")
        print("→ foreman conversations ID shows one; `foreman -r` in the project's folder continues one" if convs
              else "No conversations yet: `foreman` in a project's folder starts one.")
        return 0
    conv = next((c for c in convs if c["id"] == args.id), None)
    if not conv:
        raise ApiError("not_found", f"No conversation {args.id}.")
    got = api.conversations_get(conv["project"], conv["id"])
    if args.json:
        _dump(got)
        return 0
    from orchestrator.api import plain_output
    print(f"{got['conversation']['title'] or '(untitled)'} — {conv['project']}\n")
    for e in got["conversation"]["entries"]:
        print(f"> {e.get('shown') or e['user']}")
        task = got["tasks"].get(e.get("task") or "")
        print((f"#{task['id']} {task['title']} — {task['status']}" if task else plain_output(e.get("output") or "")).rstrip() + "\n")
    return 0


def open_cmd(eng, args) -> int:
    p = _api(eng).projects_open(args.path)
    print(f"Opened {p['name']}: {p['path']}")
    if args.no_chat or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return 0
    from orchestrator.chat import Chat
    eng.actor = "chat"
    Chat(p["path"], engine=eng).run()
    return 0


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
  foreman open [path]              open a project folder (listed by the app and the chat) and start the chat there
  foreman conversations [id]       conversations in every project (the chat's and the desktop app's), or one's messages
  foreman "<request>"              do something (plans, picks projects/sessions, executes, validates, reports)
  foreman --mode qa "<request>"    same, in a chosen mode (research, planning, development, qa, deployment)
  foreman --dry-run "<request>"    plan only: what would be done, nothing runs; `foreman resume` then runs that plan
  foreman lifecycle | next         where this project's features stand | start the recommended next step
  foreman modes                    the mode templates and where each comes from
  foreman models [PROVIDER MODEL [EFFORT]]   choose each provider's model and effort (interactive), or set one
  foreman config [KEY VALUE]       settings (interactive), or set one; --list shows them all
  foreman status [task]            state of a task (instant, no AI)
  foreman resume [task] [note]     continue a task, re-verifying against current code
  foreman answer <task> <text>     answer a task's open question
  foreman close <task> [note]      mark a task finished another way (--cancel: not wanted); "close #N" in chat
  foreman stop <task>              stop a running task (it ends as interrupted; resume continues it)
  foreman output <task> [run]      raw output of a task's worker runs (redacted when written)
  foreman tasks | show <task>      history and details (show --events for the audit log)
  foreman stats [--since DATE]     totals across tasks: outcomes, model calls, retries, time, cost (no AI)
  foreman suggestions | accept <id> | dismiss <id>
  foreman projects [list|scan|show|add|link|unlink|test|hide]
  foreman sessions [project]       existing Claude/OpenCode/Antigravity sessions for a project
  foreman doctor [--fix]           providers, display, MCP registration (--fix registers MCP where missing)
  --json                           machine-readable output on ask, status, tasks, show, lifecycle, next, resume, answer, doctor
  foreman completion bash|zsh      shell completion script (eval "$(foreman completion bash)")
  foreman --version                version (with the commit when run from a checkout)
  foreman mcp [install [claude|antigravity|codex]]   MCP server for Claude Code, Antigravity, Codex…
  foreman serve --stdio            the application API for the desktop app (JSON-RPC 2.0 on stdin/stdout)

  --visual / --headless               override the display mode for one run
"""


if __name__ == "__main__":
    sys.exit(main())
