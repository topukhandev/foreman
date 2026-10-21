# Orchestrator — Architecture & Commands Reference

> **Project Codename:** Foreman / Quorum  
> **Install:** `pip install -e .` (from repo root)  
> **Entry Point:** `orchestrator` (or `python -m orchestrator.cli`)

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        User / Manager Agent                     │
│  (Interactive Shell  •  MCP Client  •  Direct API)              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    OrchestratorEngine (src/engine.py)           │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │   Router    │  │  Budget     │  │    Hub      │              │
│  │ (role→prov) │  │  Guard      │  │ (FS memory) │              │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
       ┌────────────┐  ┌────────────┐  ┌────────────┐
       │ Adapter:   │  │ Adapter:   │  │ Adapter:   │
       │ Antigravity│  │  Claude    │  │  OpenCode  │
       └────────────┘  └────────────┘  └────────────┘
              │               │               │
              ▼               ▼               ▼
       ┌─────────────────────────────────────────┐
       │         Your Local AI CLIs              │
       │  (Subprocess calls, isolated sessions)  │
       └─────────────────────────────────────────┘
```

---

## Key Modules (src/orchestrator/)

| Module | Responsibility |
|--------|----------------|
| `cli.py` | Command dispatch, arg parsing, subcommands |
| `config.py` | `providers.yaml` load/validate, defaults |
| `router.py` | Role → provider mapping, fallback, reviewer≠builder |
| `budget.py` | Daily limits, concurrency, timeouts, 80% warnings |
| `hub.py` | File-based message bus: inbox/, tasks/, status/, logs/, archive/ |
| `engine.py` | Orchestrates packs, runs workflow loop (Plan→Critique→Exec→Review) |
| `adapters/` | Per-CLI wrapper: `base.py`, `opencode.py`, `claude.py`, `antigravity.py`, `mock.py` |
| `packs/` | Role definitions: `developer.py`, `business_idea.py`, `qa.py` |
| `wizard.py` | Interactive first-run setup (detects CLIs, writes config) |
| `doctor.py` | Diagnoses installed tools, reports missing/broken |
| `repl.py` | Interactive front-desk shell (`orchestrator shell`) |
| `mcp_server.py` | MCP stdio server for GUI apps (Antigravity desktop) |
| `progress.py` | Live dashboard rendering for visible workers |

---

## Hub Directory Layout

```
<project>/
├── providers.yaml          # Machine-local provider registry (never synced)
├── hub/
│   ├── state.md            # Single-page: goal, phase, active tasks, blockers
│   ├── inbox/              # JSON messages: assign/result/review/revise/question
│   ├── tasks/<id>.md       # One file per task (checklist, context, results)
│   ├── status/<id>.json    # {task, worker, state, one_line_summary}
│   ├── logs/<id>.log       # Full raw worker output (debug only)
│   └── archive/            # Finished tasks → 1-2 line summaries
```

---

## Commands Quick Reference

```bash
# ── Setup & Diagnostics ──────────────────────────────────────
orchestrator init [path]              # Initialize hub + default providers.yaml
orchestrator setup [path] [--preset]  # Interactive wizard (presets: 1-6, auto, antigravity_only, claude_only, dual_model, free_only, mock)
orchestrator doctor [path]            # Diagnose CLIs, show enabled/disabled, suggest fixes
orchestrator status [path]            # Print hub/state.md (current goal, tasks, blockers)

# ── Run Workflows ────────────────────────────────────────────
orchestrator run "your goal here"     # Run developer pack (default)
orchestrator run "goal" --pack business_idea
orchestrator run "goal" --pack qa
orchestrator run "goal" --mock        # Simulated workers (no real CLIs needed)
orchestrator run "goal" --visual      # Force visible terminal windows
orchestrator run "goal" --headless    # Force headless (default if --visual not set)

# ── Interactive & Integration ────────────────────────────────
orchestrator shell [path]             # Launch front-desk REPL (default if no args)
orchestrator mcp [--project path]     # Start MCP stdio server for GUI apps
```

---

## Provider Registry (`providers.yaml`)

```yaml
machine: my-laptop
os: auto
providers:
  antigravity_cli:
    enabled: true
    tier: paid
    command: "antigravity run --non-interactive"
    roles: [planner, coder, researcher, reviewer]
    limits: {max_runs_per_day: 50, max_parallel: 2}
  claude_code:
    enabled: true
    tier: paid
    command: "claude -p"
    roles: [planner, coder, reviewer]
  opencode:
    enabled: true
    tier: free
    command: "opencode run"
    roles: [bulk, researcher]
routing:
  reviewer_must_differ_from_builder: true
  fallback: any_enabled
```

- **Auto-detected** on first `orchestrator setup`
- **Per-machine** (never committed)
- **Role-based routing** — each task role maps to a provider
- **Reviewer ≠ Builder** enforced by router

---

## Role Packs (Workflows)

| Pack | Roles | Use Case |
|------|-------|----------|
| `developer` | planner, critic, coder, researcher, reviewer | Feature implementation, refactoring, debugging |
| `business_idea` | planner, critic, researcher, reviewer | Market research, MVP specs, go-to-market |
| `qa` | planner, coder, reviewer | Test generation, bug repro, regression suites |

Each pack defines a sequence of tasks with role assignments. The engine dispatches each task to the routed provider.

---

## Budget Guard (Hard Limits)

| Limit | Default | Configured In |
|-------|---------|---------------|
| Max parallel workers | 2 | `providers.yaml` per provider |
| Max runs/day per provider | 50 (paid), 20 (free) | `providers.yaml` |
| Max task runtime | 300s | `budget.py` constant |
| Max review bounces | 3 | `budget.py` constant |
| Warn threshold | 80% of any limit | `budget.py` |

At 80%: Manager pauses and asks user: *"Provider X at 80% daily limit. Continue or wait?"*

---

## Message Protocol (Hub Inbox)

Each message is a JSON file in `hub/inbox/`:

```json
{
  "from": "manager",
  "to": "worker:opencode:1",
  "task_id": "task-123",
  "type": "assign",
  "body": { "role": "coder", "prompt": "Implement X...", "context": "..." }
}
```

Types: `assign`, `result`, `review`, `revise`, `question`

---

## Adding a New Provider Adapter

1. Create `src/orchestrator/adapters/your_cli.py` extending `BaseAdapter`
2. Implement: `run(prompt: str, role: str) -> str`
3. Register in `src/orchestrator/adapters/__init__.py`
4. Add to `providers.yaml` with roles & limits

---

## Development

```bash
# Install in dev mode
pip install -e .

# Run tests
pytest tests/

# Type check (if configured)
mypy src/orchestrator

# Lint
ruff check src/orchestrator
```

---

## Security Notes

- Workers run **inside project directory** (or container if configured)
- Command allow-list — destructive ops require approval
- API keys **never** passed to workers (CLI handles auth)
- Web content / cloned repos treated as **untrusted** (prompt injection risk)
- All actions logged to `hub/logs/`

---

## License

Open source (license TBD). See `orchestrator_blueprint.md` for full spec.