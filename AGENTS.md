# AGENTS.md: rules for agents working on and inside Foreman

## Working on this codebase

- Python ≥ 3.9, one dependency (`pyyaml`). Tests: `python3 -m unittest discover tests` (must stay green and fast;
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
3. Investigate steps are read-only; any file change is reported.
4. Don't invent business rules: return a `business` question instead.
5. Report discovered cross-project dependencies with evidence; they become `learned` relations.
6. Fix small related problems in place; only major, out-of-scope items become suggestions to the user.

## Limits (config.yaml → `limits`)

- Concurrency `max_parallel` (default 4); one running step per project at a time.
- Step timeout `step_timeout_sec`; validation command timeout `check_timeout_sec`.
- `max_fix_rounds` (default 3) review/check bounces, then escalate as Remaining.
- `max_replans` (default 2) plan expansions after discoveries.
- Daily runs per provider: hard stop at 100%, user sign-off at `warn_at_percent` (80%).
- The user is notified once per task run, when it finishes.
