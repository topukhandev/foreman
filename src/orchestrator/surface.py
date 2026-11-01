"""The front-end protocol: everything a user can do in Foreman, and where each front end offers it.

The rule (AGENTS.md, "One feature, every front end"):
- An operation is built once, in the core: the behaviour in the engine and its other core modules, the data shape
  in `api.Api` (a JSON-RPC method in `rpc_server.METHODS`).
- Every operation reaches every front end: the CLI (a `foreman` command, or the terminal chat for conversations)
  and the desktop app. A new feature, or a change to one, is made in the core and then offered by both.
- A front end leaves an operation out only with the reason written below ("-: why"). "via <method>" means it is
  offered through another operation (the app sends every request in a conversation, for example).
- tests/test_protocol.py fails when an API method is missing here, a CLI command is neither an operation's form nor
  explained in CLI_ONLY, a CLI form names a command that doesn't exist, or the app doesn't call a method it is
  listed as offering.

The MCP server is a third front end for AI clients; it offers the subset of tools an assistant needs
(mcp_server.TOOLS) through the same API, and is not held to this table.
"""

from typing import Dict, Tuple

# API method -> (how the CLI offers it, how the desktop app offers it)
OPERATIONS: Dict[str, Tuple[str, str]] = {
    # system
    "system.hello": ("-: the handshake of an API client; `foreman version` prints the versions", "on connecting"),
    "system.doctor": ("foreman doctor", "Settings > About: Check setup"),
    "system.stats": ("foreman stats", "Settings > About: totals"),
    # tasks
    "tasks.list": ("foreman tasks", "sidebar (Needs you, tasks under each project)"),
    "tasks.get": ("foreman show", "task card"),
    "tasks.submit": ("foreman ask", "via conversations.send: the app sends every request in a conversation"),
    "tasks.answer": ("foreman answer", "task card: questions, one at a time"),
    "tasks.resume": ("foreman resume", "task card: Continue / Run this plan"),
    "tasks.next": ("foreman next", "via conversations.send: the task card's Next button says \"next\""),
    "tasks.stop": ("foreman stop", "task card: Stop"),
    "tasks.close": ("foreman close", "task card: Mark finished / Cancel task"),
    "tasks.events": ("foreman show --events", "task card (latest activity) and the side panel's Activity"),
    "tasks.outputs": ("foreman output", "side panel: Raw output"),
    "tasks.output": ("foreman output", "side panel: Raw output"),
    # projects
    "projects.list": ("foreman projects", "sidebar (opened projects) and Settings > Projects"),
    "projects.get": ("foreman projects show", "via projects.list: the list carries each project's details"),
    "projects.scan": ("foreman projects scan", "Settings > Projects: Scan again"),
    "projects.open": ("foreman open", "New conversation: the folder picker"),
    "projects.hide": ("foreman projects hide", "sidebar: a project's Hide"),
    "lifecycle.get": ("foreman lifecycle", "via conversations.send: \"what's next?\" in a conversation"),
    "sessions.list": ("foreman sessions", "Settings > Projects: a project's AI sessions"),
    # conversations
    "conversations.list": ("foreman conversations", "sidebar (conversations under each project)"),
    "conversations.get": ("foreman conversations", "the conversation view"),
    "conversations.send": ("foreman chat", "the message box"),
    "conversations.rename": ("chat /rename", "the conversation title (click to rename)"),
    # suggestions
    "suggestions.list": ("foreman suggestions", "task card: Worth considering"),
    "suggestions.accept": ("foreman accept", "task card: Do it"),
    "suggestions.dismiss": ("foreman dismiss", "task card: Dismiss"),
    # settings
    "config.get": ("foreman config", "Settings > General"),
    "config.set": ("foreman config", "Settings > General"),
    "config.set_on_close": ("foreman config", "the close dialog: Remember my choice"),
    "models.list": ("foreman models", "Settings > Models: Change"),
    "models.set": ("foreman models", "Settings > Models: Use this"),
    # live updates
    "events.subscribe": ("-: notifications for a connected client; the terminal chat follows its running task "
                         "itself", "on connecting and when a task is opened"),
    "events.unsubscribe": ("-: as events.subscribe", "-: the app follows every active task while it is open"),
}

# CLI commands that are not operations: front ends, plumbing, or terminal-only conveniences, with the reason.
CLI_ONLY: Dict[str, str] = {
    "chat": "the terminal chat itself (the CLI's conversation front end)",
    "help": "help text", "-h": "help text", "--help": "help text", "version": "prints the version",
    "completion": "shell completion scripts",
    "mcp": "runs or registers the MCP server (the front end for AI clients)",
    "serve": "runs the API server the desktop app talks to",
    "run-task": "internal: the detached runner a front end starts for a task",
    "status": "the same instant report as asking \"status\" in a conversation (conversations.send)",
    "modes": "lists the lifecycle mode templates and where each comes from (files to edit); both front ends "
             "offer the modes themselves",
}
