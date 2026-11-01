# AGENTS.md: rules for agents working on and inside Foreman

## Working on this codebase

- Python ≥ 3.9, one dependency (`pyyaml`). Tests: `python3 -m unittest discover tests`; lint: `ruff check .` (both run in CI; must stay green and fast;
  use `tests/helpers.py` workspaces and the scripted `MockProvider`, never real CLIs, in unit tests).
- Keep the split described in `README_ARCHITECTURE.md`: deterministic state/safety in `registry.py`,
  `store.py`, `engine.py`; AI prompts and schemas only in `brain.py`; CLI specifics only in `providers/`; role
  permissions only in `roles.py` (guidance files are text and must never grant anything).
- Interfaces (`cli.py`, `mcp_server.py`) stay thin. New behaviour goes into the engine so every front end
  gets it.

## One feature, every front end (the protocol)

Foreman has two user front ends that must stay aligned: the CLI (`foreman …` and the terminal chat) and the desktop
app (`desktop/`). `src/orchestrator/surface.py` is the contract between them.
- Build a feature once, in the core. The behaviour goes in the engine and core modules; the data shape goes in
  `api.Api`, as a JSON-RPC method in `rpc_server.METHODS`.
- Then offer it in both front ends in the same change: a CLI command (through `api.Api`, so both get the same data
  and errors) and the app. Record where each offers it in `surface.OPERATIONS`.
- A front end may leave an operation out only with a written reason (`"-: why"`), or offer it through another
  operation (`"via <method>"`).
- `tests/test_protocol.py` fails when something isn't accounted for: an unlisted API method, a CLI command with no
  operation or reason, a CLI form that doesn't exist, or an app that doesn't call what it claims to offer.
- Changing an operation's behaviour or data means checking both front ends and the API contract
  (`docs/api-contract.md`, with a minor version bump for additions).
- Shared state lives in the core files that every process reads: tasks, conversations (`conversations.py`), the
  project registry (re-read when another process changes it), and `config.yaml` (`settings.py`). A front end
  never keeps its own copy of a user-visible list or setting.
- Opened projects are part of that shared state: the app's folder picker, `foreman open`, the chat started in a
  project, and a request sent from a project's folder all open it; both front ends list only opened projects.
- Don't add UI layers (visual dashboards, voice, custom line editors) unless the user asks. Visible
  terminal windows are supported only through `terminal.py`.
- Verify provider behaviour against the installed CLI before changing an adapter, and record what was verified
  in the adapter's docstring.

## Rules Foreman gives worker sessions (enforced by the engine)

1. Edit only inside the step's project; related projects are read-only context.
2. Never commit, push, reset, stash, clean or switch branches. HEAD movement is reported as a violation.
   The one git action Foreman itself takes is `git init` (no commit), only when the user accepts a suggestion for it.
3. Investigate steps are read-only; any file change is reported.
4. Don't invent business rules: return a `business` question instead. A technical choice that changes what was asked
   (new dependency, dropping a source, deviating from the plan) is a `technical` question with options and a
   recommended `default`. Every worker question reaches the user; the answer goes back into the same session.
5. Report discovered cross-project dependencies with evidence; they become `learned` relations.
6. Fix small related problems in place; only major, out-of-scope items become suggestions to the user.
7. Follow the mode: R&D, planning and deployment are read-only; QA may change test files only (other changes are
   reported); web research only in R&D and planning. Deployment never runs deploy, publish, migration or
   infrastructure commands; it prepares and verifies, and the user deploys.
8. Follow the confirmed decisions in the project's `.foreman/decisions.md`. Only Foreman writes to
   `.foreman/`; workers never do.
9. Before code changes that are risky or touch something with known consumers, an impact investigation runs first.
10. Report results in the versioned contract (`brain.STEP_SCHEMA`); never report a test as passed without running it.
    Foreman validates every result and re-runs the checks itself; claims it can't confirm are reported as such.
11. QA verifies independently (read-only, its own session) and its verified problems go back to the developer's
    session; a result is only reported as verified when Foreman's checks or QA support it.
12. High-impact changes (security, money, data integrity, destructive, production) wait for the user's approval of
    the plan; a new dependency is never added silently (the user keeps it or the worker removes it).

## Models and effort

- One model + effort per provider, chosen with the interactive `foreman models` / `/models` (saved as
  `providers.<name>.model/effort/levels`; built-in defaults in `config.DEFAULT_MODELS`). The planner only rates a
  step's `weight` (light/standard); light steps and planning run one level below the choice (`ProviderSettings.choice`).
  There are no per-task overrides: models and effort change only through the menu.
- Models and effort levels come from the CLIs (`Provider.list_models`), never from a hard-coded list; verify what a
  CLI accepts before passing it and record that in the adapter's docstring.
- `auto_switch: false` makes a usage limit wait for the same provider (steps, planning, review) instead of handing over.
  Settings the user may want to change belong in `settings.py` (the terminal menu `menus.config_menu` and the desktop app both show that list).

## Limits (config.yaml → `limits`)

- Concurrency `max_parallel` (default 4) within a task; `max_global` (default 6) AI CLI runs at once across all tasks and
  Foreman processes (slot locks); one running step per project at a time.
- Step timeout `step_timeout_sec` (default 1 h; a planner can ask for more per step with `expected_minutes`, about
  1.5x). A step that runs out of time resumes the same session (same provider) up to `timeout_retries` times with
  double the time each time, capped by `step_timeout_max_sec`; it never starts over in a fresh session for a
  timeout. Validation command timeout `check_timeout_sec`.
- A cut-off run is never lost: session ids are saved as soon as the CLI prints them and every attempt is journaled.
  Network errors resume the same session (`transient_retries`); a usage limit or login problem rests the provider
  and hands the step to the next one with a handoff brief; when all providers are out of limits Foreman waits for
  the reset or checks every `limit_retry_sec` (`limit_retries` times), then pauses. Providers at `pressure_percent`
  of their usage window get no new work if another can do it.
- Steps after an unfinished one wait (never skipped); a half-done plan is not checked or fixed. "continue" / "next"
  carry on the same task and sessions; re-planning only for new instructions or code changed since the last run.
- `max_fix_rounds` (default 3) QA/check bounces back to the developer, then escalate as Remaining.
- `max_replans` (default 2) plan expansions after discoveries.
- Daily runs per provider: hard stop at 100%, user sign-off at `warn_at_percent` (80%).
- The user is notified once per task run, when it finishes.
- A task's mode is decided once; moving to the next mode is the user's call ("next").
