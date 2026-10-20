# Orchestrator Blueprint (working name: Foreman / Quorum)

A local, cross-platform, open-source AI orchestrator. The user talks to ONE manager agent; the manager delegates work to AI CLI "workers" on the user's PC, reviews their output, and returns only the final result.

## 1. Core Principles
- One front desk: the user talks only to the manager (e.g. Antigravity app, or any chosen interface – predefined, not dynamic).
- Workers never talk to the user directly.
- Manager decides routing itself; interrupts the user only for real decisions, blockers, or manual steps.
- Every result is reviewed by a different worker (different provider if available, otherwise a fresh session).
- Simple first, extensible later.

## 2. Decisions Made
- Language: **Python** (cross-platform: Windows, macOS, Linux).
- Communication: **file-based inbox** first; a local **MCP server** later for GUI apps (e.g. Antigravity desktop).
- License: open source (license chosen later).
- Memory: three-layer, lightweight (see section 6).
- Limits: enforced by a Python budget guard, NOT by the AI.

## 3. Architecture
```
User <-> Manager (Antigravity app / chosen UI)
            |
      Python core (controller)
       |- providers.yaml (per machine)
       |- router (role -> provider)
       |- budget guard
       |- adapters (one per CLI)
       |- hub/ (inbox, tasks, status, logs)
            |
  Workers: Antigravity CLI, Claude Code (claude -p), OpenCode (opencode run), others
```

## 4. Provider Registry (`providers.yaml`, local per machine, never synced)
```yaml
machine: home-pc
os: auto
providers:
  antigravity_cli:
    enabled: true
    tier: paid
    command: "<antigravity non-interactive command>"
    roles: [planner, coder, researcher, reviewer]
    limits: {max_runs_per_day: 50, max_parallel: 2}
  claude_code:
    enabled: false
    tier: paid
    command: "claude -p"
    roles: [planner, coder, reviewer]
  opencode:
    enabled: false
    tier: free
    command: "opencode run"
    roles: [bulk, researcher]
routing:
  reviewer_must_differ_from_builder: true
  fallback: any_enabled
```
- At startup: detect OS, verify each enabled command exists, disable anything missing and report it in plain language.
- If only one provider is enabled (e.g. Antigravity), all roles run as separate sessions of that provider.

## 5. Hub Layout (file-based protocol)
```
hub/
  state.md          # one page: goal, phase, active tasks, blockers
  inbox/            # messages between manager and workers
  tasks/<id>.md     # one file per task
  status/<id>.json  # {task, worker, state, one_line_summary}
  logs/<id>.log     # full raw output (user never needs to read)
  archive/          # finished tasks summarised to 1-2 lines
```
Message format (inbox): `from, to, task_id, type (assign|result|review|revise|question), body`.

## 6. Memory Rules
1. Manager reads only `state.md` by default.
2. Opens task files only when needed.
3. Finished tasks are summarised and moved to archive; state.md never grows beyond one page.

## 7. Workflow Loop
1. User states goal in natural language.
2. Planner creates a full checklist (nothing forgotten: costs, risks, legal, data sources).
3. Critic (different model if possible) finds gaps.
4. Workers execute tasks (with web access via built-in or MCP search/fetch tools).
5. Reviewer checks each result against the checklist; failures go back for revision.
6. Manager gives the user a short report. User changes of mind update the plan and notify affected workers.

## 8. Budget Guard (Python, non-negotiable)
- Max parallel workers; max runs per provider per day.
- Max time per task; max retries before giving up.
- Max files / total size per project.
- Warn at ~80% of any limit: "Antigravity is at 80% of today's limit. Continue or wait?"
- Detect loops (same task bouncing too many times) and stop.

## 9. Manual Steps (Setup Doctor)
When something needs the user (login, admin install, payment):
- Pause and show the exact command for the user's OS plus a one-line reason.
  Example: "Run `claude login` – this lets me use Claude as a reviewer."

## 10. Ease of Use
- One install command / installer.
- First-run setup chat: asks use case + subscriptions, auto-detects tools, writes providers.yaml.
- Presets: Free only / Antigravity only / Claude / Use everything.
- Plain-language changes: "I just got Claude Pro" -> manager updates config.
- Advanced mode: edit YAML directly.
- Role packs: Business idea, Developer, QA engineer, Manager, Beginner explorer.

## 11. Security
- Workers run inside the project folder (or container).
- Command allow-list; approval required for delete, install, payments, external data sending.
- API keys kept out of agents' reach.
- Treat web pages and cloned repos as untrusted (prompt injection).
- Log every action. Note: no system is 100% secure.

## 12. Other Considerations
- Check each provider's terms of service regarding automated/scripted use.
- Give each worker its own git branch/worktree to avoid file clashes.
- Timeouts, retries, resume after crash.
- Usage/cost tracking per project.
- Keep each CLI adapter small so version changes are easy to fix.

## 13. Version 1 Scope
- Python core + file hub + budget guard.
- Two providers (Antigravity CLI + one other), one role pack (Business idea).
- Setup chat + setup doctor.
- Later: MCP server for GUI apps, web dashboard, more role packs.

## 14. Instructions for the Building Agent
Build version 1 per section 13. Start with the folder structure, providers.yaml loader with command detection, adapters, router, budget guard, then the manager rules file (AGENTS.md) describing delegation, review, and short reporting. Ask the user only when a decision is genuinely required.
