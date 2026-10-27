# Foreman

One entry point for AI-assisted development across **related projects**. You describe what you want; Foreman
works out which projects and sessions are involved, runs the work through your AI CLIs (Claude Code,
Antigravity `agy`, OpenCode, or any other CLI), validates the result itself, and gives you a short report.

The AI providers supply the intelligence; Foreman supplies the coordination, context, workflow and lifecycle.

- **Knows your projects.** Discovers projects under your folders and how they relate (UI → API → integration
  tests, shared modules). Relations carry evidence and are re-verified when code changes.
- **Scales effort to the request.** "What's the state of my task?" is answered instantly from its records, with no
  AI call. A question gets one planning call. A cross-project change gets a full plan, parallel workers,
  checks and an independent QA pass.
- **Says what was actually verified.** Foreman runs the tests itself and keeps "done" apart from "verified": a
  report says *verified by tests*, *approved by QA*, or *NOT verified* and why. See [Roles and verification](#roles-and-verification).
- **Remembers tasks.** Every task records its request, projects, decisions, evidence, sessions and what remains,
  so "continue the previous task" just works, after re-checking what changed since.
- **Survives cut-offs.** A worker that hits a usage limit, a network error, a timeout or a crash doesn't lose its
  work: its session is resumed, or another provider takes the step over with a handoff. When every provider is out
  of its limit, Foreman waits for the reset and carries on by itself. See [When a provider stops](#when-a-provider-stops).
- **Works with what you already use.** Any MCP-capable tool (Claude Code, the Antigravity app/CLI, Codex, …)
  can be the front end. Recent sessions are forked so workers inherit your context.
- **Autonomous where safe, asks where it matters.** Routine technical decisions are made automatically; business
  rules, and technical choices that change what was asked, are asked, with the worker's options and its
  recommendation; workers never commit; validation commands are allow-listed.
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

Press Esc to interrupt a running task ("Interrupted by user"; say "continue" later). Ctrl+C offers stop or keep it
running in the background.

While a task runs you see who is working (`◜ Working on orders-api with claude… (27s)`) and what it is doing
live: `◆ Implement(orders-api) · claude` with its tool calls lined up underneath (`╰ Read(server.js)`, then
`Edit(server.js)` aligned under it), then checks and review. Output longer than three lines ends with
`… +N lines (ctrl+o to expand)`; Ctrl+O shows the whole conversation with everything in full (q or Esc closes it).
A failing check shows the start of its output. Messages you type meanwhile are queued (↑ takes the last one back to
edit). `/btw <question>` asks a quick side question: it is answered on the spot, read-only, also while a task runs,
and it isn't saved in the conversation. Typing `/` opens a command menu (↑/↓, Tab or Enter). Providers that fail to log in (for example
an expired API key) are skipped for a few hours and shown in the welcome box. Ctrl+C lets you
stop the task or keep it running in the background. Decisions such as approving a cross-project plan or
answering a business question appear as an arrow-key menu. Press Esc to decide later by typing. Shortcuts
(`/status`, `/tasks`, `/show`, `/new`, `/display`, `/help`) are optional. `/mode` picks a lifecycle mode for
your next requests (or `auto`), `/lifecycle` shows where this project's features stand, and after a step a
menu offers the recommended next one. `/providers` opens an ↑/↓ menu to
choose the planner (and optionally the first worker); `foreman config planner agy` does the same from a shell.

Pasting: long text (4+ lines or 400+ characters) appears as `[Pasted text #1 +545 lines]` instead of flooding the
screen, and is sent in full; Backspace removes the whole chip. Pasting the same thing again right after its chip
expands it into the full text (for an image, its file path), like Claude Code. Up to 2000 pasted lines per message (the rest is
cut, with a notice). Drag an image file into the window, or press Ctrl+V with an image on the clipboard, to
attach it as `[Image #1]`; the file path goes to the AI, which can open it. Clipboard images use `wl-paste`,
`xclip` or `pngpaste` if installed, otherwise GTK through `python3-gi`. Requests too long for a command line
(a very large paste) are handed to the AI as a file.

Typing works like Claude Code: `\` then Enter starts a new line (each line gets its own row; ↑/↓ move between
lines), Ctrl+A / Ctrl+E go to the start / end of the line, Ctrl+U / Ctrl+K delete to the start / end of the line,
Ctrl+W deletes a word, and Ctrl+Y pastes back what those removed. Type `@` to pick a file or folder of this project
from a list (the AI gets the path). Dragging in any other file adds it as `[File #1 notes.csv]`. After an answer, a
suggested next message (the first "Next for you" item) waits in grey in the empty input: Tab or → takes it,
typing ignores it (`ui.prompt_suggestions`). Questions from the planner or a worker appear as Claude Code-style
panels: each option has a short label with a grey description under it, the recommendation first, and the last row
takes your own answer as you type. Several questions show as chips across the top (←/→ between them) with a review
step before they are sent together. A status line under the input shows the project, mode, the task in focus and
each provider's model (`ui.status_line`).

Conversations are saved per project. `/resume` (or `foreman -r`) lists earlier chats in the current project
with ↑/↓ and restores the transcript and its context; `foreman -c` continues the latest one. The chat runs in
its own screen like vim, so your terminal is restored as it was when you exit. The mouse wheel or PgUp/PgDn
scroll the whole conversation (back to the welcome box) while the input box stays at the bottom; typing or Esc
returns to the latest output, and ↑/↓ stay for your earlier messages. While you are scrolled up, the last message
you sent above the view stays pinned on the top row (click it to go there) and a bar at the bottom reads
"Jump to bottom · N new messages" (click it or press Esc). The terminal title shows `Foreman · <conversation>`;
`/rename <name>` names the conversation. Select text with the mouse (press, drag,
release), in the conversation or in what you are typing, and it is copied to the clipboard, with a "N characters copied to clipboard" note (needs `xclip`,
`wl-clipboard` or `pbcopy`, or `python3-gi`; over SSH it uses the terminal's own clipboard escape). Turn this off with
`foreman config copy-on-select off` (`ui.copy_on_select`), then hold Shift to select with the terminal itself.
`ui.alt_screen: false` in `~/.foreman/config.yaml` uses the terminal's normal scrollback instead.

### One-shot commands

```bash
foreman "Show the currency on the orders page; the API should return it"   # do something
foreman --dry-run "VIP discounts"          # plan only: what would be done; nothing runs. `foreman resume` then runs that plan
foreman --mode planning "VIP discounts"   # in a chosen mode (research, planning, development, qa, deployment)
foreman lifecycle | next              # where features stand | start the recommended next step
foreman status                      # state of your current task (instant, no AI)
foreman resume                      # continue the most recent unfinished task here
foreman answer 3 "10% off above $500"   # answer a question a task is waiting on ("1. …" per line for several)
foreman close 3 "done by hand"      # a task finished another way (--cancel: not wanted); "close #3" in the chat
foreman accept s2 | dismiss s2      # act on, or silence, a suggestion
foreman tasks | show 3 [--events]   # history, full record, audit log
foreman stats [--since 2026-10-01]  # outcomes, verification, model calls, retries, time, reported cost (no AI)
foreman projects [scan|show X|link A B tests "note"|test X "cmd"]
foreman sessions admin-ui           # existing Claude/OpenCode/Antigravity sessions for a project
foreman config                      # interactive settings; `foreman models` for model and effort
foreman config display terminal|headless   # watch workers in terminal windows, or run under the hood
foreman config notify on|off        # one desktop notification when a task finishes
```

`--visual` / `--headless` override the display mode for a single run.

`--json` prints machine-readable output for scripts and CI on `ask`, `status`, `tasks`, `show`, `lifecycle`,
`next`, `resume`, `answer` and `doctor`. Task results carry `status`, `projects`, `steps`, `checks`, open
`questions` and the human `report`:

```bash
foreman --dry-run "add currency to orders" --json | jq '.task.steps[] | {project, kind}'
foreman tasks --json | jq '.[] | select(.status=="needs_input")'
```

`foreman doctor --fix` registers Foreman with Claude Code / Antigravity / Codex where it is missing and rescans
projects when none are known; it only does safe, reversible things and prints what it did.

From Claude Code or Antigravity (after `foreman mcp install`), just ask normally. The tool's AI calls
Foreman's `orchestrate`, `task_status`, `resume_task`, `answer_question` and `suggestion` tools.

## Lifecycle modes

| Mode | What it does | Workers may | Result |
|---|---|---|---|
| R&D | domain, existing solutions, options, pain points, opportunities | read, search the web | `.foreman/research/<feature>.md` |
| Planning | maps what already exists, assesses impact, produces an actionable plan | read, search the web | `.foreman/plans/<feature>.md` |
| Development | implements across projects; impact check first when risky | change code | code changes + checks + QA |
| QA | tests against requirements, regressions in related projects, lint/type/e2e checks | change **tests** only | `.foreman/qa/<feature>.md` |
| Deployment | release checklist, config/migrations, deploy order, rollback, post-deploy verification | read | `.foreman/releases/<feature>.md` |

Foreman recommends a mode for each request and says why ("Planning: changes shared money logic with three
consumers"); a quick question needs no mode. Override it with `/mode qa` in the chat or `--mode qa` on the
command line. After each step, the report recommends the next one (planning → development → QA → deployment;
problems found send it back to development). Say `next` to start it; "what's next?" shows where each feature
stands without an AI call. **Deployment never deploys**: it prepares and verifies, and you run the deploy.

Templates are plain markdown files (`foreman modes` lists them). Copy one to `~/.foreman/modes/<name>.md`
(personal) or `<project>/.foreman/modes/<name>.md` (shared with your team) to change it, or add your own mode.

## Roles and verification

Workers take one of three roles, set by the step: a **Developer** implements, a **Researcher** investigates (read-only),
and a **QA verifier** checks the result independently (read-only, in its own session, on a different provider than the
developer's when there is one). What a role may change is fixed in code and narrowed by the mode; the guidance text of
a role can be replaced with `~/.foreman/roles/<role>.md` or `<project>/.foreman/roles/<role>.md`, which never grants
more. Every step keeps its structured assignment (role, permissions, criteria, context) for the audit trail.

The planner states 2-6 **acceptance criteria** for a change. Workers report a versioned result (tests they ran,
issues with severity and evidence, criteria ratings, risks, assumptions); Foreman checks it against its schema (a
malformed or unknown-version result is a failure, never a success) and compares what workers say about their tests
with its own runs ("Unverified claims" in the report).

After the work, Foreman runs the checks itself and decides how much verification the change needs, from the
planner's prediction **and** the actual diff:

| Change | Verification |
|---|---|
| light, low risk, one project, ≤ 3 files, its checks pass | Foreman's checks only |
| anything else | checks + one QA verify step |
| high risk, or migrations, auth, payments, secrets, CI, deploy files or dependency manifests changed | checks + QA with a risk checklist |

QA's verified problems (medium or worse) and failed criteria go back to **the developer's session**; QA re-checks in
**its own session**, at most `max_fix_rounds` times. Suspected problems are listed as QA notes, not fixed. QA can't
run commands, so it names the tests it wants and Foreman runs them. The report's first line and its `Verified` row
say how far the result is verified: *verified by tests*, *approved by QA (AI), not verified by tests*, *NOT verified*
(with the reason: no test command, QA couldn't run, not a git repository, …) or *verification failed*.

You are asked before:
- **High-impact changes**: when the planner flags a significant security, money, data-integrity, destructive or
  production impact, nothing changes until you approve the plan (in every interface, not just the chat).
- **New dependencies**: a dependency the work added (package.json, requirements, pyproject, composer, go.mod,
  Cargo.toml, Gemfile) needs your OK; "Remove it" sends that back to the same worker, who continues without it.

A question your project already decided (`.foreman/decisions.md`) is answered from there instead of being asked
again; a new answer to the same question supersedes the old one, which stays in the file marked as such.

**Reviewing your own work.** "QA my changes" or "review the feature branch" in QA mode reviews changes Foreman didn't
make (your uncommitted ones, or a branch against its base): Foreman runs the checks, QA reviews the diff, and the
problems are reported, not fixed.

### Safety

- Workers don't get secret-looking environment variables (`*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, the ssh agent,
  …) except the exact names their own CLI needs to log in (e.g. `ANTHROPIC_API_KEY` for Claude only); add one with
  `providers.<name>.env_keep` in `config.yaml`. Secret values are redacted from task records, events, logs and reports.
- Claude's read-only runs (planning, investigation, QA) are confined to the project folders (`--restricted`).
- Check commands from the registry or `.foreman/checks.yaml` run as written; ones the planner or QA propose run only
  if they are a known test, lint or type-check runner with plain arguments (no shell syntax, no flags that run other
  code, nothing outside the project), without a shell. Only test runs count as "verified by tests". Check output is
  redacted before it goes into a prompt.
- One task at a time changes a project, across chat, MCP and CLI processes (an OS file lock held for the whole run,
  checks and QA included; released by the OS if Foreman dies); others wait for it.
- A worker that changes the project's `.foreman/` files (check commands, decisions, templates) is reported, and those
  files are ignored for the rest of that task.
- Commits (HEAD moved) and pushes (remote branches changed) during a step are reported; files that already had your
  uncommitted edits and were changed by the task are named.
- Limits: workers that write can still read files outside the project, and a provider's own key is readable by its
  worker. Checks Foreman runs itself keep your full environment (tests may need it), so tests a worker wrote can
  read it too. Claude Code's bubblewrap sandbox would close that, but on Ubuntu it needs AppArmor to allow unprivileged
  user namespaces (see the Claude Code sandboxing docs); without that Claude runs unsandboxed, so Foreman doesn't
  rely on it.

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
✔ #2 Add currency code to orders API and show it on the Orders page — achieved · verified by tests

  Projects  orders-api (changed 3 files) · admin-ui (changed 2 files) · e2e-tests (changed 1 file)
  Checks    ✔ orders-api: npm test  ✔ admin-ui: npm test  ✔ e2e-tests: npm test
  Review    approved by QA on antigravity (different provider): …
  Verified  verified by tests (admin-ui, e2e-tests, orders-api passed); QA approved
  Sessions  claude ×3 (1 forked from your existing sessions)

What changed / Acceptance / Unverified claims / Risks / QA notes / Needs your decision / Remaining / Notes /
Next for you / Worth considering (your call)

Details: foreman show 2
```

Answers are written by the provider in Markdown (bold key terms, bullets, `code`), shown formatted in the chat.
"Next for you" lists up to three concrete things to do or ask next, written by the provider that did the work (the
planner for an answer, the workers for a change), from where the project stands and what you were after; it is
left out when a decision is waiting or the work stopped.

"Worth considering" lists at most three *major* items you probably forgot, such as a consumer that will break or
a missing migration. Minor issues are fixed by the workers instead. Dismissed items are never shown again.
Workers never run git. The one exception is a "run `git init`" suggestion for a project without a repository: when you
accept it (`do s6`), Foreman itself runs `git init` (no commit), so it can detect and review changes there.

## Models, effort and settings

Foreman chooses the model and reasoning effort for every run instead of leaving it to whatever the CLI last had (your
`/model` in Claude Code no longer changes what Foreman's workers use). You choose once per provider:

```bash
foreman models      # interactive: pick the provider, then the model, then the effort for that model
foreman config      # interactive settings list (below)
```

The same menus are `/models` and `/config` in the chat. The lists come from the CLIs themselves, so new models and
levels appear without a Foreman release, and each model offers only the effort levels it supports: Claude's
aliases and levels are read from `claude --help`; Antigravity's from `agy models` (Gemini Flash has low / medium /
high, other models a single fixed setting); OpenCode's from `opencode models --verbose` (it has hundreds of models, so
you pick its backend first and can type part of a name to narrow the list; levels differ per model). You can also
type a model name that isn't listed.

Until you choose, Claude uses Sonnet 5.5 and Antigravity Gemini 3.8 Flash, both at high effort (never max); OpenCode uses
its own default. The planner rates each step light or standard; **light steps and planning run one effort level below
your choice** (when the model has a lower level), everything else, review included, runs exactly as chosen. Reports
show what ran ("Models  claude sonnet (xhigh) ×3").

`foreman config` lists these settings with their current values in a panel that leaves nothing in the chat; typing
filters the list, Enter flips a true/false setting on the spot and opens a sub-panel for the others, ←/→ switch to
the Status tab (version, config file, projects, what each provider runs), Esc closes:

| Setting | |
|---|---|
| Show workers in terminal windows | yes: each worker opens in a visible terminal; no: headless |
| Switch provider automatically at a limit | yes: another provider takes over the step; no: Foreman waits for that provider's reset and continues in the same session |
| Models and effort | the menu above |
| Planner · first worker | which provider plans and reviews · which one works first |
| Output style | how answers and documents are written: Default, Concise, Explanatory (adds a ★ Insight), Learning (leaves small steps for you), Proactive (points out the next action or risk) |
| Suggest your next message · status line | the grey suggestion in the input · the line under it |
| Notification · recent sessions · copy on select | one desktop notification per task run · fork your recent sessions · copy selected text in the chat |

The short forms still work (`foreman config display headless`, `foreman config auto-switch off`, ...).

## When a provider stops

Every attempt at a step is journaled (provider, session, why it ended) and the session id is saved the moment the
CLI prints it, so closing the terminal or a crash loses nothing. What happens next depends on why it stopped:

| Why | What Foreman does |
|---|---|
| Foreman's time limit | resumes the **same session** with double the time (up to `timeout_retries`) |
| Network drop, API overload | resumes the **same session** after 30 s, 2 min, 5 min (`transient_retries`) |
| Finished but no final report | asks the **same session** for the report only |
| Usage limit (e.g. Claude "resets 7:10pm") | rests that provider until its reset; **another provider takes over** (unless you turned automatic switching off: then Foreman waits for it) with a handoff: who worked on it and why it stopped, files changed since the step started, the worker's progress notes, its last actions |
| Login problem / expired key | rests it for 6 h, tells you to renew the key, another provider takes over |
| Context window full | a fresh session of the same provider, with the handoff |
| Crash | one fresh retry with the handoff, then the next provider |
| Every provider out of limits | waits for the earliest stated reset (doesn't count), otherwise checks every 5 min (`limit_retry_sec`) up to 10 times (`limit_retries`), then pauses; a reset more than `limit_wait_max_hours` away pauses at once. Esc Esc stops the wait. |
| Foreman itself closed or killed | "continue" resumes the cut-off session where it stopped |

A provider that reports it is near its usage window (Claude does, `pressure_percent`, default 90%) keeps the
sessions it has, but new work goes to the others first.

When a worker needs a decision (a business rule, or a technical choice that changes what was asked) the task waits
with the question, its options and the worker's recommendation; your answer goes back into **the same session**.
The steps after an unfinished one wait instead of being skipped, and a half-done plan is never "fixed" or written
up. The report says where the task stopped and what continuing does; "continue" (or "next") carries on with that
task, in the same sessions. It is re-planned only when you add instructions ("continue, but use the CSV dumps") or
the code changed since it last ran.

## Tests

```bash
python3 -m unittest discover tests
ruff check .          # lint: syntax errors, undefined names, unused imports (config in pyproject.toml)
```

GitHub Actions (`.github/workflows/ci.yml`) runs both on Python 3.9 to 3.13 for every push to `main` and every pull request.

## License

[MIT](LICENSE)

See [README_ARCHITECTURE.md](README_ARCHITECTURE.md) for the design.
