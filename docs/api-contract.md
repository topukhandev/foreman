# Foreman application API, v1.0 (draft for approval)

Status: **approved and implemented, 2026-10-28** (`api.py` operations, `feed.py` events, `rpc_server.py` for
`foreman serve --stdio`; tests in `tests/test_api.py`). The desktop app uses it. The MCP server and the CLI's `--json`
output still call the engine directly; moving them onto `api.py` comes later. See `desktop-ui-plan.md` for the
decisions behind it.

## Transport
- JSON-RPC 2.0 over the stdio of `foreman serve --stdio`, which the desktop app starts as a sidecar: one JSON
  object per line, UTF-8, read and written as bytes. No network port.
- Requests may overlap (ids). The server sends **notifications** for events (no id).
- **No method waits for AI work.** Anything that plans or runs starts a detached task runner and returns a task id
  at once; progress arrives as events. Every method answers within about a second.
- Stdout carries the protocol only; logs go to stderr.

## Handshake
`system.hello {client: "desktop/0.1", api_version: "1.0"}` →
`{api_version: "1.0", core_version, home, platform, actor: "desktop", providers: [{name, available, info}]}`.
A client must call it first. A different major `api_version` is refused (`invalid`).

## Errors
JSON-RPC error objects with `data.kind`:

| kind | meaning | client does |
|---|---|---|
| `not_found` | no such task, project or suggestion | refresh the list |
| `ambiguous` | a reference matches several (`data.candidates`) | ask which one |
| `running` | the task is running; changes wait until it ends | show "after the current steps", keep drafts |
| `stale` | the question is closed, or the plan changed since it was shown (`data.task`: fresh snapshot) | show the current plan again |
| `invalid` | bad parameters, or a version mismatch | fix the call |
| `internal` | anything else (message only; details in Foreman's log) | show it |

## Data shapes (all ids are strings)

- **TaskSummary:** `id, title, status, rev, running, mode, intent, feature, cwd, created_at, updated_at, dry_run,
  projects {name: role}, steps [{id, project, kind, status, provider, summary}], checks [{project, command, ok}],
  questions [Question] (open only), remaining [], next, answer, next_for_you [], verification {level, gaps},
  acceptance [{id, text, status, level, evidence}]`.
  - This is `report.task_summary` plus `rev`, `running`, the timestamps and full questions.
- **Question:** `id ("q3"), kind (business | technical | approval | budget | dependency), text, header, options [],
  descriptions [], default, plan_fp (approvals), from_step, project`.
- **TaskDetail:** `{summary: TaskSummary, record: <task.json, redacted>, report_md}`.
- **Event:** `{task_id, cursor, at, ts, event, ...}`, one line of `events.jsonl` as written, plus its cursor.
- **Project:** `{name, path, stack [], test, source, relations [{type, other, evidence, fresh}]}`.

## Methods

**Tasks**

| method | params | result |
|---|---|---|
| `tasks.list` | `{status?, project?, limit=50}` | `{tasks: [TaskSummary], unreadable: [id]}` |
| `tasks.get` | `{task_id}` | `TaskDetail` (reconciles a dead runner first) |
| `tasks.submit` | `{request, cwd, mode?, dry_run?, confirm_plan?, idempotency_key}` | `{task_id}`, or `{reply}` for instant answers (status, small talk) |
| `tasks.answer` | `{task_id, answers {qid: text}, plan_fp?, idempotency_key}` | `{task_id, queued, message}` |
| `tasks.resume` | `{task_id, note?}` | `{task_id}` |
| `tasks.next` | `{cwd}` | `{task_id}` or `{reply}` |
| `tasks.stop` | `{task_id}` | `{requested}` (the runner ends the run as `interrupted`) |
| `tasks.close` | `{task_id, note?, cancel?}` | `{task: TaskSummary, message}` |
| `tasks.events` | `{task_id, cursor=0, limit=1000}` | `{events: [Event], cursor, resync}` |

**Projects and sessions**

| method | params | result |
|---|---|---|
| `projects.list` | `{}` | `{projects: [Project]}` |
| `projects.get` | `{name}` | `Project` |
| `projects.scan` | `{}` | `{projects: [name]}` |
| `lifecycle.get` | `{cwd}` | `engine.lifecycle_data` |
| `sessions.list` | `{project}` | `{sessions: [{provider, session_id, updated_at, title}]}` |

**Suggestions, settings, system**

| method | params | result |
|---|---|---|
| `suggestions.list` | `{status="open"}` | `{suggestions: [...]}` |
| `suggestions.accept` | `{id, cwd}` | `{task_id}` or `{reply}` |
| `suggestions.dismiss` | `{id}` | `{}` |
| `config.get` | `{}` | `{display, auto_switch, notify, on_close, planner, providers {name: {enabled, model, effort}}}` |
| `config.set_on_close` | `{value: ask \| keep \| stop}` | `{}` (D3; the only setting the app changes in v1) |
| `system.doctor` | `{}` | `cli.doctor_data` |

Every mutating call (`submit`, `answer`, `resume`, `next`, `stop`, `close`, `accept`, `dismiss`,
`set_on_close`) is recorded with the actor `desktop`. `submit` and `answer` with an `idempotency_key` seen in the
last 24 hours return the first result instead of acting twice.

## Live updates
- `events.subscribe {tasks: [task_id] | "active", since: {task_id: cursor}}` → `{}`. A task given without a cursor
  is followed from the current end of its log (`tasks.events` has its history); "active" means the tasks queued
  or running now, plus every task that starts later, followed from its beginning.
- `events.unsubscribe {}` → `{}`

Notifications from the server:
- `event {task_id, cursor, event}`: in order within a task. The server tails each subscribed task's
  `events.jsonl` (about every 250 ms); "active" also follows tasks that start later.
- `tasks_changed {task_ids}`: a hint to call `tasks.get` (a new task, a status change seen without its event, a
  runner that died).
- `resync {task_id, cursor}`: the client's cursor no longer fits; reload the task and go on from `cursor`.

Client rules:
- `task.json` (`tasks.get`) is the truth, and events are hints.
- After `run_started`, `status`, `question_opened`, `finished`, `resync` or a reconnect, call `tasks.get`, and
  keep the snapshot with the highest `rev`.
- To reconnect, subscribe again with the last cursors.

## Not in v1.0
- **Never in the API:** running checks, marking something verified, editing a plan, approving without `plan_fp`,
  running commands. Those are the engine's alone.
- **Later minor versions:**
  - `tasks.output` (raw live output, B1) in 1.1;
  - model and settings editing;
  - interactive terminals (`sessions.lease`, `terminal.launch_spec`).
- **A different transport:** HTTP/SSE would carry the same methods unchanged.

## Versioning
`api_version` is major.minor. Minor versions only add methods, fields and event kinds, and clients ignore what
they don't know. Removing or changing anything is a new major version. This is separate from the worker result
contract (`brain.PROTOCOL_VERSION`) and from the record schema.
