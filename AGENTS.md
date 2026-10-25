# AGENTS.md: rules for agents working on and inside Foreman

## Working on this codebase

- Python ≥ 3.9, one dependency (`pyyaml`). Tests: `python3 -m unittest discover tests`; lint: `ruff check .` (both run in CI; must stay green and fast;
  use `tests/helpers.py` workspaces and the scripted `MockProvider`, never real CLIs, in unit tests).
- Keep the split described in `README_ARCHITECTURE.md`: deterministic state/safety in `registry.py`,
  `store.py`, `engine.py`; AI prompts and schemas only in `brain.py`; CLI specifics only in `providers/`.
- Interfaces (`cli.py`, `mcp_server.py`) stay thin. New behaviour goes into the engine so every front end
  gets it.
- Don't add UI layers (visual dashboards, voice, custom line editors) unless the user asks. Visible
  terminal windows are supported only through `terminal.py`.
- Verify provider behaviour against the installed CLI before changing an adapter, and record what was verified
  in the adapter's docstring.

## Rules Foreman gives worker sessions (enforced by the engine)

1. Edit only inside the step's project; related projects are read-only context.
2. Never commit, push, reset, stash, clean or switch branches. HEAD movement is reported as a violation.
   The one git action Foreman itself takes is `git init` (no commit), only when the user accepts a suggestion for it.
3. Investigate steps are read-only; any file change is reported.
4. Don't invent business rules: return a `business` question instead.
5. Report discovered cross-project dependencies with evidence; they become `learned` relations.
6. Fix small related problems in place; only major, out-of-scope items become suggestions to the user.
7. Follow the mode: R&D, planning and deployment are read-only; QA may change test files only (other changes are
   reported); web research only in R&D and planning. Deployment never runs deploy, publish, migration or
   infrastructure commands; it prepares and verifies, and the user deploys.
8. Follow the confirmed decisions in the project's `.foreman/decisions.md`. Only Foreman writes to
   `.foreman/`; workers never do.
9. Before code changes that are risky or touch something with known consumers, an impact investigation runs first.

## Limits (config.yaml → `limits`)

- Concurrency `max_parallel` (default 4); one running step per project at a time.
- Step timeout `step_timeout_sec` (default 1 h; a planner can ask for more per step with `expected_minutes`, about
  1.5x). A step that runs out of time resumes the same session (same provider) up to `timeout_retries` times with
  double the time each time, capped by `step_timeout_max_sec`; it never starts over in a fresh session for a
  timeout. Validation command timeout `check_timeout_sec`.
- `max_fix_rounds` (default 3) review/check bounces, then escalate as Remaining.
- `max_replans` (default 2) plan expansions after discoveries.
- Daily runs per provider: hard stop at 100%, user sign-off at `warn_at_percent` (80%).
- The user is notified once per task run, when it finishes.
- A task's mode is decided once; moving to the next mode is the user's call ("next").
