"""Interactive character-by-character input reader with arrow-key navigable slash dropdown.
Emulates Claude Code and Antigravity CLI terminal UX without external dependencies.
"""

import os
import select
import sys
from typing import List, Optional, Tuple


COMMANDS: List[Tuple[str, str]] = [
    ("/run",      "Launch full 4-agent sprint (Planner, Critic, Builder, Reviewer)"),
    ("/provider", "Select or switch default AI worker (Antigravity, Claude, OpenCode)"),
    ("/status",   "View single-page project state (state.md) & active workers"),
    ("/tasks",    "Inspect recent tasks, specs, and reviewer verdicts"),
    ("/link",     "Connect another directory, repository, or microservice"),
    ("/doctor",   "Check installed AI tools & system health diagnostics"),
    ("/clear",    "Clear the terminal screen"),
    ("/help",     "Display interactive command guide"),
    ("/exit",     "Exit orchestrator session")
]


def get_command_matches(prefix: str) -> List[Tuple[str, str]]:
    """Return commands matching prefix."""
    prefix_clean = prefix.strip().lower()
    return [(cmd, desc) for cmd, desc in COMMANDS if cmd.lower().startswith(prefix_clean)]


def render_dropdown_box(prefix: str, selected_index: int = 0) -> Tuple[str, int, Optional[str]]:
    """
    Render floating dropdown box with arrow selection pointer.
    Returns: (rendered_ansi_string, number_of_lines, currently_selected_command)
    """
    matches = get_command_matches(prefix)
    if not matches:
        return "", 0, None

    # Keep selected_index in valid bounds
    selected_index = max(0, min(selected_index, len(matches) - 1))
    selected_cmd = matches[selected_index][0]

    lines = ["\n  ╭─ Commands (↑/↓ to navigate, Enter to select, Esc to cancel) ──────╮"]
    matches_to_show = matches[:10]
    for i, (cmd, desc) in enumerate(matches_to_show):
        if i == selected_index:
            # Highlighted item in bright cyan with pointer
            lines.append(f"  │ \033[1;36m❯ {cmd:<11} {desc:<51}\033[0m │")
        else:
            lines.append(f"  │   {cmd:<11} {desc:<51} │")
    lines.append("  ╰───────────────────────────────────────────────────────────────────╯")

    return "\n".join(lines), len(lines), selected_cmd


def read_interactive_line(prompt: str = "\033[1;36m❯\033[0m \033[1morchestrator\033[0m › ") -> str:
    """
    Read line with live character-by-character slash dropdown and arrow key navigation.
    Falls back to sys.stdin.readline() if not a TTY.
    """
    # Guarantee prompt never has embedded newlines that would cause scroll-jump during redraw
    prompt = prompt.replace("\r", "").replace("\n", "")

    if not sys.stdin.isatty():
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.strip()

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    buf: List[str] = []
    dropdown_lines = 0
    selected_idx = 0
    active_matches_count = 0
    current_selected_cmd: Optional[str] = None
    dropdown_dismissed = False

    def get_clean_prompt_len() -> int:
        """Calculate visible length of prompt excluding ANSI escape codes."""
        import re
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return len(ansi_escape.sub('', prompt))

    def redraw():
        nonlocal dropdown_lines, active_matches_count, current_selected_cmd
        # 1. Clear dropdown from previous render
        if dropdown_lines > 0:
            sys.stdout.write("\033[J")

        # 2. Redraw prompt and current buffer
        curr_text = "".join(buf)
        sys.stdout.write("\r\033[K" + prompt + curr_text)

        # 3. If line starts with '/' and not dismissed, draw live dropdown below cursor
        if curr_text.startswith("/") and not dropdown_dismissed:
            token = curr_text.split()[0]
            matches = get_command_matches(token)
            visible_count = min(10, len(matches))
            active_matches_count = visible_count

            if matches:
                box_str, num_lines, selected_cmd = render_dropdown_box(token, selected_idx)
                dropdown_lines = num_lines
                current_selected_cmd = selected_cmd
                sys.stdout.write(box_str)
                # Move cursor back up to the input line at correct column
                target_col = get_clean_prompt_len() + len(curr_text)
                sys.stdout.write(f"\033[{num_lines}A\r\033[{target_col}C")
            else:
                dropdown_lines = 0
                active_matches_count = 0
                current_selected_cmd = None
        else:
            dropdown_lines = 0
            active_matches_count = 0
            current_selected_cmd = None

        sys.stdout.flush()

    try:
        tty.setcbreak(fd)
        sys.stdout.write(prompt)
        sys.stdout.flush()

        while True:
            raw = os.read(fd, 1)
            if not raw:
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise EOFError

            ch = raw.decode("utf-8", errors="replace")

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

            # Escape sequences (Arrow keys, Esc)
            if ch == "\x1b":
                # Check unbuffered fd if more characters follow immediately
                r, _, _ = select.select([fd], [], [], 0.05)
                if r:
                    raw2 = os.read(fd, 1)
                    ch2 = raw2.decode("utf-8", errors="replace") if raw2 else ""
                    if ch2 in ("[", "O"):
                        raw3 = os.read(fd, 1)
                        ch3 = raw3.decode("utf-8", errors="replace") if raw3 else ""
                        # Up Arrow
                        if ch3 == "A":
                            if active_matches_count > 0:
                                selected_idx = (selected_idx - 1) % active_matches_count
                                redraw()
                            continue
                        # Down Arrow
                        elif ch3 == "B":
                            if active_matches_count > 0:
                                selected_idx = (selected_idx + 1) % active_matches_count
                                redraw()
                            continue
                        # Right/Left Arrow
                        elif ch3 in ("C", "D"):
                            continue
                        # Handle keypad or extended function keys with potential trailing ~
                        elif ch3.isdigit():
                            r_tilde, _, _ = select.select([fd], [], [], 0.02)
                            if r_tilde:
                                os.read(fd, 1)
                            continue
                else:
                    # Standalone Esc key pressed
                    if dropdown_lines > 0:
                        dropdown_dismissed = True
                        redraw()
                        continue
                    else:
                        if buf:
                            buf.clear()
                            redraw()
                            continue

            # Enter (\r or \n)
            if ch in ("\r", "\n"):
                if dropdown_lines > 0:
                    sys.stdout.write("\033[J")
                sys.stdout.write("\n")
                sys.stdout.flush()

                curr_text = "".join(buf).strip()
                # If dropdown was active and user selected with arrow keys:
                if curr_text.startswith("/") and not dropdown_dismissed and current_selected_cmd:
                    # If user just typed '/' or partial prefix, return the selected item
                    if curr_text in ("/", "/p", "/s", "/t", "/l", "/d", "/c", "/h", "/e") or " " not in curr_text:
                        return current_selected_cmd
                return curr_text

            # Tab (Auto-complete to highlighted command)
            if ch == "\t":
                if current_selected_cmd:
                    buf = list(current_selected_cmd)
                    selected_idx = 0
                    redraw()
                continue

            # Backspace (\x7f or \x08)
            if ch in ("\x7f", "\x08"):
                if buf:
                    buf.pop()
                    dropdown_dismissed = False
                    selected_idx = 0
                    redraw()
                continue

            # Printable characters
            if ch.isprintable():
                buf.append(ch)
                dropdown_dismissed = False
                selected_idx = 0
                redraw()

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def select_from_menu(
    title: str,
    options: List[Tuple[str, str]],
    default_index: int = 0
) -> Optional[str]:
    """
    Interactive arrow-key menu selector.
    Supports Up/Down arrow navigation, Enter to choose, Esc / 'c' / 'q' to cancel,
    or typing the direct key (e.g. '1', '2', '3') for instant selection.
    """
    if not options:
        return None

    if not sys.stdin.isatty():
        print(f"\n{title}")
        for key, label in options:
            print(f"  [{key}] {label}")
        print("  [c] Cancel")
        try:
            val = sys.stdin.readline().strip().lower()
            if not val or val in ("c", "cancel", "q", "quit"):
                return None
            for key, _ in options:
                if val == key.lower():
                    return key
            return None
        except (EOFError, KeyboardInterrupt):
            return None

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    selected_idx = max(0, min(default_index, len(options) - 1))
    num_opts = len(options)

    def render(initial: bool = False):
        lines = []
        if initial:
            lines.append(f"\n{title} \033[2m(↑/↓ to navigate, Enter to select, Esc to cancel)\033[0m")
        for i, (key, label) in enumerate(options):
            if i == selected_idx:
                lines.append(f"  \033[1;36m❯ [{key}] {label}\033[0m")
            else:
                lines.append(f"    [{key}] {label}")
        if initial:
            sys.stdout.write("\n".join(lines) + "\n")
        else:
            sys.stdout.write(f"\033[{num_opts}A")
            for line in lines:
                sys.stdout.write(f"\r\033[K{line}\n")
        sys.stdout.flush()

    try:
        tty.setcbreak(fd)
        render(initial=True)
        keys_map = {opt[0].lower(): opt[0] for opt in options}

        while True:
            raw = os.read(fd, 1)
            if not raw:
                sys.stdout.write("\nCancelled.\n")
                sys.stdout.flush()
                return None

            ch = raw.decode("utf-8", errors="replace")

            if ch in ("\x03", "\x04"):  # Ctrl+C, Ctrl+D
                sys.stdout.write("\nCancelled.\n")
                sys.stdout.flush()
                return None

            if ch == "\x1b":
                r, _, _ = select.select([fd], [], [], 0.08)
                if r:
                    raw2 = os.read(fd, 1)
                    ch2 = raw2.decode("utf-8", errors="replace") if raw2 else ""
                    if ch2 in ("[", "O"):
                        raw3 = os.read(fd, 1)
                        ch3 = raw3.decode("utf-8", errors="replace") if raw3 else ""
                        if ch3 == "A":  # Up Arrow
                            selected_idx = (selected_idx - 1) % num_opts
                            render(initial=False)
                            continue
                        elif ch3 == "B":  # Down Arrow
                            selected_idx = (selected_idx + 1) % num_opts
                            render(initial=False)
                            continue
                        elif ch3.isdigit():
                            r_tilde, _, _ = select.select([fd], [], [], 0.02)
                            if r_tilde:
                                os.read(fd, 1)
                            continue
                else:
                    # Standalone Esc pressed -> Cancel immediately!
                    sys.stdout.write("\nCancelled.\n")
                    sys.stdout.flush()
                    return None

            elif ch in ("\r", "\n"):
                res = options[selected_idx][0]
                sys.stdout.write("\n")
                sys.stdout.flush()
                return res

            elif ch.lower() in ("c", "q"):
                sys.stdout.write("\nCancelled.\n")
                sys.stdout.flush()
                return None

            elif ch.lower() in keys_map:
                res = keys_map[ch.lower()]
                sys.stdout.write(f"\n[Selected: {res}]\n")
                sys.stdout.flush()
                return res

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def read_input_with_esc(prompt: str, default: str = "") -> Optional[str]:
    """
    Read line of text from user with immediate cancellation on Esc or Ctrl+C.
    Returns None if cancelled, or the string entered (or default if blank).
    """
    prompt = prompt.replace("\r", "").replace("\n", "")

    if not sys.stdin.isatty():
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return None
        res = line.strip()
        return res if res else default

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    buf: List[str] = []

    try:
        tty.setcbreak(fd)
        sys.stdout.write(prompt)
        sys.stdout.flush()

        while True:
            raw = os.read(fd, 1)
            if not raw:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return None

            ch = raw.decode("utf-8", errors="replace")

            if ch in ("\x03", "\x04"):  # Ctrl+C, Ctrl+D
                sys.stdout.write("\nCancelled.\n")
                sys.stdout.flush()
                return None

            if ch == "\x1b":
                r, _, _ = select.select([fd], [], [], 0.05)
                if r:
                    # Consume any trailing escape sequences
                    os.read(fd, 8)
                    continue
                else:
                    # Standalone Esc pressed! Cancel immediately!
                    sys.stdout.write("\nCancelled.\n")
                    sys.stdout.flush()
                    return None

            elif ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                res = "".join(buf).strip()
                return res if res else default

            elif ch in ("\x7f", "\x08"):
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()

            elif ch.isprintable():
                buf.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
