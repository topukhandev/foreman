"""Shell completion scripts for `foreman completion bash|zsh`. Static on purpose: completing must not start Python."""

from typing import Dict, List, Tuple

COMMANDS: List[Tuple[str, str]] = [
    ("chat", "interactive chat"), ("ask", "submit a request"), ("next", "start the recommended next step"),
    ("lifecycle", "where features stand"), ("modes", "list lifecycle modes"), ("status", "state of a task"),
    ("resume", "continue a task"), ("answer", "answer an open question"), ("close", "mark a task finished or cancelled"),
    ("tasks", "list tasks"),
    ("show", "task details"), ("projects", "project registry"), ("sessions", "existing AI sessions"),
    ("suggestions", "open suggestions"), ("accept", "act on a suggestion"), ("dismiss", "dismiss a suggestion"),
    ("config", "show or change settings"), ("doctor", "check providers and setup"), ("mcp", "MCP server"),
    ("completion", "print a shell completion script"), ("version", "print the version"), ("help", "usage"),
]
ARGUMENTS: Dict[str, List[str]] = {
    "config": ["display", "notify", "reuse", "planner", "first-worker", "copy-on-select"],
    "mcp": ["serve", "install", "status"],
    "projects": ["list", "scan", "show", "add", "link", "unlink", "test"],
    "completion": ["bash", "zsh"],
}
MODES = ["research", "planning", "development", "qa", "deployment"]
FLAGS = ["--visual", "--headless", "--mode", "--dry-run", "--fix", "--cwd", "--json", "--events", "-c", "-r", "--version", "-h", "--help"]
SHELLS = tuple(ARGUMENTS["completion"])


def bash() -> str:
    cases = "".join(f"    {cmd}) COMPREPLY=($(compgen -W \"{' '.join(vals)}\" -- \"$cur\")); return ;;\n"
                    for cmd, vals in ARGUMENTS.items())
    return f"""# bash completion for foreman: eval "$(foreman completion bash)"
_foreman() {{
  local cur prev
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  prev="${{COMP_WORDS[COMP_CWORD-1]}}"
  if [ "$prev" = "--mode" ]; then
    COMPREPLY=($(compgen -W "{' '.join(MODES)}" -- "$cur")); return
  fi
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W "{' '.join(c for c, _ in COMMANDS)} {' '.join(FLAGS)}" -- "$cur")); return
  fi
  case "${{COMP_WORDS[1]}}" in
{cases}  esac
  if [[ "$cur" == -* ]]; then
    COMPREPLY=($(compgen -W "{' '.join(FLAGS)}" -- "$cur"))
  fi
}}
complete -F _foreman foreman orchestrator
"""


def zsh() -> str:
    cmds = "\n    ".join(f"'{c}:{d}'" for c, d in COMMANDS)
    cases = "".join(f"    {cmd}) compadd {' '.join(vals)} ;;\n" for cmd, vals in ARGUMENTS.items())
    return f"""#compdef foreman orchestrator
# zsh completion for foreman: foreman completion zsh > "${{fpath[1]}}/_foreman"   (or: eval "$(foreman completion zsh)")
_foreman() {{
  local -a cmds
  cmds=(
    {cmds}
  )
  if [[ "$words[CURRENT-1]" == --mode ]]; then
    compadd {' '.join(MODES)}
    return
  fi
  if (( CURRENT == 2 )); then
    _describe 'command' cmds
    compadd -- {' '.join(FLAGS)}
    return
  fi
  case "$words[2]" in
{cases}    *) [[ "$words[CURRENT]" == -* ]] && compadd -- {' '.join(FLAGS)} ;;
  esac
}}
compdef _foreman foreman orchestrator
"""


def script(shell: str) -> str:
    return {"bash": bash, "zsh": zsh}[shell]()
