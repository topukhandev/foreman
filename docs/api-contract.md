# Foreman application API, v1.5

Every method is offered by both the CLI and the desktop app, or `src/orchestrator/surface.py` says why not
(AGENTS.md, "One feature, every front end"; enforced by `tests/test_protocol.py`).

Status: **approved and implemented, 2026-10-28** (`api.py` operations, `feed.py` events, `rpc_server.py` for
`foreman serve --stdio`; tests in `tests/test_api.py`). The desktop app uses it, and so do the MCP server and the
CLI's `--json` output (they also use a few Python-only helpers in `api.py` for task references in words, free-text
answers and waiting for a task; those are not JSON-RPC methods). See `desktop-ui-plan.md` for the decisions behind
it.

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
  - This is `report.task_summary` plus `rev`, `running`, the timestamps, full questions, (1.4) `project`: the
    project the task's folder belongs to, and (1.5) `notices`: what Foreman wants the user to know about this run
    (a provider skipped and why, a check that couldn't run), as the terminal report's "Notes".
- **Question:** `id ("q3"), kind (business | technical | approval | budget | dependency), text, header, options [],
  descriptions [], default, plan_fp (approvals), from_step, project`.
- **TaskDetail:** `{summary: TaskSummary, record: <task.json, redacted>, report_md}`.
- **Event:** `{task_id, cursor, at, ts, event, ...}`, one line of `events.jsonl` as written, plus its cursor.
- **Project:** `{name, path, stack [], test, source, opened, relations [{type, other, evidence, fresh}]}`. `opened`
  (1.5): epoch seconds when the user last opened it (in any front end, including a request sent from its folder),
  `false` when hidden, `null` when only found by a scan. Front ends list opened projects.

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
| `tasks.outputs` | `{task_id}` | `{outputs: [{name, size, updated}]}`: the live output log of each provider run (1.1) |
| `tasks.output` | `{task_id, name, offset=0, limit=65536}` | `{text, offset, eof}`: whole lines from a byte offset (1.1) |

**Conversations** (1.4): per-project transcripts, shared with the terminal chat (`~/.foreman/chats/<project>/<id>.json`,
`conversations.py`), so a conversation started in one continues in the other.

| method | params | result |
|---|---|---|
| `conversations.list` | `{}` | `{conversations: [{id, project, title, started_at, updated_at, tasks [task_id], messages}]}`, newest first |
| `conversations.get` | `{project, conversation_id}` | `{conversation: {id, project, title, entries [{user, output, at, shown?, task?}], focus, started_at, updated_at}, tasks {task_id: TaskSummary}}` |
| `conversations.send` | `{cwd, text, conversation_id?, mode?, dry_run?, idempotency_key}` | `{conversation_id, project, task_id}` when a task starts or continues, or `{conversation_id, project, reply}` |
| `conversations.rename` | `{project, conversation_id, title}` | `{title}` |

`conversations.send` handles a message the way the terminal chat does: while the conversation's task (`focus`) waits
for answers, the message answers it (unless it asks for a status); otherwise Foreman routes it with the
conversation's last exchanges as context, each task's line brought up to date. Without `conversation_id` it starts
a new conversation in the folder's project. An entry's `task` is the task that message started or answered.

**Projects and sessions**

| method | params | result |
|---|---|---|
| `projects.list` | `{}` | `{projects: [Project]}` |
| `projects.get` | `{name}` | `Project` |
| `projects.scan` | `{}` | `{projects: [name]}` |
| `projects.open` | `{path}` | `Project` (1.5): the user opened this folder (the app's folder picker, `foreman open`); it becomes a project if none holds it, and is listed by every front end |
| `projects.hide` | `{name}` | `Project` (1.5): off the lists until it is opened again; nothing else changes |
| `lifecycle.get` | `{cwd}` | `engine.lifecycle_data` |
| `sessions.list` | `{project}` | `{sessions: [{provider, session_id, updated_at, title}]}` |

**Suggestions, settings, system**

| method | params | result |
|---|---|---|
| `suggestions.list` | `{status="open"}` | `{suggestions: [...]}` |
| `suggestions.accept` | `{id, cwd, conversation_id?}` | `{task_id}` or `{reply}`; with `conversation_id` (1.4) the task joins that conversation |
| `suggestions.dismiss` | `{id}` | `{}` |
| `config.get` | `{}` | `{display, auto_switch, notify, on_close, planner, config_file, settings: [Setting], providers {name: {enabled, available, info, model, effort, levels, light {model, effort}}}}` |
| `config.set` | `{key, value}` | `{setting: Setting}` (1.3): one setting from `settings`; `invalid` for an unknown key, a value not offered, or a provider that isn't installed |
| `config.set_on_close` | `{value: ask \| keep \| stop}` | `{}` (D3; kept for 1.0 clients, the same as `config.set` with `on_close`) |
| `models.list` | `{provider}` | `{provider, available, info, current {model, effort}, models [{id, name, levels, group}], typed_levels}` (1.3): asked of the provider's CLI, so it may take a few seconds |
| `models.set` | `{provider, model, effort?}` | `{provider, model, effort}` (1.3): `model` "default" lets the CLI decide; `effort` must be one of the model's levels (none given: high, or the highest it has) |
| `system.doctor` | `{}` | `cli.doctor_data` |
| `system.stats` | `{since?}` | `{stats, text}` (1.5): totals across tasks, as `foreman stats` shows them |

**Setting** (1.3): `{key, label, kind: toggle | choice, applies: all | chat, value, about?, options? [{value, label,
description, available}]}`. The list is `settings.py`'s, the same one `foreman config` and /config in the chat
show, so the app and the terminal always offer the same settings. `applies: chat` means it only affects the
terminal chat.

Every mutating call (`submit`, `answer`, `resume`, `next`, `stop`, `close`, `accept`, `dismiss`,
`set_on_close`) is recorded with the actor `desktop`. `submit` and `answer` with an `idempotency_key` seen in the
last 24 hours return the first result instead of acting twice.

**Live output (1.1, read-only).** Each provider run's raw output is appended to its log as it arrives. An `output`
event names the log when a run starts.
- **Redacted before it is written:** environment secret values, well-known token formats, and private keys, also
  when a key spans lines. This is best effort: a secret read from a file that looks like nothing known isn't
  recognised.
- **Served as plain text:** terminal escape sequences, other control characters and bidirectional overrides are
  removed. The client renders it as text, never as HTML.
- **No input path:** there is no way to send input to a run.
- **Names come from `tasks.outputs`;** paths are never accepted.

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
- `phase {task_id, text, provider}` (1.2): what a queued or running task is doing now, in a few words ("Implementing
  in admin-ui", "Reading footer.js", "Running checks"), with the provider at work ("" when none). Sent when it
  changes, and once after subscribing for a task already running. The same line the chat shows; display only.

Client rules:
- `task.json` (`tasks.get`) is the truth, and events are hints.
- After `run_started`, `status`, `question_opened`, `finished`, `resync` or a reconnect, call `tasks.get`, and
  keep the snapshot with the highest `rev`.
- To reconnect, subscribe again with the last cursors.

## Not in v1.0
- **Never in the API:** running checks, marking something verified, editing a plan, approving without `plan_fp`,
  running commands. Those are the engine's alone.
- **Later minor versions:**
  - interactive terminals (`sessions.lease`, `terminal.launch_spec`).
- **A different transport:** HTTP/SSE would carry the same methods unchanged.

## Versioning
`api_version` is major.minor. Minor versions only add methods, fields and event kinds, and clients ignore what
they don't know. Removing or changing anything is a new major version. This is separate from the worker result
contract (`brain.PROTOCOL_VERSION`) and from the record schema.
