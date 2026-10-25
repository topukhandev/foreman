# Foreman

One entry point for AI-assisted development across **related projects**. You describe what you want; Foreman
works out which projects and sessions are involved, runs the work through your AI CLIs (Claude Code,
Antigravity `agy`, OpenCode, or any other CLI), validates the result itself, and gives you a short report.

The AI providers supply the intelligence; Foreman supplies the coordination, context, workflow and lifecycle.

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
- **Follows the lifecycle.** R&D, planning, development, QA and deployment modes, with a recommended mode per
  request and a recommended next step after each one. Impact on existing code and consumers is checked before
  risky edits, and confirmed business decisions are kept in the project and followed by every later session.

## Install

```bash
pip install -e .                 # or run ./foreman from the checkout
foreman doctor              # providers, display mode, projects found, MCP registration
foreman mcp install         # register with Claude Code / Antigravity / Codex (whichever are installed)
foreman --version           # version, plus the commit when run from a checkout
```

Shell completion for subcommands, `--mode` values and more:

```bash
eval "$(foreman completion bash)"                    # add to ~/.bashrc
foreman completion zsh > "${fpath[1]}/_foreman"      # zsh
```

Data from the time the project was called Switchyard (`~/.switchyard`) is moved to `~/.foreman` on first run;
`foreman mcp install` replaces a `switchyard` MCP registration.

Configuration lives in `~/.foreman/config.yaml` (created on first run): project roots, provider
preference order, limits, display mode and session reuse.

## Use

### Chat (recommended)

```bash
cd ~/Desktop/my-project
foreman
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
(`/status`, `/tasks`, `/show`, `/new`, `/display`, `/help`) are optional. `/mode` picks a lifecycle mode for
your next requests (or `auto`), `/lifecycle` shows where this project's features stand, and after a step a
menu offers the recommended next one. `/providers` opens an ↑/↓ menu to
choose the planner (and optionally the first worker); `foreman config planner agy` does the same from a shell.

Conversations are saved per project. `/resume` (or `foreman -r`) lists earlier chats in the current project
with ↑/↓ and restores the transcript and its context; `foreman -c` continues the latest one. The chat runs in
its own screen like vim, so your terminal is restored as it was when you exit. The mouse wheel or PgUp/PgDn
scroll the whole conversation (back to the welcome box) while the input box stays at the bottom; typing or Esc
returns to the latest output, and ↑/↓ stay for your earlier messages. Because the chat captures the mouse, hold
Shift to select text. `ui.alt_screen: false` in `~/.foreman/config.yaml` uses the terminal's normal scrollback instead.

### One-shot commands

```bash
foreman "Show the currency on the orders page; the API should return it"   # do something
foreman --mode planning "VIP discounts"   # in a chosen mode (research, planning, development, qa, deployment)
foreman lifecycle | next              # where features stand | start the recommended next step
foreman status                      # state of your current task (instant, no AI)
foreman resume                      # continue the most recent unfinished task here
foreman answer 3 "10% off above $500"   # answer a business question a task is waiting on
foreman accept s2 | dismiss s2      # act on, or silence, a suggestion
foreman tasks | show 3 [--events]   # history, full record, audit log
foreman projects [scan|show X|link A B tests "note"|test X "cmd"]
foreman sessions admin-ui           # existing Claude/OpenCode/Antigravity sessions for a project
foreman config display terminal|headless   # watch workers in terminal windows, or run under the hood
foreman config notify on|off        # one desktop notification when a task finishes
```

`--visual` / `--headless` override the display mode for a single run.

From Claude Code or Antigravity (after `foreman mcp install`), just ask normally. The tool's AI calls
Foreman's `orchestrate`, `task_status`, `resume_task`, `answer_question` and `suggestion` tools.

## Lifecycle modes

| Mode | What it does | Workers may | Result |
|---|---|---|---|
| R&D | domain, existing solutions, options, pain points, opportunities | read, search the web | `.foreman/research/<feature>.md` |
| Planning | maps what already exists, assesses impact, produces an actionable plan | read, search the web | `.foreman/plans/<feature>.md` |
| Development | implements across projects; impact check first when risky | change code | code changes + checks + review |
| QA | tests against requirements, regressions in related projects, lint/type/e2e checks | change **tests** only | `.foreman/qa/<feature>.md` |
| Deployment | release checklist, config/migrations, deploy order, rollback, post-deploy verification | read | `.foreman/releases/<feature>.md` |

Foreman recommends a mode for each request and says why ("Planning: changes shared money logic with three
consumers"); a quick question needs no mode. Override it with `/mode qa` in the chat or `--mode qa` on the
command line. After each step, the report recommends the next one (planning → development → QA → deployment;
problems found send it back to development). Say `next` to start it; "what's next?" shows where each feature
stands without an AI call. **Deployment never deploys**: it prepares and verifies, and you run the deploy.

Templates are plain markdown files (`foreman modes` lists them). Copy one to `~/.foreman/modes/<name>.md`
(personal) or `<project>/.foreman/modes/<name>.md` (shared with your team) to change it, or add your own mode.

### The `.foreman/` folder in each project

Foreman keeps project knowledge next to the code, so your team can share it through git:

- `decisions.md`: business decisions you confirmed (your answers to its questions). Every later planner, worker
  and reviewer in that project gets them.
- `lifecycle.yaml`: features, the stage each reached, and the recommended next step.
- `plans/`, `research/`, `qa/`, `releases/`: the documents those modes produce.
- `knowledge.md` (optional, yours): glossary, business rules, conventions the AI should know.
- `checks.yaml` (optional): check commands by kind, e.g. `lint: npm run lint`, `e2e: npm run test:e2e`. QA and
  deployment run them; lint, type-check, build and e2e scripts are otherwise detected from the project.

Only Foreman writes there (workers never do), and it is never counted as a code change.

### Adding another AI CLI

Any CLI can be added in `~/.foreman/config.yaml` without code:

```yaml
providers:
  codex:
    type: generic
    command: codex
    read_args: [exec, --sandbox, read-only, --skip-git-repo-check, "{prompt}"]
    write_args: [exec, --sandbox, workspace-write, --skip-git-repo-check, "{prompt}"]
    read_only_enforced: true
```

## Reports

Reports are short, with separate sections. Only non-empty sections are shown:

```
✔ #2 Add currency code to orders API and show it on the Orders page — achieved

  Projects  orders-api (changed 3 files) · admin-ui (changed 2 files) · e2e-tests (changed 1 file)
  Checks    ✔ orders-api: npm test  ✔ admin-ui: npm test  ✔ e2e-tests: npm test
  Review    approve by antigravity (different provider): …
  Sessions  claude ×3 (1 forked from your existing sessions)

What changed / Needs your decision / Remaining / Notes / Worth considering (your call)

Details: foreman show 2
```

"Worth considering" lists at most three *major* items you probably forgot, such as a consumer that will break or
a missing migration. Minor issues are fixed by the workers instead. Dismissed items are never shown again.

## Tests

```bash
python3 -m unittest discover tests
ruff check .          # lint: syntax errors, undefined names, unused imports (config in pyproject.toml)
```

GitHub Actions (`.github/workflows/ci.yml`) runs both on Python 3.9 to 3.13 for every push to `main` and every pull request.

## License

[MIT](LICENSE)

See [README_ARCHITECTURE.md](README_ARCHITECTURE.md) for the design.
