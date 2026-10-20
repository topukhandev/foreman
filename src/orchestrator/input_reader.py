"""Interactive character-by-character input reader with live slash command popup."""

import os
import sys
from typing import List, Optional, Tuple


COMMANDS: List[Tuple[str, str]] = [
    ("/provider", "Select or switch default AI worker (Antigravity, Claude, OpenCode)"),
    ("/status",   "View single-page project state (state.md) & active workers"),
    ("/tasks",    "Inspect recent tasks, specs, and reviewer verdicts"),
    ("/link",     "Connect another directory, repository, or microservice"),
    ("/pack",     "Select default workflow pack (developer, business_idea, qa)"),
    ("/doctor",   "Check installed AI tools & system health diagnostics"),
    ("/clear",    "Clear the terminal screen"),
    ("/help",     "Display interactive command guide"),
    ("/exit",     "Exit orchestrator session")
]


def get_command_matches(prefix: str) -> List[Tuple[str, str]]:
    """Return commands matching prefix."""
    prefix_clean = prefix.strip().lower()
    return [(cmd, desc) for cmd, desc in COMMANDS if cmd.lower().startswith(prefix_clean)]


def render_dropdown_box(prefix: str) -> Tuple[str, int]:
    """Render popup box string and count of lines."""
    matches = get_command_matches(prefix)
    if not matches:
        return "", 0

    lines = ["\n  ╭─ Slash Commands (Tab to complete, Enter to run) ────────────────────╮"]
    for cmd, desc in matches[:7]:
        lines.append(f"  │  {cmd:<12} {desc:<53} │")
    lines.append("  ╰─────────────────────────────────────────────────────────────────────╯")
    return "\n".join(lines), len(lines)


def read_interactive_line(prompt: str = "orchestrator> ") -> str:
    """
    Read line with live character-by-character slash dropdown.
    Falls back to sys.stdin.readline() if not a TTY.
    """
    if not sys.stdin.isatty():
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.strip()

    # Unix terminal cbreak mode for instant key reactions
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    buf: List[str] = []
    dropdown_lines = 0

    def redraw():
        nonlocal dropdown_lines
        # 1. Clear dropdown from previous render
        if dropdown_lines > 0:
            # Move down into dropdown area and clear down
            sys.stdout.write("\033[J")

        # 2. Redraw prompt and current buffer
        curr_text = "".join(buf)
        sys.stdout.write("\r\033[K" + prompt + curr_text)

        # 3. If line starts with '/', draw live dropdown below cursor
        if curr_text.startswith("/"):
            token = curr_text.split()[0]
            box_str, num_lines = render_dropdown_box(token)
            if box_str:
                dropdown_lines = num_lines
                sys.stdout.write(box_str)
                # Move cursor back up to the input line at correct column
                target_col = len(prompt) + len(curr_text)
                sys.stdout.write(f"\033[{num_lines}A\r\033[{target_col}C")
            else:
                dropdown_lines = 0
        else:
            dropdown_lines = 0

        sys.stdout.flush()

    try:
        tty.setcbreak(fd)
        sys.stdout.write(prompt)
        sys.stdout.flush()

        while True:
            ch = sys.stdin.read(1)

            # Ctrl+C
            if ch == "\x03":
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise KeyboardInterrupt

            # Ctrl+D
            if ch == "\x04":
                if not buf:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    raise EOFError

            # Enter (\r or \n)
            if ch in ("\r", "\n"):
                if dropdown_lines > 0:
                    sys.stdout.write("\033[J")
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(buf).strip()

            # Tab (Auto-complete command)
            if ch == "\t":
                curr_text = "".join(buf)
                if curr_text.startswith("/"):
                    token = curr_text.split()[0]
                    matches = get_command_matches(token)
                    if matches:
                        # Auto-complete to first matching command
                        completed_cmd = matches[0][0]
                        buf = list(completed_cmd)
                        redraw()
                continue

            # Backspace (\x7f or \x08)
            if ch in ("\x7f", "\x08"):
                if buf:
                    buf.pop()
                    redraw()
                continue

            # Printable characters
            if ch.isprintable():
                buf.append(ch)
                redraw()

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
