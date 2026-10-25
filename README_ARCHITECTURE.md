# Architecture

```
 Front ends (thin)        CLI (cli.py)        MCP server (mcp_server.py) ◄── Claude Code / Antigravity / Codex …
                               │                         │  (long tasks run detached: app.spawn_task)
                               └──────────┬──────────────┘
                                          ▼
 Core (engine.py)   request ─► fast path: status / resume (no AI)
                            └► plan (brain.py) ─► questions? ─► needs_input
                                   │
                                   ▼
                       steps DAG ─ parallel across projects, one session per step
                                   │   new dependency reported ─► learn relation, re-plan (bounded)
                                   ▼
                       validate: checks run by Foreman + diffs + independent review
                                   │   problems ─► fix rounds in the same sessions (bounded)
                                   ▼
                       finalize: state, relations verified, suggestions, report, one notification
                                          │
 State (~/.foreman)   registry.py  projects.yaml      projects + relations (declared/discovered/learned)
                      store.py     tasks/<id>/        task.json, events.jsonl, logs/, report.md
                      budget.py    budget.json        daily runs per provider
                                          │
 Providers            providers/claude.py · antigravity.py · opencode.py   (mock.py for tests)
                      Capabilities: resume, fork, structured output, session discovery, enforced read-only
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
- **Asking.** Only business/product questions and the 80% budget sign-off pause a task. Technical choices
  are recorded as decisions.

## Provider notes (verified)

| Provider | Headless | Session reuse | Read-only | Structured output |
|---|---|---|---|---|
| Claude Code 2.1 | `claude -p --output-format json` | `--resume` + `--fork-session`; sessions found in `~/.claude/projects/<cwd>` | tool allow/deny lists | `--json-schema` |
| Antigravity `agy` 1.4 | `agy --output-format json -p=…` | `--conversation=<id>` (no fork); index `~/.gemini/antigravity/conversation_summaries.db` | `--mode plan` + no terminal | `--json-schema` |
| OpenCode | `opencode run --format json` | `-s` + `--fork`; `opencode session list` | `--agent plan` | prompt + JSON extraction |

## Adding a provider

Write `providers/<name>.py` with a `Provider` subclass (`build_command`, `run`, optional `discover_sessions`,
honest `capabilities`), register it in `providers/__init__.py`, and add it to `config.yaml`.
