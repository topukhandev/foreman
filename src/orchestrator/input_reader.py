"""Interactive character-by-character input reader with arrow-key navigable slash dropdown.
Emulates Claude Code and Antigravity CLI terminal UX without external dependencies.
"""

import os
import re
import select
import shutil
import sys
from typing import List, Optional, Tuple


_ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def get_clean_prompt_len(prompt: str) -> int:
    """Calculate visible length of prompt excluding ANSI escape codes."""
    return len(_ANSI_ESCAPE_RE.sub('', prompt))


COMMANDS: List[Tuple[str, str]] = [
    ("/run",      "Launch full 4-agent sprint (Planner, Critic, Builder, Reviewer)"),
    ("/new",      "Start a fresh conversation thread with active AI worker"),
    ("/provider", "Select or switch default AI worker (Antigravity, Claude, OpenCode)"),
    ("/visual",   "Toggle visible desktop terminal windows for AI workers (on/off)"),
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
    matches_to_show = matches[:15]
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
    Read line with live character-by-character slash dropdown, arrow key navigation,
    and glitch-free long text pasting across wrapped terminal rows.
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
    last_rendered_rows = 0
    selected_idx = 0
    active_matches_count = 0
    current_selected_cmd: Optional[str] = None
    dropdown_dismissed = False
    in_bracketed_paste = False

    clean_prompt_len = get_clean_prompt_len(prompt)

    def update_matches():
        nonlocal active_matches_count, current_selected_cmd, selected_idx
        curr_text = "".join(buf)
        if curr_text.startswith("/") and not dropdown_dismissed and " " not in curr_text:
            token = curr_text
            matches = get_command_matches(token)
            active_matches_count = min(15, len(matches))
            if active_matches_count > 0:
                selected_idx = max(0, min(selected_idx, active_matches_count - 1))
                current_selected_cmd = matches[selected_idx][0]
            else:
                selected_idx = 0
                current_selected_cmd = None
        else:
            active_matches_count = 0
            current_selected_cmd = None

    def redraw():
        nonlocal dropdown_lines, last_rendered_rows
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        if cols < 20:
            cols = 80

        # If dropdown was visible previously, cursor was already restored to prompt row.
        # Otherwise, move cursor up by previously rendered wrapped rows.
        if dropdown_lines > 0:
            dropdown_lines = 0
        elif last_rendered_rows > 0:
            sys.stdout.write(f"\033[{last_rendered_rows}A")

        curr_text = "".join(buf)
        sys.stdout.write("\r\033[J" + prompt + curr_text)

        total_len = clean_prompt_len + len(curr_text)
        last_rendered_rows = (total_len - 1) // cols if total_len > 0 else 0

        # Render floating dropdown box below cursor if command prefix is active
        if active_matches_count > 0 and curr_text.startswith("/") and not dropdown_dismissed and " " not in curr_text:
            box_str, num_lines, _ = render_dropdown_box(curr_text, selected_idx)
            dropdown_lines = num_lines
            sys.stdout.write(box_str)
            # Move cursor back up to the input line at correct column
            target_col = clean_prompt_len + len(curr_text)
            sys.stdout.write(f"\033[{num_lines}A\r\033[{target_col}C")

        sys.stdout.flush()

    try:
        tty.setcbreak(fd)
        # Enable bracketed paste mode so terminals delimit pasted blocks
        sys.stdout.write("\033[?2004h")
        sys.stdout.write(prompt)
        sys.stdout.flush()

        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise EOFError

            # Absorb any remaining bytes already in OS buffer for instant paste responsiveness
            while True:
                r, _, _ = select.select([fd], [], [], 0)
                if not r:
                    break
                more = os.read(fd, 4096)
                if not more:
                    break
                chunk += more

            text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
            i = 0
            while i < len(text):
                # Bracketed paste sequences
                if text[i:i+6] == "\x1b[200~":
                    in_bracketed_paste = True
                    i += 6
                    continue
                if text[i:i+6] == "\x1b[201~":
                    in_bracketed_paste = False
                    i += 6
                    continue

                # Escape sequences (Arrow keys, Esc, function keys)
                if text[i] == "\x1b":
                    # Check if more bytes of an escape sequence are in flight
                    if i == len(text) - 1:
                        r, _, _ = select.select([fd], [], [], 0.05)
                        if r:
                            more = os.read(fd, 1024)
                            if more:
                                text += more.decode("utf-8", errors="replace").replace("\r\n", "\n")

                    if i + 2 < len(text) and text[i+1] in ("[", "O"):
                        code = text[i+2]
                        if code == "A":  # Up Arrow
                            if active_matches_count > 0:
                                selected_idx = (selected_idx - 1) % active_matches_count
                                update_matches()
                            i += 3
                            continue
                        elif code == "B":  # Down Arrow
                            if active_matches_count > 0:
                                selected_idx = (selected_idx + 1) % active_matches_count
                                update_matches()
                            i += 3
                            continue
                        elif code in ("C", "D"):  # Left / Right Arrow
                            i += 3
                            continue
                        elif code.isdigit():
                            # Extended sequence like \x1b[3~ (Delete), \x1b[1~ (Home), etc.
                            end = i + 2
                            while end < len(text) and text[end] != "~":
                                end += 1
                            i = end + 1 if end < len(text) else len(text)
                            continue
                        else:
                            i += 3
                            continue
                    else:
                        # Standalone Esc key pressed
                        if dropdown_lines > 0:
                            dropdown_dismissed = True
                        elif buf:
                            buf.clear()
                        update_matches()
                        i += 1
                        continue

                # Enter (\r or \n)
                elif text[i] in ("\r", "\n"):
                    # Inside a paste or if followed by more characters in the same chunk,
                    # convert newline to space so paste doesn't trigger premature execution.
                    if in_bracketed_paste or i < len(text) - 1:
                        if buf and buf[-1] != " ":
                            buf.append(" ")
                        update_matches()
                    else:
                        if dropdown_lines > 0:
                            sys.stdout.write("\033[J")
                        sys.stdout.write("\n")
                        sys.stdout.flush()

                        curr_text = "".join(buf).strip()
                        # If dropdown was active and user selected with arrow keys:
                        if curr_text.startswith("/") and not dropdown_dismissed and current_selected_cmd:
                            if curr_text in ("/", "/p", "/s", "/t", "/l", "/d", "/c", "/h", "/e") or " " not in curr_text:
                                return current_selected_cmd
                        return curr_text
                    i += 1

                # Tab (Auto-complete to highlighted command)
                elif text[i] == "\t":
                    if current_selected_cmd:
                        buf = list(current_selected_cmd)
                        selected_idx = 0
                        update_matches()
                    i += 1

                # Backspace (\x7f or \x08)
                elif text[i] in ("\x7f", "\x08"):
                    if buf:
                        buf.pop()
                        dropdown_dismissed = False
                        selected_idx = 0
                        update_matches()
                    i += 1

                # Ctrl+C
                elif text[i] == "\x03":
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    raise KeyboardInterrupt

                # Ctrl+D
                elif text[i] == "\x04":
                    if not buf:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        raise EOFError
                    i += 1

                # Printable characters
                elif text[i] >= " ":
                    buf.append(text[i])
                    dropdown_dismissed = False
                    selected_idx = 0
                    update_matches()
                    i += 1
                else:
                    i += 1

            redraw()

    finally:
        sys.stdout.write("\033[?2004l")
        sys.stdout.flush()
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
    Read line of text from user with immediate cancellation on Esc or Ctrl+C,
    with clean multiline redraw and bracketed paste protection.
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
    last_rendered_rows = 0
    in_bracketed_paste = False
    clean_prompt_len = get_clean_prompt_len(prompt)

    def redraw():
        nonlocal last_rendered_rows
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        if cols < 20:
            cols = 80
        if last_rendered_rows > 0:
            sys.stdout.write(f"\033[{last_rendered_rows}A")
        curr_text = "".join(buf)
        sys.stdout.write("\r\033[J" + prompt + curr_text)
        total_len = clean_prompt_len + len(curr_text)
        last_rendered_rows = (total_len - 1) // cols if total_len > 0 else 0
        sys.stdout.flush()

    try:
        tty.setcbreak(fd)
        sys.stdout.write("\033[?2004h")
        sys.stdout.write(prompt)
        sys.stdout.flush()

        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return None

            while True:
                r, _, _ = select.select([fd], [], [], 0)
                if not r:
                    break
                more = os.read(fd, 4096)
                if not more:
                    break
                chunk += more

            text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
            i = 0
            while i < len(text):
                if text[i:i+6] == "\x1b[200~":
                    in_bracketed_paste = True
                    i += 6
                    continue
                if text[i:i+6] == "\x1b[201~":
                    in_bracketed_paste = False
                    i += 6
                    continue

                if text[i] == "\x1b":
                    if i == len(text) - 1:
                        r, _, _ = select.select([fd], [], [], 0.05)
                        if r:
                            more = os.read(fd, 1024)
                            if more:
                                text += more.decode("utf-8", errors="replace").replace("\r\n", "\n")

                    if i + 1 < len(text) and text[i+1] in ("[", "O"):
                        # Skip arrow / function key escape sequences
                        i += 2
                        while i < len(text) and not text[i].isalpha() and text[i] != "~":
                            i += 1
                        i += 1
                        continue
                    else:
                        # Standalone Esc pressed -> Cancel immediately!
                        sys.stdout.write("\nCancelled.\n")
                        sys.stdout.flush()
                        return None

                elif text[i] in ("\x03", "\x04"):  # Ctrl+C, Ctrl+D
                    sys.stdout.write("\nCancelled.\n")
                    sys.stdout.flush()
                    return None

                elif text[i] in ("\r", "\n"):
                    if in_bracketed_paste or i < len(text) - 1:
                        if buf and buf[-1] != " ":
                            buf.append(" ")
                    else:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        res = "".join(buf).strip()
                        return res if res else default
                    i += 1

                elif text[i] in ("\x7f", "\x08"):
                    if buf:
                        buf.pop()
                    i += 1

                elif text[i] >= " ":
                    buf.append(text[i])
                    i += 1
                else:
                    i += 1

            redraw()

    finally:
        sys.stdout.write("\033[?2004l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
