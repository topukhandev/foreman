# Architecture

```
 Front ends (thin)        CLI (cli.py)        MCP server (mcp_server.py) ◄── Claude Code / Antigravity / Codex …
                               │                         │  (long tasks run detached: app.spawn_task)
                               └──────────┬──────────────┘
                                          ▼
 Core (engine.py)   request ─► fast path: status / resume / "what's next" / "next" (no AI)
                            └► plan (brain.py): mode, scope, steps, impact ─► questions? ─► needs_input
                                   │   risky change or consumers left out ─► impact investigation first
                                   │
                                   ▼
                       steps DAG ─ parallel across projects, one session per step
                                   │   new dependency reported ─► learn relation, re-plan (bounded)
                                   ▼
                       validate: checks run by Foreman + diffs + independent review
                                   │   problems ─► fix rounds in the same sessions (bounded)
                                   ▼
                       document modes ─► research brief / plan / QA report / release doc in the project
                       finalize: state, lifecycle + next step, relations verified, suggestions, report, notification
                                          │
 State (~/.foreman)   registry.py  projects.yaml      projects + relations (declared/discovered/learned)
                      store.py     tasks/<id>/        task.json, events.jsonl, logs/, report.md
                      budget.py    budget.json        daily runs per provider
                                          │
 Per project (git)    knowledge.py .foreman/       decisions.md, lifecycle.yaml, plans/ research/ qa/ releases/
 Modes                modes.py     modes/*.md         R&D, planning, development, QA, deployment templates
                                                      (overridable in ~/.foreman/modes or .foreman/modes)
 Providers            providers/claude.py · antigravity.py · opencode.py · generic.py (any CLI via config)
                      Capabilities: resume, fork, structured output, session discovery, enforced read-only, web
```

## Responsibilities

| Module | Deterministic or AI | Responsibility |
|---|---|---|
| `registry.py` | deterministic | Find projects under `roots`; detect stack and test command; discover relations from paths, module/package names and distinctive name mentions; record evidence plus the git HEADs at verification; select a compact context (focus projects, one-hop neighbours, names only for the rest). |
| `store.py` | deterministic | Task records, append-only event log, raw logs, task lookup (`#7`, words, or most recent in context), suggestions with dedup and dismissal. |
| `brain.py` | AI | Prompts and JSON schemas for planning, re-planning and review. Read-only, schema-validated, with fallback across providers. |
| `engine.py` | both | Orchestration loop: scheduling, provider/session choice, budget, check-command allow-list, git guards (HEAD moved = violation; read-only steps that edit are flagged), change detection, fix loop, finalize. |
| `providers/*` | adapter | One small module per CLI. Builds commands, parses output, discovers sessions. Nothing provider-specific leaks out. |
| `terminal.py` | adapter | Optional visible terminal windows. Same capture and exit semantics as headless runs. |
| `report.py` | deterministic | Short reports and status built from the task record, plus the single desktop notification. |
| `modes.py` + `modes/*.md` | data | Lifecycle mode templates: what workers may write (none / code / tests), web access, which checks run, where the result document goes, the usual next mode, and guidance for planner and workers. |
| `knowledge.py` | deterministic | The project's `.foreman/` folder: confirmed decisions, feature lifecycle, documents, team notes, check overrides. |

## Key decisions

- **Global state, not per-folder.** Cross-project work needs one place for projects, relations and tasks.
- **Progressive context.** The planner sees one-line cards for focus projects and their neighbours, only
  names for unrelated projects, and may open code read-only to settle scope. Workers report new dependencies;
  the engine records them as `learned` relations and re-plans at most `max_replans` times.
- **Trust, but verify history.** Relations and task results record git HEADs and file snapshots. Status flags
  projects that changed since the last run; resume passes those changes to the planner for re-verification.
- **Session reuse.** Continuity first (same session for fixes), then forking your most recent session in that
  project (≤ `reuse_max_age_hours`, providers that can fork). Then a new session. Foreman's own planning and
  review sessions are never mistaken for yours. Providers without fork (agy) only resume sessions Foreman created.
- **Independent review.** The reviewer is a different provider than the implementer when one is available,
  otherwise a fresh session.
- **Validation is Foreman's.** Test commands run as subprocesses with a timeout. Only recognised test/build
  commands run; destructive patterns are refused and reported.
- **Git policy.** Workers never commit, push, reset or switch branches (tool-level deny for Claude, prompt
  policy for all). Any HEAD movement is reported. Changes are measured against a snapshot, so your own
  uncommitted work isn't attributed to the task.
- **Asking.** Business questions, worker questions about technical choices that change what was asked (with the
  worker's options and recommendation), and the 80% budget sign-off pause a task. Routine technical choices are
  recorded as decisions. Answers to a worker go back into that worker's session; the plan is not redone.
- **Cut-offs are routine.** Providers translate their errors into failure kinds (`providers/base.py`: timeout,
  limit with reset time, transient, auth, context, output, crash) and report session ids while running. The
  engine journals every attempt per step and applies one policy per kind: resume the same session, hand over to
  the next provider with a handoff brief (changed files, progress notes, last actions), or wait for usage limits
  to reset (planning and review too). Unfinished work stays in the plan (dependent steps wait), the status and
  next step are derived from what actually happened, and "continue" runs the same plan in the same sessions.
- **Modes are data, not code.** One pipeline; a mode only changes instructions, permissions (read-only,
  tests-only, web) and checks. The planner recommends a mode per request (with a reason) unless the user fixed
  one. A task keeps its mode; the next mode is recommended deterministically from the template and the outcome
  (problems found → back to development) and started only when the user says "next".
- **Impact gate.** Before code changes, Foreman adds a read-only impact investigation when the planner rates
  the risk medium/high or when a known consumer of a changed project (relation pointing at it) is not in the
  plan; implement steps wait for it.
- **Knowledge lives with the code.** Answers to business questions go to `.foreman/decisions.md` in the
  project and are given to every later planner, worker and reviewer there. Lifecycle and documents sit next to
  them, so a team shares them through git. Change detection ignores `.foreman/`.

## Provider notes (verified)

| Provider | Headless | Session reuse | Read-only | Structured output |
|---|---|---|---|---|
| Claude Code 2.1 | `claude -p --output-format json` | `--resume` + `--fork-session`; sessions found in `~/.claude/projects/<cwd>` | tool allow/deny lists | `--json-schema` |
| Antigravity `agy` 1.4 | `agy --output-format json -p=…` | `--conversation=<id>` (no fork); index `~/.gemini/antigravity/conversation_summaries.db` | `--mode plan` + no terminal | `--json-schema` |
| OpenCode | `opencode run --format json` | `-s` + `--fork`; `opencode session list` | `--agent plan` | prompt + JSON extraction |
| Any CLI (`type: generic`) | `read_args` / `write_args` from config | none | only if the CLI enforces it | prompt + JSON extraction |

Web research: Claude gets `WebSearch`/`WebFetch` in allowed tools only for R&D/planning steps (tool-level); agy's
`search_web` can't be disabled per run, so it is allowed or forbidden in the prompt.

## Adding a provider

For a quick start, add it to `config.yaml` with `type: generic`, `command`, `read_args` and `write_args`
(placeholders `{prompt}`, `{cwd}`); the default config has a disabled Codex preset. For full support (sessions,
structured output, live activity), write `providers/<name>.py` with a `Provider` subclass (`build_command`,
`run`, optional `discover_sessions`, honest `capabilities`) and register it in `providers/__init__.py`.
