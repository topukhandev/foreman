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
    ("/run",       "Launch full 4-agent sprint (Planner, Critic, Builder, Reviewer)"),
    ("/new",       "Start a fresh conversation thread with active AI worker"),
    ("/provider",  "Select or switch default AI worker (Antigravity, Claude, OpenCode)"),
    ("/effort",    "Set AI reasoning effort & model tier (low, medium, high)"),
    ("/voice",     "Toggle voice command recording or set API key (/voice key <key>)"),
    ("/visual",    "Toggle visible desktop terminal windows for AI workers (on/off)"),
    ("/close",     "Instantly close all active desktop terminal windows"),
    ("/status",    "View single-page project state (state.md) & active workers"),
    ("/system",    "View real-time System Resource Guard metrics (RAM, CPU, Disk)"),
    ("/tasks",     "Inspect recent tasks, specs, and reviewer verdicts"),
    ("/link",      "Connect another directory, repository, or microservice"),
    ("/doctor",    "Check installed AI tools & system health diagnostics"),
    ("/clear",     "Clear the terminal screen"),
    ("/help",      "Display interactive command guide"),
    ("/exit",      "Exit orchestrator session")
]


def get_command_matches(prefix: str) -> List[Tuple[str, str]]:
    """Return commands matching prefix."""
    prefix_clean = prefix.strip().lower()
    return [(cmd, desc) for cmd, desc in COMMANDS if cmd.lower().startswith(prefix_clean)]


def render_dropdown_box(prefix: str, selected_index: int = 0) -> Tuple[str, int, Optional[str]]:
    """
    Render floating dropdown box with arrow selection pointer.
    Guaranteed to fit terminal width without line wrapping or row desynchronization.
    Returns: (rendered_ansi_string, number_of_lines, currently_selected_command)
    """
    matches = get_command_matches(prefix)
    if not matches:
        return "", 0, None

    # Keep selected_index in valid bounds
    selected_index = max(0, min(selected_index, len(matches) - 1))
    selected_cmd = matches[selected_index][0]

    cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    if cols < 20:
        cols = 80

    if cols < 42:
        compact_line = f"  \033[1;36m❯ {selected_cmd}\033[0m \033[90m(Enter to run, Esc to back)\033[0m"
        return "\n" + compact_line, 1, selected_cmd

    box_width = max(34, min(cols - 4, 72))

    title = " Commands (↑/↓ to navigate, Enter to select, Esc to cancel) "
    if len(title) > box_width - 4:
        title = " Commands "
    dash_total = max(2, box_width - 2 - len(title))
    dashes_left = dash_total // 2
    dashes_right = dash_total - dashes_left
    top_border = f"  ╭{'─' * dashes_left}{title}{'─' * dashes_right}╮"
    bottom_border = f"  ╰{'─' * (box_width - 2)}╯"

    inner_w = box_width - 4
    cmd_w = 11
    desc_w = max(10, inner_w - cmd_w - 4)

    lines = [top_border]
    matches_to_show = matches[:7]
    for i, (cmd, desc) in enumerate(matches_to_show):
        clean_desc = desc if len(desc) <= desc_w else desc[:desc_w - 3] + "..."
        if i == selected_index:
            lines.append(f"  │ \033[1;36m❯ {cmd:<{cmd_w}} {clean_desc:<{desc_w}}\033[0m │")
        else:
            lines.append(f"  │   {cmd:<{cmd_w}} {clean_desc:<{desc_w}} │")
    lines.append(bottom_border)

    return "\n" + "\n".join(lines), len(lines), selected_cmd


_HISTORY_FILE = os.path.expanduser("~/.foreman_history")
SESSION_HISTORY: List[str] = []


def _load_history() -> None:
    """Load persistent command history from disk."""
    global SESSION_HISTORY
    if not SESSION_HISTORY and os.path.isfile(_HISTORY_FILE):
        try:
            with open(_HISTORY_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = [line.rstrip("\r\n") for line in f if line.strip()]
                SESSION_HISTORY = lines[-1000:]
        except Exception:
            pass


def _append_history(line: str) -> None:
    """Append a command line to in-memory and persistent history."""
    line = line.strip()
    if not line:
        return
    if not SESSION_HISTORY or SESSION_HISTORY[-1] != line:
        SESSION_HISTORY.append(line)
        try:
            with open(_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def read_interactive_line(prompt: str = "\033[1;36m❯\033[0m \033[1morchestrator\033[0m › ") -> str:
    """
    Read line with live character-by-character slash dropdown, arrow key navigation,
    persistent command history recall (Up/Down arrows), full cursor movement (Left/Right arrows),
    and glitch-free long text pasting across wrapped terminal rows.
    Falls back to sys.stdin.readline() if not a TTY.
    """
    _load_history()
    # Guarantee prompt never has embedded newlines that would cause scroll-jump during redraw
    prompt = prompt.replace("\r", "").replace("\n", "")

    if not sys.stdin.isatty():
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        ret = line.strip()
        _append_history(ret)
        return ret

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    buf: List[str] = []
    cursor_pos: int = 0
    history_index: int = len(SESSION_HISTORY)
    scratch_buffer: str = ""
    dropdown_lines = 0
    last_rendered_cursor_row = 0
    selected_idx = 0
    active_matches_count = 0
    current_selected_cmd: Optional[str] = None
    dropdown_dismissed = False
    in_bracketed_paste = False

    clean_prompt_len = get_clean_prompt_len(prompt)

    voice_active = False
    recorder = None

    def toggle_voice():
        nonlocal voice_active, recorder, cursor_pos, buf
        if not voice_active:
            from orchestrator.voice import VoiceRecorder, get_recording_tool
            tool = get_recording_tool()
            if not tool:
                sys.stdout.write("\r\033[K\033[1;31m[!] Neither arecord nor ffmpeg installed for audio recording.\033[0m\n")
                redraw()
                return

            recorder = VoiceRecorder()
            if recorder.start():
                voice_active = True
                sys.stdout.write("\r\033[K\033[1;31m🎙️  [Recording Audio...]\033[0m \033[1;37mSpeak now!\033[0m \033[90m(Press Ctrl+V, Ctrl+M, or Enter to stop)\033[0m")
                sys.stdout.flush()
        else:
            sys.stdout.write("\r\033[K\033[1;33m⏳ Transcribing audio...\033[0m")
            sys.stdout.flush()
            wav_file = recorder.stop() if recorder else None
            voice_active = False
            recorder = None

            if wav_file:
                from orchestrator.voice import transcribe_audio
                ok, transcript = transcribe_audio(wav_file)
                if ok and transcript:
                    clean_text = transcript.strip()
                    for ch in clean_text:
                        buf.insert(cursor_pos, ch)
                        cursor_pos += 1
                else:
                    sys.stdout.write(f"\r\033[K\033[1;33m[Voice Info]\033[0m {transcript}\n")
            update_matches()
            redraw()

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
        nonlocal dropdown_lines, last_rendered_cursor_row
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        if cols < 20:
            cols = 80

        # Restore cursor to prompt base row if previous text wrapped across rows
        if last_rendered_cursor_row > 0:
            sys.stdout.write(f"\033[{last_rendered_cursor_row}A")
            last_rendered_cursor_row = 0

        curr_text = "".join(buf)
        # Move to column 0 and clear from cursor to bottom of screen (erasing previous prompt and dropdown)
        sys.stdout.write("\r\033[J" + prompt + curr_text)

        total_len = clean_prompt_len + len(curr_text)
        end_row = (total_len - 1) // cols if total_len > 0 else 0

        # Render floating dropdown box below cursor if command prefix is active
        if active_matches_count > 0 and curr_text.startswith("/") and not dropdown_dismissed and " " not in curr_text:
            box_str, num_lines, _ = render_dropdown_box(curr_text, selected_idx)
            dropdown_lines = num_lines
            sys.stdout.write(box_str)
            # Move cursor back up from bottom of dropdown to end_row
            sys.stdout.write(f"\033[{num_lines}A")
        else:
            dropdown_lines = 0

        # Position cursor at cursor_pos
        cursor_total = clean_prompt_len + cursor_pos
        target_row = (cursor_total - 1) // cols if cursor_total > 0 else 0
        target_col = cursor_total % cols if cols > 0 else cursor_total

        rows_up = end_row - target_row
        if rows_up > 0:
            sys.stdout.write(f"\033[{rows_up}A")
        sys.stdout.write(f"\033[{target_col + 1}G")

        last_rendered_cursor_row = target_row
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

                    seq_match = re.match(r'^\x1b(?:\[[0-9;]*[a-zA-Z~]|O[a-zA-Z])', text[i:])
                    if seq_match:
                        seq = seq_match.group(0)
                        seq_len = len(seq)

                        if seq in ("\x1b[A", "\x1bOA"):  # Up Arrow
                            if active_matches_count > 0:
                                selected_idx = (selected_idx - 1) % active_matches_count
                                update_matches()
                            else:
                                if SESSION_HISTORY and history_index > 0:
                                    if history_index == len(SESSION_HISTORY):
                                        scratch_buffer = "".join(buf)
                                    history_index -= 1
                                    buf = list(SESSION_HISTORY[history_index])
                                    cursor_pos = len(buf)
                                    dropdown_dismissed = True
                                    update_matches()
                        elif seq in ("\x1b[B", "\x1bOB"):  # Down Arrow
                            if active_matches_count > 0:
                                selected_idx = (selected_idx + 1) % active_matches_count
                                update_matches()
                            else:
                                if history_index < len(SESSION_HISTORY):
                                    history_index += 1
                                    if history_index == len(SESSION_HISTORY):
                                        buf = list(scratch_buffer)
                                    else:
                                        buf = list(SESSION_HISTORY[history_index])
                                    cursor_pos = len(buf)
                                    dropdown_dismissed = True
                                    update_matches()
                        elif seq in ("\x1b[D", "\x1bOD"):  # Left Arrow
                            cursor_pos = max(0, cursor_pos - 1)
                        elif seq in ("\x1b[C", "\x1bOC"):  # Right Arrow
                            cursor_pos = min(len(buf), cursor_pos + 1)
                        elif seq in ("\x1b[H", "\x1bOH", "\x1b[1~", "\x1b[7~"):  # Home
                            cursor_pos = 0
                        elif seq in ("\x1b[F", "\x1bOF", "\x1b[4~", "\x1b[8~"):  # End
                            cursor_pos = len(buf)
                        elif seq == "\x1b[3~":  # Delete
                            if cursor_pos < len(buf):
                                buf.pop(cursor_pos)
                                dropdown_dismissed = False
                                selected_idx = 0
                                update_matches()
                        elif seq in ("\x1b[1;5D", "\x1b[5D"):  # Ctrl+Left (word jump)
                            while cursor_pos > 0 and buf[cursor_pos - 1] == ' ':
                                cursor_pos -= 1
                            while cursor_pos > 0 and buf[cursor_pos - 1] != ' ':
                                cursor_pos -= 1
                        elif seq in ("\x1b[1;5C", "\x1b[5C"):  # Ctrl+Right (word jump)
                            while cursor_pos < len(buf) and buf[cursor_pos] == ' ':
                                cursor_pos += 1
                            while cursor_pos < len(buf) and buf[cursor_pos] != ' ':
                                cursor_pos += 1

                        i += seq_len
                        continue
                    elif text[i:i+2] == "\x1bb":  # Alt+B (word jump left)
                        while cursor_pos > 0 and buf[cursor_pos - 1] == ' ':
                            cursor_pos -= 1
                        while cursor_pos > 0 and buf[cursor_pos - 1] != ' ':
                            cursor_pos -= 1
                        i += 2
                        continue
                    elif text[i:i+2] == "\x1bf":  # Alt+F (word jump right)
                        while cursor_pos < len(buf) and buf[cursor_pos] == ' ':
                            cursor_pos += 1
                        while cursor_pos < len(buf) and buf[cursor_pos] != ' ':
                            cursor_pos += 1
                        i += 2
                        continue
                    else:
                        # Standalone Esc key pressed
                        if dropdown_lines > 0:
                            dropdown_dismissed = True
                        elif buf:
                            buf.clear()
                            cursor_pos = 0
                        update_matches()
                        i += 1
                        continue

                # Enter (\r or \n)
                elif text[i] in ("\r", "\n"):
                    if voice_active:
                        toggle_voice()
                        i += 1
                        continue
                    # Inside a paste or if followed by more characters in the same chunk,
                    # convert newline to space so paste doesn't trigger premature execution.
                    if in_bracketed_paste or i < len(text) - 1:
                        if not buf or (cursor_pos > 0 and buf[cursor_pos - 1] != " "):
                            buf.insert(cursor_pos, " ")
                            cursor_pos += 1
                            update_matches()
                    else:
                        if dropdown_lines > 0:
                            sys.stdout.write("\033[J")
                        curr_text = "".join(buf)
                        total_len = clean_prompt_len + len(curr_text)
                        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
                        if cols < 20:
                            cols = 80
                        end_row = (total_len - 1) // cols if total_len > 0 else 0
                        if last_rendered_cursor_row < end_row:
                            sys.stdout.write(f"\033[{end_row - last_rendered_cursor_row}B")
                        sys.stdout.write("\n")
                        sys.stdout.flush()

                        clean_val = curr_text.strip()
                        ret_cmd = clean_val
                        # If dropdown was active and user selected with arrow keys:
                        if clean_val.startswith("/") and not dropdown_dismissed and current_selected_cmd:
                            if clean_val in ("/", "/p", "/s", "/t", "/l", "/d", "/c", "/h", "/e") or " " not in clean_val:
                                ret_cmd = current_selected_cmd

                        _append_history(ret_cmd)
                        return ret_cmd
                    i += 1

                # Tab (Auto-complete to highlighted command)
                elif text[i] == "\t":
                    if current_selected_cmd:
                        buf = list(current_selected_cmd)
                        cursor_pos = len(buf)
                        selected_idx = 0
                        update_matches()
                    i += 1

                # Backspace (\x7f or \x08)
                elif text[i] in ("\x7f", "\x08"):
                    if cursor_pos > 0:
                        cursor_pos -= 1
                        buf.pop(cursor_pos)
                        dropdown_dismissed = False
                        selected_idx = 0
                        update_matches()
                    i += 1

                # Ctrl+A (Home)
                elif text[i] == "\x01":
                    cursor_pos = 0
                    i += 1

                # Ctrl+E (End)
                elif text[i] == "\x05":
                    cursor_pos = len(buf)
                    i += 1

                # Ctrl+B (Backward character / Left)
                elif text[i] == "\x02":
                    cursor_pos = max(0, cursor_pos - 1)
                    i += 1

                # Ctrl+F (Forward character / Right)
                elif text[i] == "\x06":
                    cursor_pos = min(len(buf), cursor_pos + 1)
                    i += 1

                # Ctrl+K (Kill text to end of line)
                elif text[i] == "\x0b":
                    del buf[cursor_pos:]
                    update_matches()
                    i += 1

                # Ctrl+U (Kill text to start of line)
                elif text[i] == "\x15":
                    del buf[:cursor_pos]
                    cursor_pos = 0
                    update_matches()
                    i += 1

                # Ctrl+W (Kill word backward)
                elif text[i] == "\x17":
                    orig = cursor_pos
                    while cursor_pos > 0 and buf[cursor_pos - 1] == ' ':
                        cursor_pos -= 1
                    while cursor_pos > 0 and buf[cursor_pos - 1] != ' ':
                        cursor_pos -= 1
                    del buf[cursor_pos:orig]
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
                    # If line has text, act as Delete at cursor
                    if cursor_pos < len(buf):
                        buf.pop(cursor_pos)
                        update_matches()
                    i += 1

                # Ctrl+V (\x16) or Ctrl+G (\x07): Toggle Voice Command
                elif text[i] in ("\x16", "\x07"):
                    toggle_voice()
                    i += 1
                    continue

                # Printable characters
                elif text[i] >= " ":
                    buf.insert(cursor_pos, text[i])
                    cursor_pos += 1
                    dropdown_dismissed = False
                    selected_idx = 0
                    update_matches()
                    i += 1
                else:
                    i += 1

            redraw()

    finally:
        if voice_active and recorder:
            try:
                recorder.stop()
            except Exception:
                pass
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
    cursor_pos: int = 0
    last_rendered_cursor_row = 0
    in_bracketed_paste = False
    clean_prompt_len = get_clean_prompt_len(prompt)

    def redraw():
        nonlocal last_rendered_cursor_row
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        if cols < 20:
            cols = 80
        if last_rendered_cursor_row > 0:
            sys.stdout.write(f"\033[{last_rendered_cursor_row}A")
        curr_text = "".join(buf)
        sys.stdout.write("\r\033[J" + prompt + curr_text)
        total_len = clean_prompt_len + len(curr_text)
        end_row = (total_len - 1) // cols if total_len > 0 else 0

        cursor_total = clean_prompt_len + cursor_pos
        target_row = (cursor_total - 1) // cols if cursor_total > 0 else 0
        target_col = cursor_total % cols if cols > 0 else cursor_total

        rows_up = end_row - target_row
        if rows_up > 0:
            sys.stdout.write(f"\033[{rows_up}A")
        sys.stdout.write(f"\033[{target_col + 1}G")

        last_rendered_cursor_row = target_row
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

                    seq_match = re.match(r'^\x1b(?:\[[0-9;]*[a-zA-Z~]|O[a-zA-Z])', text[i:])
                    if seq_match:
                        seq = seq_match.group(0)
                        seq_len = len(seq)
                        if seq in ("\x1b[D", "\x1bOD"):  # Left Arrow
                            cursor_pos = max(0, cursor_pos - 1)
                        elif seq in ("\x1b[C", "\x1bOC"):  # Right Arrow
                            cursor_pos = min(len(buf), cursor_pos + 1)
                        elif seq in ("\x1b[H", "\x1bOH", "\x1b[1~", "\x1b[7~"):  # Home
                            cursor_pos = 0
                        elif seq in ("\x1b[F", "\x1bOF", "\x1b[4~", "\x1b[8~"):  # End
                            cursor_pos = len(buf)
                        elif seq == "\x1b[3~":  # Delete
                            if cursor_pos < len(buf):
                                buf.pop(cursor_pos)
                        elif seq in ("\x1b[1;5D", "\x1b[5D"):  # Ctrl+Left
                            while cursor_pos > 0 and buf[cursor_pos - 1] == ' ':
                                cursor_pos -= 1
                            while cursor_pos > 0 and buf[cursor_pos - 1] != ' ':
                                cursor_pos -= 1
                        elif seq in ("\x1b[1;5C", "\x1b[5C"):  # Ctrl+Right
                            while cursor_pos < len(buf) and buf[cursor_pos] == ' ':
                                cursor_pos += 1
                            while cursor_pos < len(buf) and buf[cursor_pos] != ' ':
                                cursor_pos += 1

                        i += seq_len
                        continue
                    elif text[i:i+2] == "\x1bb":  # Alt+B
                        while cursor_pos > 0 and buf[cursor_pos - 1] == ' ':
                            cursor_pos -= 1
                        while cursor_pos > 0 and buf[cursor_pos - 1] != ' ':
                            cursor_pos -= 1
                        i += 2
                        continue
                    elif text[i:i+2] == "\x1bf":  # Alt+F
                        while cursor_pos < len(buf) and buf[cursor_pos] == ' ':
                            cursor_pos += 1
                        while cursor_pos < len(buf) and buf[cursor_pos] != ' ':
                            cursor_pos += 1
                        i += 2
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
                        if not buf or (cursor_pos > 0 and buf[cursor_pos - 1] != " "):
                            buf.insert(cursor_pos, " ")
                            cursor_pos += 1
                    else:
                        curr_text = "".join(buf)
                        total_len = clean_prompt_len + len(curr_text)
                        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
                        if cols < 20:
                            cols = 80
                        end_row = (total_len - 1) // cols if total_len > 0 else 0
                        if last_rendered_cursor_row < end_row:
                            sys.stdout.write(f"\033[{end_row - last_rendered_cursor_row}B")
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        res = curr_text.strip()
                        return res if res else default
                    i += 1

                elif text[i] in ("\x7f", "\x08"):
                    if cursor_pos > 0:
                        cursor_pos -= 1
                        buf.pop(cursor_pos)
                    i += 1

                elif text[i] == "\x01":  # Ctrl+A
                    cursor_pos = 0
                    i += 1
                elif text[i] == "\x05":  # Ctrl+E
                    cursor_pos = len(buf)
                    i += 1
                elif text[i] == "\x02":  # Ctrl+B
                    cursor_pos = max(0, cursor_pos - 1)
                    i += 1
                elif text[i] == "\x06":  # Ctrl+F
                    cursor_pos = min(len(buf), cursor_pos + 1)
                    i += 1
                elif text[i] == "\x0b":  # Ctrl+K
                    del buf[cursor_pos:]
                    i += 1
                elif text[i] == "\x15":  # Ctrl+U
                    del buf[:cursor_pos]
                    cursor_pos = 0
                    i += 1
                elif text[i] == "\x17":  # Ctrl+W
                    orig = cursor_pos
                    while cursor_pos > 0 and buf[cursor_pos - 1] == ' ':
                        cursor_pos -= 1
                    while cursor_pos > 0 and buf[cursor_pos - 1] != ' ':
                        cursor_pos -= 1
                    del buf[cursor_pos:orig]
                    i += 1

                elif text[i] >= " ":
                    buf.insert(cursor_pos, text[i])
                    cursor_pos += 1
                    i += 1
                else:
                    i += 1

            redraw()

    finally:
        sys.stdout.write("\033[?2004l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
