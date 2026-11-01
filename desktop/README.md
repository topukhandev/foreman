# Foreman desktop app

A window over Foreman, organised like a chat app. On the left are the projects you opened, with their conversations
(the same ones `foreman -r` resumes in a terminal), and what needs you. New conversation shows the folder picker to
open a project, and `foreman open` in a terminal opens one in the same way. In the middle is the conversation, with the message
box at the bottom. Each task appears where it was started: live progress while it runs, its questions one at a time,
and its result when done. Settings are the same as `foreman config` (models and effort included), plus light or
dark mode. It is a Tauri 2 shell with a Svelte 5 page. The app starts
`foreman serve --stdio` and talks to it through the JSON-RPC API in `docs/api-contract.md`. Tasks run in Foreman's
own detached processes, so they keep running when the app closes, unless you choose "stop all".

## Set up (development)

1. **Foreman itself.** From the repository root, run `pip install -e .`, so that `foreman` is on your PATH or in
   `~/.local/bin`. A development build can use a checkout instead: `FOREMAN_BIN=/path/to/foreman/foreman`.
2. **Node 20+ and Rust** (stable, through rustup).
3. **System libraries for Tauri:**
   - Debian/Ubuntu: `sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev`
   - Fedora: `sudo dnf install webkit2gtk4.1-devel openssl-devel curl wget file libappindicator-gtk3-devel librsvg2-devel libxdo-devel`, plus `sudo dnf group install "c-development"`
   - macOS: Xcode Command Line Tools (`xcode-select --install`)
   - Windows: Microsoft C++ Build Tools and WebView2 (preinstalled on Windows 11)
4. In this folder, run `npm install`.

## Run

| What | Command |
|---|---|
| The app, against your real Foreman | `npm run tauri dev` |
| The page alone in a browser, with demo data (no Foreman) | `npm run dev`, then open http://127.0.0.1:5173 |
| Page tests (XSS fixtures, RPC client, task helpers) | `npm test` |
| Shell tests (request checks, confirmations, links) | `cd src-tauri && cargo test` |
| A release build (no installer yet) | `npm run tauri build` |

In the browser demo, `#task/3`, `#conversation/<project>/<id>` and `#settings/models` open a view directly. An
end-to-end test can drive the page in a browser against a real `foreman serve --stdio` by setting
`window.__foremanBridge` before the page loads (see `bridgeTransport` in `src/lib/rpc.js`; it is ignored inside the
app).

## The app icon in the dock (Linux)

A desktop shows an app's icon and name from its `.desktop` entry. A packaged build installs one itself. For
`npm run tauri dev`, run `desktop/scripts/install-linux-launcher.sh` once: it installs the entry and the icon into
`~/.local/share`, and then Foreman can be started from the app grid and pinned to the dash. The window reports the
app id `dev.foreman.desktop` (`enableGTKAppId` in `tauri.conf.json`), which is how GNOME matches it to that entry.
Side effect: with an app id, a second `tauri dev` joins the first instead of opening another window.

## How it is put together

- The app and the CLI offer the same operations, through the same API. `src/orchestrator/surface.py` lists each
  operation with its CLI form and where the app offers it, and `tests/test_protocol.py` checks that the list is
  complete. When you add or change a feature, do it in both (AGENTS.md, "One feature, every front end").

- `src-tauri/src/sidecar.rs` runs `foreman serve --stdio`. It sends checked request lines and passes responses
  and notifications to the page as `rpc://message` events. When Foreman stops, the page gets `rpc://closed` with
  Foreman's last stderr lines, and its banner offers "Start again".
- `src-tauri/src/policy.rs` holds the shell's rules:
  - only the API's methods with numeric ids pass;
  - an answer to a plan approval or a new dependency goes through only after a native confirmation, built from
    the task record (the user's request, the steps, the risk, the dependency names), not from the page;
  - only http(s) links open, and only after a native confirmation.
- The webview gets six commands (`rpc_start`, `rpc_send`, `open_link`, `pick_folder`, `close_ack`, `app_exit`) and no
  plugins (`capabilities/default.json`), and a strict CSP
  (`tauri.conf.json`). Devtools exist only in debug builds.
- `src/lib/render.js` is the only place AI text becomes HTML: markdown-it with `html: false`, then DOMPurify with a
  short tag list. There are no images, SVG, styles or event attributes, links become `data-href`, and direction
  overrides are removed. `tests/render.test.js` holds the XSS fixtures.
- `src/App.svelte` is the layout: `Sidebar`, `Conversation` (messages, `TaskCard`, `QuestionWizard`, `Composer`),
  `SidePanel` (a task's full activity and raw output) and `SettingsDialog`.
- `src/lib/app.svelte.js` holds the state. `tasks.get` is the truth; the `event`, `phase`, `tasks_changed` and
  `resync` notifications only say when to read it again (the snapshot with the higher `rev` wins).
- Closing the window while tasks run asks "keep running or stop all" (D3). A saved choice ("remember my choice",
  or Settings) skips the question. If the page doesn't answer within 3 seconds, a second close closes the window
  and leaves the tasks running.

## Not in this version

- Installers, signing, a bundled Python, and desktop notifications on Windows (C2, C5 in the plan).
- An interactive terminal for a running worker. Raw output is a read-only text view.
