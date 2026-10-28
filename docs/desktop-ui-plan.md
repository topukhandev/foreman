# Desktop UI and application API plan (v2)

Status: **approved, not implemented.** Written 2026-10-27 against commit `8d94e0a`; decisions D1–D13 (§9) approved
2026-10-28 (D3 changed from the recommendation); review amendments in §10 take precedence. Re-check the cited code first: it may have changed.

Labels: **[C]** confirmed in the repository · **[R]** recommendation · **[A]** assumption, not verified.

## 0. Baseline and changes from v1

**Baseline (kept):**
- Python core, plus a transport-free application service layer (`api.py`).
- JSON-RPC 2.0 over stdio to a sidecar process (`foreman serve --stdio`).
- Detached `run-task` runners, one task per process.
- Tauri 2 shell (Electron as fallback); no daemon, no HTTP server.

The repository supports this baseline. One task per process is a real invariant [C]: `util.STOPPING` is
process-global and never cleared.

**Changed from v1:**
- **Windows moved from the last phase into the pre-UI work.** Platform abstractions and a three-OS CI come first;
  only real-provider checks per OS come later.
- **The run lock is an OS lock held for the whole run.** No O_EXCL lock file, because those go stale when a process
  dies.
- **Finding [C]:** a superseded plan's unanswered approval question stays open. `_apply_plan` clears
  `plan_approved` but not the question, and questions are deduplicated only by text, so a later "yes" answers
  both the old and the new approval.
- **Finding [C], Windows:** `atomic_write` uses `os.replace`, which fails while another process (for example a
  polling UI) has `task.json` open. The failure raises inside the runner's `save`, so the task fails.
- **New `rev` counter on `task.json`.** `updated_at` has one-second resolution, too coarse to order snapshots.
- **PyInstaller rejected for distribution.** `entry_command()` starts runners with `sys.executable -m
  orchestrator.cli` [C]. That works with a relocatable interpreter but breaks in a frozen build, where
  `sys.executable` is the app itself.

**Deliberately not done:**
- No queue for answers given mid-run. Applying them during a run would be new engine behaviour; refusing them
  clearly is enough.
- No per-event sequence numbers. Byte-offset cursors already identify each event.
- Secret redaction by pattern is best-effort and documented as such. The real protection stays `worker_env`, which
  keeps secrets away from workers.
- The terminal chat (`ui.py`/`chat.py`, built on `termios`) is not ported to Windows. Windows users use the CLI
  subcommands and the desktop UI (D10).

## 1. Cross-platform from the start

**[C] POSIX-only code today:**
- the `fcntl` project lock, which silently degrades to locking within one process on Windows;
- `killpg` and `start_new_session`;
- stopping a task via SIGTERM;
- `_pid_alive` using `os.kill(pid, 0)`; on Windows 0 is `CTRL_C_EVENT`, so this is not a liveness check;
- `termios` in `chat.py`, `ui.py` and the menus;
- `notify-send` / `osascript`;
- the 90 KB argv assumption (`providers/base.py` `MAX_PROMPT_BYTES`).

Already safe:
- `terminal.py` returns `None` on win32 and where no display is set, so runs fall back to headless.
- Changed-file paths come from `git status`, which always uses `/`, so the sensitive-path patterns work on Windows.

**[R] Platform module, required before any UI.** Standard library only, so the one-dependency rule holds. Name it
something like `oscompat.py`, not `platform.py`, which would shadow the stdlib module.

| Function | POSIX | Windows |
|---|---|---|
| `FileLock(path).try_acquire()` / `acquire(timeout)` / `release()` | `flock` | `msvcrt.locking` on byte 0 |
| `spawn_detached(cmd, log)` | `start_new_session` | `CREATE_NEW_PROCESS_GROUP \| DETACHED_PROCESS`, child assigned to a Job Object |
| `kill_tree(proc)` | `killpg` TERM, then KILL | Job Object terminate via `ctypes` [A: assignment at spawn time; verify] |
| `pid_alive(pid)` (display only) | `os.kill(pid, 0)` | `OpenProcess` + `GetExitCodeProcess` via `ctypes` |
| `atomic_write` retry | n/a | retry `os.replace` on `PermissionError` with short backoff |
| `max_argv_bytes()` | ~90 KB (unchanged) | ~30 KB; 0 when the target is a `.cmd`/`.bat` shim, so the prompt always goes in a file |
| `notify(title, body)` | argv-safe `notify-send -- …`; `osascript` with `on run argv` | no-op for now (D11) |

**Cooperative stop.**
- The API writes a stop file, `tasks/<id>/stop`.
- A watcher thread in the runner polls for it and calls `kill_all_children()`.
- On POSIX, SIGTERM stays as a faster path.

The watcher sets `STOPPING`, which the engine's main thread already checks between steps. Instead of raising
`Interrupted`, the runner ends the run as `interrupted`. This also fixes Windows, where `os.kill(SIGTERM)`
terminates the process with no cleanup.

**Windows `.cmd` shims [C+R].** On Windows, npm-installed CLIs are batch shims. Passing prompt text as an
argument through `cmd.exe` cannot be escaped safely (the BatBadBut class of bugs). For such targets, the short
prompt stub that points to the prompt file contains only the file path, without the excerpts `fit_prompt`
includes today.

**Before the UI: CI on ubuntu, macos and windows, with MockProvider only.**
- Inherently POSIX-only tests (chat/ui TTY, `terminal.py`) are skipped with a stated reason.
- Everything else passes on all three.
- [A] How much of the suite fails on Windows today hasn't been measured; expect a first pass of fixes.

**Later, before claiming support on an OS:** real-provider checks for each OS and provider:
- binary discovery (`finder.py` has no Windows locations such as `%APPDATA%\npm`);
- session discovery paths (`claude.encode_cwd` with drive letters);
- auth environment variables;
- parsing of limit and error messages;
- read-only flags;
- tree-kill of real `node` child processes.

Record each result in the adapter's docstring.

## 2. Concurrency and state integrity

**Run lock [R]:**
- **File:** `tasks/<id>/run.lock`, a `FileLock` that is never deleted. Holder details go in a separate `run.json`
  (`pid`, `started_at`, `run`), because a Windows byte lock blocks other processes from reading the locked file.
- **Runner:** non-blocking `try_acquire`. On failure the runner exits with no writes at all. On success it
  re-reads `task.json`. Today `run-task` loads the task before `execute` [C], so a change made between spawn and
  lock acquisition would be overwritten. The lock is held from planning through finalize and released by the OS
  on exit or crash, so a stale lock can't exist.
- **Short mutations** (`prepare_answer`, `prepare_resume`, `close`, `approve`, `accept`): `try_acquire`, then
  read, change, write, then release. If the lock is held they return a `running` error and change nothing. They
  spawn the runner only after releasing the lock. The in-process CLI `ask` path uses the same lock.
- **"Stale lock" recovery is really state recovery.** If the status is `queued`, `planning` or `running` and
  `try_acquire` succeeds, no runner is alive. The reconciler marks the task `interrupted` (unfinished attempts are
  already closed this way in `execute`) and writes a `status` event with `reason: runner_exited`. It does this
  while holding the lock, so two reconcilers can't race. This replaces the pid check in `status()`; the pid is
  no longer used for decisions.
- **[A]** The Foreman home is on a local disk. `flock` is unreliable on NFS; document this.

**Shared JSON files** (`suggestions.json`, `health.json`, `budget.json`): read, change and write under a blocking
`FileLock` per file with a short timeout [R]. Today they are protected only by thread locks [C].

**Behaviour while a task is running [R]:**

| Operation | Behaviour |
|---|---|
| Read (get, list, events, output, report) | allowed |
| `stop` | allowed: stop file, plus a signal on POSIX |
| answer / approve / resume / close / next / accept on the same task | refused with `running`; nothing changes |
| submit a different task | allowed; the existing project claims make it wait or block |
| question raised mid-run by a step | appears in the snapshot and as a `question_opened` event with `answerable: false` until the run ends; the UI shows "answerable when the current steps finish" (the engine already applies answers only between runs [C]) |
| config change | allowed; a running runner keeps the config it started with |

**Questions and approvals [R]:**
- **Ids.** Every question gets `id: "q<N>"`. Questions are append-only and never removed [C], so ids are stable.
- **Fingerprint.** Approval questions store `plan_fp`: a SHA-256 of the canonical JSON of the pending implement
  steps (id, project, kind, instructions), plus `impact.needs_approval` and `impact.summary`, plus the checks list.
  The snapshot and the report show it.
- **`tasks.approve(task, qid, decision, plan_fp)`**, under the lock: the question must exist, still be open, be of
  kind `approval`, and `plan_fp` must equal the current plan's fingerprint. Otherwise it returns `stale` with a
  fresh snapshot.
- **Re-plan.** Any new plan or re-plan that changes the pending steps marks open approval questions `superseded`
  (closed, no answer). This fixes the finding in §0.
- **Budget and dependency questions** are answered by `qid`. Dependency questions already carry their `deps`
  list.
- **The free-text path stays for chat and MCP.** It applies to the questions open while it holds the lock.
  Superseded approvals are closed, so a "yes" can only reach the current one. Accepted, documented risk: an MCP
  user replying to an older report.

**Minimum regression tests before exposing the API to more than one client:**

| # | Scenario | Expected result |
|---|---|---|
| T1 | Two processes `execute` the same task | exactly one runs; the other makes no writes |
| T2 | Runner hard-killed (SIGKILL / TerminateProcess) | lock free; reconciler sets `interrupted` with an event; resume continues the same sessions |
| T3 | answer / resume / close / approve while running | `running`; `task.json` byte-identical |
| T4 | Approve with a stale `plan_fp` | `stale` |
| T4 | Approve with the correct fingerprint | proceeds |
| T4 | Re-plan | the old approval is superseded |
| T5 | Two concurrent text answers | one applies; the other gets `running` or "no open questions" |
| T6 | Change between spawn and lock acquisition | the change survives (runner re-reads) |
| T7 | Stop file | `interrupted`, child process tree gone, on all three OSes |
| T8 | Concurrent `add_suggestions` from two processes | unique ids, nothing lost |
| T9 | Event log left with a partial last line by a crash | the next event isn't merged into it |
| T10 | `notify` with quotes and AppleScript in the body | passed literally |
| T11 | Windows: a reader holds `task.json` open during `save` | the save succeeds after a retry |

## 3. Event feed and recovery

**Authority [R]:** `task.json` is the authoritative state. `events.jsonl` is the audit trail and a source of hints.
Clients derive everything from snapshots and use events only to know when to re-fetch.

**Writer rules [R]:**
- Encode each event as one complete line of bytes and write it with a single `os.write` on an `O_APPEND` file
  descriptor. Today a buffered text-mode write is used [C].
- On opening the log, if its last byte isn't `\n` (left by a crash), write `\n` first.
- Add events: `status` (on every status change), `question_opened`, `notice`, `output` (pointer to a live log).
- `Store.save` increments `rev`; the runner is the only writer during a run.

**Reader rules [R]:**
- Consume only up to the last `\n`. The cursor is the byte offset after the last complete line, so a partial
  line is re-read on the next poll.
- A complete line that doesn't parse is skipped and counted. Chat already skips such lines [C].
- If the cursor is beyond the file size, or the task has disappeared, return `resync`: the client re-fetches the
  snapshot and continues from the current end of the log.
- Replay is paged (for example 1,000 events per page).

**Crashes:**
- No `finished` event appears.
- The feed notices "status is running but the lock is free" and calls the engine's reconciler, which persists the
  change and writes an event.
- The feed never invents events itself. To clients it only sends `tasks_changed`, meaning "re-fetch".

**Reconnect:** the client sends its cursor for each task, the server replays from there, and the client
re-fetches the snapshots of active tasks.

**Delayed or missing events:**
- Some state changes have no event (health, budget, older code paths).
- Clients re-fetch on `run_started`, `finished`, `status`, `question_opened`, `resync`, on reconnect, and whenever
  a cheap poll finds a newer `task.json` mtime or `rev` for an active task.
- A snapshot with a lower `rev` than one the client already has is discarded.
- Order is guaranteed within one task only.

## 4. Live output security

**[C] Today:**
- Raw provider output reaches `logs/` only after an attempt ends, passing through `Store.clean`. That redacts the
  values of secret-looking environment variables of the runner process, both as written and JSON-escaped.
- Provider stdin is `DEVNULL`, so there is no input path into a worker.
- A live log exposes the same kind of data, just earlier and in pieces.

**Policy [R]:**
1. **Redact before writing, in the runner.** Nothing unredacted reaches the disk, and the API serves only stored
   files, so no client can see raw output.
2. **Unit of redaction: complete lines.** `run_streaming` already iterates line by line [C].
   - A line longer than a cap (for example 256 KB) is flushed in segments.
   - Each segment keeps a carry-over tail of `max_secret_len − 1` characters, so a secret split across a segment
     boundary still matches.
   - Multi-line PEM private-key blocks are handled with a small state machine (BEGIN to END) across lines.
3. **Second, best-effort layer:** patterns for well-known token formats (`sk-ant-`, `ghp_`/`github_pat_`,
   `AKIA…`, `xox[bp]-`, PEM).
4. **Limitations (to be stated in user documentation):**
   - Matching environment-variable values can't catch secrets the worker reads from files (`.env`, cloud
     credential files) or secrets that aren't in the runner's environment.
   - Encoded forms (base64, URL-encoding, hex) and secrets reformatted or wrapped by tools are missed.
   - Redaction reduces accidental exposure; it is not a guarantee. The real protection remains `worker_env`.
5. **Storage:** create the Foreman home as `0700` on POSIX. Today nothing sets permissions except the project lock
   files (`0600`) [C]. [A] Windows profile folders are private by default. Retention stays as it is (deferred).
6. **Read-only UI:**
   - `tasks.output(task, attempt_ref, offset, limit)` resolves the file from the attempt records in `task.json`;
     it never takes a path from the client.
   - Output renders as text or ANSI in xterm.js, never as HTML.
   - The default view is the existing summarised `activity` events; raw output is an opt-in toggle.
   - No API call writes to a worker.

## 5. Desktop packaging (no implementation yet)

**Development setup (v1 requirement):**
- Install Foreman: `pip install -e .`, or use the `./foreman` script.
- The Tauri dev shell finds it through a `FOREMAN_BIN` environment variable, falling back to PATH, and spawns
  `foreman serve --stdio`.
- Requires Python ≥ 3.9.

**Future distribution [R]:**
- Bundle `python-build-standalone` plus the Foreman wheel plus `pyyaml` inside the app.
- The sidecar runs as `<bundled>/python -I -m orchestrator.cli serve --stdio`.
- Runners keep working unchanged through `entry_command()` [C]; this is why PyInstaller is ruled out.

**Must stay true from now on, so bundling remains possible:**
- The core never assumes `foreman` is on PATH (true today [C]).
- Resource files stay package data (`modes/*.md` and `roles/*.md` already are [C]).
- Records carry a schema version. Today `task.json` and `config.yaml` have none [C]. Add `schema` and have
  `system.hello` report versions, because a bundled app and a separately installed CLI share `~/.foreman`. Rule: a
  core never rewrites a record written with a newer schema than it knows.
- An app launched from the desktop doesn't load the user's shell profile. At startup the sidecar reads PATH and
  the environment from a login shell on macOS and Linux (D12). [A] On Windows, desktop apps receive the user's
  environment variables normally.

**Deferred:** code signing (macOS notarisation including the embedded `.so` files, Windows Authenticode),
AppImage/deb/msi/dmg packages, auto-update, a CLI shim installed by the app.

## 6. Terminal scope

**First UI:** the live output viewer from §4, read-only.

**Later (interactive PTY); the boundary is defined now:**
- **Ownership.** A provider session is owned by either a running step attempt (an open attempt in `history` while
  the run lock is held) or one interactive lease, never both.
- **`sessions.lease(provider, session_id)`.** The core refuses if any running attempt uses that session or if a
  running task holds the project's write claim. The sidecar holds the lease as a `FileLock`
  (`locks/session-<provider>-<id>.lock`) for as long as the PTY lives. If the sidecar dies, the lease is released
  and the shell kills the PTY.
- **Engine side.** When choosing a session, the engine skips leased sessions. A project claim and a lease on that
  project exclude each other.
- **`terminal.launch_spec(lease)`.** The core returns `{argv, cwd, env}`, built by the adapter in interactive mode
  with `worker_env` filtering applied. The shell (Rust `portable-pty`) only spawns the process and moves bytes to
  and from xterm.js. Terminal bytes never travel over JSON-RPC.
- **Attribution.** Edits made in an interactive session are the user's, not Foreman's. The existing "changed since
  last run" detection flags them on the next resume [C].
- **Platform.** [A] ConPTY requires Windows 10 1809 or later.
- **CLI equivalent.** `foreman attach <session>` runs the same launch spec in the user's own terminal.

## 7. macOS notification injection

**[C] Issue:** `report.notify` places the AI-written task summary unescaped inside an AppleScript string for
`osascript -e`. A `"` breaks the string, and crafted text can inject AppleScript (`do shell script`).

**[R] Fix**, in step A6 before any UI:
- Pass title and body as separate arguments to an `on run argv` script, so nothing is interpolated.
- For `notify-send`, add `--` before the text so text starting with `-` isn't read as an option.
- Cover it with test T10.

## 8. Roadmap

**A. Required before the API is exposed to more than one client**

| Step | Scope | Acceptance |
|---|---|---|
| A1 | Platform module (§1) and CI on 3 OSes with MockProvider | suite green on all three; POSIX-only tests skipped with stated reasons |
| A2 | Run lock, reconciler, refusal while running, runner re-reads after acquiring | T1–T3, T5–T7, T11 |
| A3 | Question ids, `plan_fp`, superseded approvals, structured answers | T4 |
| A4 | Event writer and reader rules, new events, `rev` | T9 plus cursor replay tests (no gaps or duplicates under a concurrent writer) |
| A5 | Locks on the three shared JSON files | T8 |
| A6 | `notify` fix; home created `0700` | T10 |
| A7 | `api.py` and `feed.py`; MCP and `--json` CLI paths moved onto them | existing tests unchanged; contract tests pin data-object fields and error codes |
| A8 | `foreman serve --stdio` (framing shared with MCP), `system.hello` with versions | scripted protocol test: submit → events → question → answer → stop → resume |

**B. Required before the first usable desktop UI**
- **B1:** live output logs with the §4 redaction policy, and `tasks.output`. Tests: secret split across segments,
  PEM block, JSON-escaped secret.
- **B2:** Windows prompt-passing safety (path-only prompt stub for `.cmd` shims, argv limit per OS).
- **B3:** schema versions on records.
- **B4:** login-shell environment resolution in the sidecar.
- **B5:** minimal Tauri shell:
  - projects and tasks views, task detail, live progress, questions and approvals, stop/resume/close,
    suggestions, read-only settings;
  - sanitised Markdown with a strict CSP and minimal Tauri capabilities;
  - closing the window with running tasks asks "keep running or stop all" (D3), with a "remember my choice" option
    and the same preference in settings.
- **B6:** developer setup docs.

**C. Required before claiming Windows/macOS support**
- **C1:** real-provider checks for each OS and provider (§1), recorded in adapter docstrings.
- **C2:** desktop notifications on Windows (D11).
- **C3:** CLI chat on Windows: a clear "unsupported" message, or a port (D10).
- **C4:** Job Object tree-kill verified against real provider process trees.
- **C5:** packaged-app smoke test on each OS, if distributing by then.

**D. Safe to defer**
- Interactive PTY with leases.
- HTTP/SSE server, a daemon, remote or multi-user access.
- Bundled runtime, signing, auto-update.
- Editing settings from the UI.
- A queue for answers given mid-run.
- Log retention.
- File watching (polling is enough).

## 9. Decisions (approved 2026-10-28)

| # | Decision | Approved choice |
|---|---|---|
| D1 | Overall architecture | Python core + `api.py` + stdio JSON-RPC sidecar; no daemon or HTTP |
| D2 | Desktop shell | Tauri 2 (Electron if xterm.js on WebKitGTK proves unusable) |
| D3 | Closing the window | **ask every time** ("keep running" or "stop all") by default, with a saved preference: `ui.on_close: ask \| keep \| stop` (default `ask`), set from the close dialog ("remember my choice") or settings; also listed in `menus.config_menu` per AGENTS.md. The core's behaviour is unchanged: tasks are detached, and "stop all" sends `tasks.stop` for each running task |
| D4 | Mutations while a task runs | refused (`running`); no mid-run answer queue |
| D5 | Run lock | OS lock held for the whole run; pid informational only; cooperative stop file |
| D6 | Approvals | question id + plan fingerprint; superseded on re-plan; free-text path kept for chat/MCP |
| D7 | State authority | `task.json` authoritative with a `rev` counter; events are hints; byte-offset cursors |
| D8 | Live output | redacted at write time, best-effort and documented; activity view by default, raw opt-in |
| D9 | Packaging | installed CLI for development; `python-build-standalone` for distribution; no PyInstaller |
| D10 | Terminal chat on Windows | not ported in this effort; prints a clear message |
| D11 | Windows notifications | no-op in the core for now; the desktop app notifies while it is open |
| D12 | GUI environment | resolve PATH and environment from a login shell on macOS and Linux |
| D13 | Foreman home | on a local disk (no NFS); created `0700` |

D3 needs one narrow write operation in B5: `config.set("ui.on_close", value)` only. General settings editing in the
UI stays deferred.

## 10. Review amendments (2026-10-28)

An independent reviewer session reviewed this plan in two rounds against the code. These amendments take
precedence over the earlier sections where they differ. The full discussion is in
`~/Desktop/README-Foreman-Desktop-UI.md`.

**Process handoff and sidecar**
- **Run-request token.** A mutation writes a one-time `run_request` token into `task.json` and passes it to the
  runner.
  - The runner acquires the run lock with a blocking wait of about 10 s, proceeds only if the status is `queued`
    and the token matches, then consumes the token. Otherwise it exits without writing.
  - The reconciler gives `queued` tasks a grace period before marking them interrupted.
  - The stop file carries the token of the run it targets.
- **The sidecar never runs AI or provider work.** Dry runs and `/btw` become runner jobs.
  - Requests are handled concurrently, with JSON-RPC ids and cancellation.
  - At startup, fd 1 is redirected (`dup2`) so a stray `print` can't corrupt the protocol.
- **`STOPPING` blocks only new workers and checks**, not Foreman's own git snapshots (today `_finalize`'s
  `snapshot()` is refused after a stop). SIGTERM behaves exactly like the stop-file watcher.
- **Version skew.** During the transition, new runners also write the legacy `tasks/<id>/lock` pid file. Rewrites
  of shared files preserve unknown fields.
- **Idempotency keys** on submit and answer. **Actor** (`cli`, `chat`, `mcp`, `desktop`) recorded on every
  mutation. UTC timestamps for new records.

**Security**
- **One rendering component** for all AI text (markdown-it `html:false` + DOMPurify), with a strict CSP, http(s)
  links only after a confirmation, a fixed Tauri command list, no devtools in release, and `FOREMAN_BIN` honoured in
  dev builds only. An XSS fixture suite covers `<img onerror>`, `javascript:` and `file:` links, SVG, raw HTML and
  bidi overrides.
- **Native confirmation** for high-impact and dependency approvals only, built from deterministic plan data.
  `plan_fp` is a consistency check, not authorisation.
- **Prompt files** move from `/tmp/foreman-prompts` into the Foreman home. The home is chmod'ed 0700 at startup,
  which also fixes existing installs.
- **Live output** strips ANSI codes, C0 control characters (except `\n` and `\t`) and bidi overrides, and caps the
  number of lines the view keeps.

**Reliability**
- fsync on `atomic_write`, plus the directory on POSIX. Corrupt tasks are listed as unreadable.
- **Global concurrency slots** (`locks/slot-k.lock`), before the first UI:
  - a slot is taken per CLI run;
  - it is released while a step waits for a usage limit or a project claim;
  - time waiting for a slot doesn't count toward the step timeout, and the wait is shown as a notice.
- **Invariants:** only the run-lock holder appends a task's events; `events.jsonl` is never truncated or replaced.
- **Login-shell environment resolution (D12)** has a timeout and marker lines.

**Windows and CI**
- **Job Objects.** The runner assigns itself to a kill-on-close Job Object at startup. Per-worker kill uses
  `%SystemRoot%\System32\taskkill.exe /T /F`. Use `CREATE_NO_WINDOW`, not `DETACHED_PROCESS`.
- **UTF-8.** Subprocess output is decoded as UTF-8; spawned Pythons get `-X utf8`; JSON-RPC uses binary UTF-8
  framing.
- **`.cmd` shims.** Prefer native `.exe` binaries. For `.cmd`, refuse arguments containing `& | < > ^ % !`, quotes
  or control characters, with an error naming the fix. Never use `.ps1`.
- **Paths.** Fix `/` and case assumptions (`registry.py` `project_for_path`, `engine._activity`).
- **CI.** Blocking on all three OSes from A1, with one guarded skip list that can't grow. A fake-CLI subprocess
  provider (spawns a grandchild, prints non-ASCII text, has a `.cmd` wrapper on Windows) is never skipped.

**First UI**
- **Layout:** an attention inbox as the home view; task list; task detail; stop/resume/close.
- **Stack:** Svelte 5 + Vite. No xterm.js in v1; raw output is a plain virtualised text view.
- **Supervision rules:**
  - deterministic data before AI prose;
  - green only for `tool_verified`;
  - "worker says" kept apart from "Foreman verified";
  - drafts kept while a question isn't answerable yet;
  - keyboard-first.

**Deferred**
- The PTY (only the ownership rule is kept) and `foreman attach`.
- A task index (when `tasks.list` takes over ~100 ms or there are more than ~300 tasks).
- Log epoch and retention; `task.json.prev`; SQLite.
- Draft answers applied at the end of a run, after the first UI.

## 11. Progress (2026-10-28)

Implemented and tested (Linux; 363 tests, ruff clean; not committed):
- **A6:**
  - Notifications pass AI-written text as arguments only (`osascript` with `on run argv`, `notify-send --`).
  - Prompts too long for a command line go to a private per-run folder under the task (`tasks/<id>/prompts`),
    or `<home>/prompts` when there is no task, instead of `/tmp`.
  - The home is made 0700 at startup, which also fixes existing installs. Others can't enter the folder, so
    files inside it keep their permissions.
- **A1:**
  - `oscompat.py`: FileLock (flock / msvcrt), pid liveness, process groups with `CREATE_NO_WINDOW`, detached
    spawn, tree kill (killpg / `taskkill /T /F`), a kill-on-close Job Object joined by the runner, `replace_file`
    with a Windows retry, and `-X utf8` for runners.
  - UTF-8 decoding of CLI output.
  - Tests: `tests/fake_cli.py` (real subprocess), `tests/test_platform.py`, `tests/platform_skips.py` (guarded
    skip list).
  - CI matrix on Linux, macOS and Windows.
- **A2:**
  - Run lock `tasks/<id>/run.lock` held for the whole run (`execute` and `plan_only`); the record is re-read
    under the lock.
  - One-time run token: `run-task --run <token>`.
  - Changes (answer, resume, close) refused while a task runs.
  - The reconciler marks a dead runner's task interrupted, with a grace period for queued tasks.
  - Stop requests through a file (`Engine.request_stop`); SIGTERM no longer raises mid-write.
  - `STOPPING` no longer blocks Foreman's own git reads, and the scheduler starts nothing after a stop.
  - The legacy pid file is still written for older CLIs.
  - Tests: `tests/test_runlock.py`.
- **A3:**
  - Question ids (`q<N>`) and `plan_fp` on approval questions; a changed plan supersedes open approvals.
  - `Engine.prepare_answers(task, {qid: answer}, plan_fp)` refuses stale answers.
  - `Engine.actor` (cli, chat, mcp, desktop) recorded on created, answered, resumed, closed, cancelled and
    stop_requested events.
  - Tests: `tests/test_approvals.py`.

- **A5 + A4** (2026-10-28):
  - `util.update_json` (read, change, write under a cross-process FileLock) for `suggestions.json`, `health.json`
    and `budget.json`.
  - Events are written as one line in a single write; a crash's partial line is closed off first.
  - `Store.read_events(task, cursor, limit)` reads complete lines only from a byte cursor, puts `cursor` on each
    event, and returns resync for a cursor that doesn't fit; unparseable lines are skipped.
  - `rev` on `task.json`. `Store.save` writes `status` (with `previous` and `reason`), `question_opened` and
    `notice` events.
  - An epoch `ts` on every event.
  - `Store.unreadable()`, shown by `foreman tasks`.
  - `stop_requested` is logged by the runner itself (only the run-lock holder appends during a run).
  - Tests: `tests/test_state.py`; 371 tests in total.

- **Contract, A7 and A8** (2026-10-28):
  - `docs/api-contract.md` approved.
  - `api.py`: all v1.0 operations, error kinds, idempotency keys, the `desktop` actor. Dry runs also go to a
    runner (`run-task --plan-only`), so the API never waits for AI work.
  - `feed.py`: follows event logs by cursor, announces new tasks, resyncs, and reconciles dead runners.
  - `rpc_server.py` / `foreman serve --stdio`: JSON-RPC as UTF-8 bytes, concurrent requests, `system.hello` first,
    stdout protected.
  - `ui.on_close` setting (D3), also in `foreman config`.
  - 391 tests.
  - Not done yet: moving MCP and `--json` onto `api.py`; moving the chat's status-line logic (`Phase`) for reuse.

Deliberate deviations and open points:
- **UTC:** `now_iso()` stays local time, because `metrics.py` and `chat.py` subtract parsed timestamps, and mixing
  old naive values with new UTC-aware ones would raise. Events get an epoch `ts` instead, and `rev` orders
  snapshots.
- **No fsync in `atomic_write`.** Measured at 6–12 ms per write (it doubled the test suite's time). The atomic
  rename already protects against process crashes; power loss is handled by listing unreadable tasks (A5).
- **Idempotency keys** move to A7, the API layer. Duplicate *runs* are already prevented by the run token.
- **CI on all three OSes is green** (2026-10-28, branch `desktop-core-hardening`): lint, Linux 3.9–3.13, macOS 3.13,
  Windows 3.9 and 3.13. The first runs found real Windows bugs, now fixed: programs found through PATHEXT (`npm`
  is `npm.cmd`), batch-file arguments refused when `cmd.exe` would misread them, file URIs for agy, separator-
  and case-aware paths, the home path, durations on a coarse clock, chat history order. Only `test_paste` is
  skipped (on Windows, D10). Real-provider checks on Windows and macOS are still to do (C1).
- **CI time:** Windows went from 15–20 minutes to about 5 (Linux 1–2, macOS 2). Two changes made the difference:
  HEAD is read from the `.git` files instead of a `git rev-parse` process (5,400 per suite run, several per prompt;
  git is still asked for anything unusual), and test workspaces copy three template projects. Processes per suite
  run: about 10,500 down to 3,100. The largest remaining cost is 107 `npm test` runs.

## 12. Implementation sequence (revised)

1. **A6** security quick fixes: notification injection, prompt files out of `/tmp`, home 0700 at startup.
2. **A1** platform module (file lock, detached spawn, Job Object, taskkill, liveness, fsync'd `atomic_write` with
   Windows retry, UTF-8), plus 3-OS CI, the fake-CLI provider and the guarded skip list.
3. **A2** run lock: OS lock, run-request token, reconciler with grace, legacy lock file, refusal while running,
   narrowed `STOPPING`, SIGTERM equals stop watcher, idempotency keys.
4. **A3** question ids, `plan_fp`, superseded approvals, structured answers, actor on mutations.
5. **A5 + A4** shared-file locks; event writer and reader rules, new events, `rev`, UTC, corrupt tasks listed.
6. **Contract freeze:** JSON-RPC schema plus recorded transcripts. The UI starts in parallel against a fake sidecar.
7. **A7** `api.py` / `feed.py`, with MCP and the `--json` paths moved onto them.
8. **A8** `serve --stdio`.
9. Global concurrency slots; B1–B4.
10. Tauri UI (B5–B6).
11. C1–C5 per-OS support, then deferred items as needed.

Each step keeps the test suite and `ruff` green. No commits are made by the implementation work.
