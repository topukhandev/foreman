# Foreman Orchestrator

One entry point for AI-assisted development across **related projects**. You describe what you want; Foreman
works out which projects and sessions are involved, runs the work through your AI CLIs (Claude Code,
Antigravity `agy`, OpenCode), validates the result itself, and gives you a short report.

- **Knows your projects.** Discovers projects under your folders and how they relate (UI → API → integration
  tests, shared modules). Relations carry evidence and are re-verified when code changes.
- **Scales effort to the request.** "What's the state of my task?" is answered instantly from its records, with no
  AI call. A question gets one planning call. A cross-project change gets a full plan, parallel workers,
  checks and review.
- **Remembers tasks.** Every task records its request, projects, decisions, evidence, sessions and what remains,
  so "continue the previous task" just works, after re-checking what changed since.
- **Works with what you already use.** Any MCP-capable tool (Claude Code, the Antigravity app/CLI, Codex, …)
  can be the front end. Recent sessions are forked so workers inherit your context.
- **Autonomous where safe, asks where it matters.** Routine technical decisions are made automatically;
  business rules are asked; workers never commit; validation commands are allow-listed.

## Install

```bash
pip install -e .                 # or run ./orchestrator from the checkout
orchestrator doctor              # providers, display mode, projects found, MCP registration
orchestrator mcp install         # register with Claude Code / Antigravity / Codex (whichever are installed)
```

Configuration lives in `~/.foreman/config.yaml` (created on first run): project roots, provider
preference order, limits, display mode and session reuse.

## Use

### Chat (recommended)

```bash
cd ~/Desktop/my-project
orchestrator
```

This opens a chat like `claude` or `agy`. You type in plain language:

- "what's the current state of this project?"
- "show the currency on the orders page; the API should return it"
- "what's the status of my task?"
- "continue"

While a task runs you see who is working (`◜ Working on orders-api with claude… (27s)`) and what it is doing
live: `◆ Implement(orders-api) · claude` with `╰ Read(server.js)`, `╰ Edit(server.js)` underneath, then checks
and review. Typing `/` opens a command menu (↑/↓, Tab or Enter). Providers that fail to log in (for example
an expired API key) are skipped for a few hours and shown in the welcome box. Ctrl+C lets you
stop the task or keep it running in the background. Decisions such as approving a cross-project plan or
answering a business question appear as an arrow-key menu. Press Esc to decide later by typing. Shortcuts
(`/status`, `/tasks`, `/show`, `/new`, `/display`, `/help`) are optional. `/providers` opens an ↑/↓ menu to
choose the planner (and optionally the first worker); `orchestrator config planner agy` does the same from a shell.

Conversations are saved per project. `/resume` (or `orchestrator -r`) lists earlier chats in the current project
with ↑/↓ and restores the transcript and its context; `orchestrator -c` continues the latest one. The chat runs in
its own screen like vim, so your terminal is restored as it was when you exit. The mouse wheel or PgUp/PgDn
scroll the whole conversation (back to the welcome box) while the input box stays at the bottom; typing or Esc
returns to the latest output, and ↑/↓ stay for your earlier messages. Because the chat captures the mouse, hold
Shift to select text. `ui.alt_screen: false` in `~/.foreman/config.yaml` uses the terminal's normal scrollback instead.

### One-shot commands

```bash
orchestrator "Show the currency on the orders page; the API should return it"   # do something
orchestrator status                      # state of your current task (instant, no AI)
orchestrator resume                      # continue the most recent unfinished task here
orchestrator answer 3 "10% off above $500"   # answer a business question a task is waiting on
orchestrator accept s2 | dismiss s2      # act on, or silence, a suggestion
orchestrator tasks | show 3 [--events]   # history, full record, audit log
orchestrator projects [scan|show X|link A B tests "note"|test X "cmd"]
orchestrator sessions admin-ui           # existing Claude/OpenCode/Antigravity sessions for a project
orchestrator config display terminal|headless   # watch workers in terminal windows, or run under the hood
orchestrator config notify on|off        # one desktop notification when a task finishes
```

`--visual` / `--headless` override the display mode for a single run.

From Claude Code or Antigravity (after `orchestrator mcp install`), just ask normally. The tool's AI calls
Foreman's `orchestrate`, `task_status`, `resume_task`, `answer_question` and `suggestion` tools.

## Reports

Reports are short, with separate sections. Only non-empty sections are shown:

```
✔ #2 Add currency code to orders API and show it on the Orders page — achieved

  Projects  orders-api (changed 3 files) · admin-ui (changed 2 files) · e2e-tests (changed 1 file)
  Checks    ✔ orders-api: npm test  ✔ admin-ui: npm test  ✔ e2e-tests: npm test
  Review    approve by antigravity (different provider): …
  Sessions  claude ×3 (1 forked from your existing sessions)

What changed / Needs your decision / Remaining / Notes / Worth considering (your call)

Details: orchestrator show 2
```

"Worth considering" lists at most three *major* items you probably forgot, such as a consumer that will break or
a missing migration. Minor issues are fixed by the workers instead. Dismissed items are never shown again.

## Tests

```bash
python3 -m unittest discover tests
```

See [README_ARCHITECTURE.md](README_ARCHITECTURE.md) for the design.
