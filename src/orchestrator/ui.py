"""Terminal UI primitives for the chat. Inspired by Claude Code's layout, with Foreman's own look:
teal palette, a turning-circle spinner with a shimmering status line, "◆ Tool(arg)" blocks with "╰ result" lines.
Plain ANSI + termios; no dependencies.
"""

import os
import re
import shutil
import sys
import textwrap
import threading
import time
from typing import List, Optional

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TTY = sys.stdout.isatty()

RESET, BOLD, ITAL = "\033[0m", "\033[1m", "\033[3m"
ACCENT = "\033[38;5;80m"    # Foreman teal: brand, spinner, selection
ACCENT_DEEP = "\033[38;5;73m"
SHIMMER = "\033[38;5;159m"  # highlight that sweeps across the status text
FRAME = "\033[38;5;66m"     # muted teal for boxes
# Colours chosen to stay readable on dark and light backgrounds (no "dim" attribute: invisible on many themes).
GREEN, RED, YELLOW, BLUE = "\033[38;5;77m", "\033[38;5;203m", "\033[38;5;214m", "\033[38;5;75m"
GREY = "\033[38;5;248m"      # secondary text
RULE = "\033[38;5;242m"      # frame lines only
SELECT_BG = "\033[48;5;238m"
SPIN = ["◐", "◓", "◑", "◒"]             # full circle, half-filled, turning clockwise
MARK, LINK = "◆", "╰"                    # block marker, result connector


def c(text: str, *codes: str) -> str:
    return "".join(codes) + text + RESET if TTY else text


def width() -> int:
    return max(40, shutil.get_terminal_size((100, 24)).columns)


def vlen(s: str) -> int:
    return len(ANSI.sub("", s))


def short_path(path: str, limit: int) -> str:
    home = os.path.expanduser("~")
    p = "~" + path[len(home):] if path.startswith(home + "/") or path == home else path
    if len(p) <= limit:
        return p
    parts = p.split("/")
    while len(parts) > 2 and len("/".join(["…"] + parts[1:])) > limit:
        parts.pop(0)
    return "…/" + "/".join(parts[1:]) if len(parts) > 1 else p[-limit:]


def write(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


OUTPUT_LOCK = threading.RLock()  # spinner thread, scrollback and transcript output never interleave


def out(text: str = "") -> None:
    """Print transcript text, kept so the full-screen chat can scroll back over it."""
    with OUTPUT_LOCK:
        print(text, flush=True)
        SCROLL.add(text)


def clear_screen() -> None:
    with OUTPUT_LOCK:
        if TTY:
            write("\033[H\033[2J")
        SCROLL.clear()


# ---------------------------------------------------------------------- scrollback

SEQ_RE = re.compile(r"\x1b(\[<[0-9;]*[Mm]|\[[0-9;]*[A-Za-z~]|O[A-Za-z])")
PARTIAL_RE = re.compile(r"\x1b(\[[<0-9;]*|O)")
MOUSE_RE = re.compile(r"\x1b\[<(\d+);\d+;\d+[Mm]")
PAGE = 10 ** 6  # scroll amount meaning "one screen"
# Frames are written in one piece inside a synchronized update (terminals that support it show the frame only
# when complete; others ignore the mode), with the cursor hidden while it moves around. Lines are overwritten in
# place and only their leftover tail erased, never cleared first: that is what keeps the input box from flickering.
SYNC_ON, SYNC_OFF = "\033[?2026h\033[?25l", "\033[?2026l"


def frame(body: str, show_cursor: bool = True) -> None:
    write(SYNC_ON + body + ("\033[?25h" if show_cursor else "") + SYNC_OFF)


def eol(line: str, cols: int) -> str:
    """Line followed by erase-to-end-of-line, unless it ends exactly at the row's edge (the erase would eat
    its last character)."""
    w = text_width(ANSI.sub("", line))
    return line if w and w % cols == 0 else line + "\033[K"


def split_keys(data: str):
    """Split raw input into keys/escape sequences; returns (keys, incomplete trailing sequence)."""
    keys, i = [], 0
    while i < len(data):
        if data[i] == "\x1b":
            m = SEQ_RE.match(data, i)
            if m:
                keys.append(m.group(0))
                i = m.end()
                continue
            if len(data) - i > 1 and PARTIAL_RE.fullmatch(data, i):
                return keys, data[i:]
        keys.append(data[i])
        i += 1
    return keys, ""


def scroll_amount(key: str) -> Optional[int]:
    """Lines to scroll (positive = up) for the mouse wheel or PgUp/PgDn; 0 for other mouse events; None otherwise."""
    m = MOUSE_RE.fullmatch(key)
    if m:
        return {64: 3, 65: -3}.get(int(m.group(1)), 0)
    return {"\x1b[5~": PAGE, "\x1b[6~": -PAGE}.get(key)


def hard_wrap(line: str, cols: int) -> List[str]:
    """Split one printed line into the rows the terminal shows, keeping colours across the break."""
    rows, cur, used, sgr = [], "", 0, ""
    for tok in re.findall(r"\x1b\[[0-9;?]*[A-Za-z]|.", line, re.S):
        if tok.startswith("\x1b"):
            cur += tok
            if tok.endswith("m"):
                sgr = "" if tok in (RESET, "\033[m") else sgr + tok
            continue
        w = char_width(tok)
        if used + w > cols:
            rows.append(cur + RESET)
            cur, used = sgr, 0
        cur += tok
        used += w
    rows.append(cur)
    return rows


def clip(line: str, cols: int) -> str:
    """The line cut to a single terminal row, with an ellipsis when it was longer."""
    if len(hard_wrap(line, cols)) == 1:
        return line
    return hard_wrap(line, cols - 1)[0] + "…" + RESET


class Scrollback:
    """The full-screen chat runs in the terminal's alternate screen, which has no scrollback, so the chat keeps
    its own: everything printed through out()/Spinner.say is recorded, and the mouse wheel or PgUp/PgDn scroll
    the transcript while the live area (input box, spinner, menu) stays at the bottom. Typing or Esc returns
    to the bottom. Live areas register themselves with push()/pop() and provide live_height() and
    frame() (their drawing, starting at the first row of the live area). Key handlers change the offset and
    call flush() once per batch, so each repaint is a single frame."""

    def __init__(self):
        self.lines: List[str] = []
        self.offset = 0          # rows scrolled up from the bottom; 0 = following the output
        self.enabled = False     # only inside FullScreen (mouse reporting on)
        self._live: List[object] = []
        self._cache = (0, 0, [])  # (logical lines wrapped, columns, rows)
        self.dirty = False       # offset changed, repaint pending

    def add(self, text: str) -> None:
        with OUTPUT_LOCK:
            before = len(self.rows()) if self.offset else 0
            self.lines.extend(text.split("\n"))
            if self.offset:  # keep the view where the user scrolled it
                self.offset += len(self.rows()) - before
                self.render()

    def clear(self) -> None:
        self.lines, self.offset, self._cache = [], 0, (0, 0, [])

    def rows(self) -> List[str]:
        cols = max(10, shutil.get_terminal_size((100, 24)).columns)
        done, wrapped_at, rows = self._cache
        if wrapped_at != cols or done > len(self.lines):
            done, rows = 0, []
        for line in self.lines[done:]:
            rows.extend(hard_wrap(line, cols))
        self._cache = (len(self.lines), cols, rows)
        return rows

    def push(self, live) -> None:
        self._live.append(live)

    def pop(self, live) -> None:
        self.bottom()
        self.flush()
        if live in self._live:
            self._live.remove(live)

    def _region(self) -> int:
        return max(1, shutil.get_terminal_size((100, 24)).lines - self._live[-1].live_height())

    def scroll(self, amount: int) -> None:
        if not (self.enabled and self._live and amount):
            return
        with OUTPUT_LOCK:
            region, total = self._region(), len(self.rows())
            if total <= region:
                return  # everything is already on screen
            if abs(amount) >= PAGE:
                amount = max(1, region - 2) * (1 if amount > 0 else -1)
            offset = max(0, min(self.offset + amount, total - region + 1))
            if offset != self.offset:
                self.offset, self.dirty = offset, True

    def bottom(self) -> None:
        if self.offset:
            self.offset, self.dirty = 0, True

    def flush(self) -> bool:
        """Repaint once after a batch of keys (a fast wheel sends many events); True if it repainted."""
        if not self.dirty:
            return False
        self.render()
        return True

    def render(self) -> None:
        self.dirty = False
        if not (self.enabled and self._live and TTY):
            return
        with OUTPUT_LOCK:
            rows, region = self.rows(), self._region()
            if self.offset:
                end = len(rows) - self.offset
                body = rows[max(0, end - (region - 1)):end]
                body.append(c(f"  ↓ {self.offset} more line{'s' if self.offset != 1 else ''} below · "
                              f"scroll or PgDn to go down · Esc to jump back", GREY))
            else:
                body = rows[-region:]
            cols = max(10, shutil.get_terminal_size((100, 24)).columns)
            live = self._live[-1]
            frame("\033[H" + "".join(eol(r + RESET, cols) + "\r\n" for r in body) + live.frame(),
                  show_cursor=getattr(live, "shows_cursor", True))


SCROLL = Scrollback()


# ---------------------------------------------------------------------- blocks

def box(lines: List[str], title: str = "", color: str = GREY) -> str:
    w = min(width() - 2, max([vlen(l) for l in lines] + [vlen(title) + 4]) + 4)
    color = FRAME if color == GREY else color
    top = c("╭─" + (f" {title} " if title else "") + "─" * max(0, w - 3 - (vlen(title) + 2 if title else 0)) + "╮", color)
    out = [top]
    for l in lines:
        for seg in _wrap_ansi(l, w - 4):
            out.append(c("│", color) + " " + seg + " " * max(0, w - 4 - vlen(seg)) + " " + c("│", color))
    out.append(c("╰" + "─" * (w - 2) + "╯", color))
    return "\n".join(out)


def _wrap_ansi(line: str, w: int) -> List[str]:
    if vlen(line) <= w:
        return [line]
    plain = ANSI.sub("", line)
    lead = re.match(r"^\s*(?:[-•*]\s+|\d+\.\s+)?", plain).group(0)
    return textwrap.wrap(plain, w, subsequent_indent=" " * len(lead), break_on_hyphens=False) or [""]


def md(text: str) -> str:
    """Tiny markdown: headings, **bold**, `code`, bullets."""
    out = []
    for line in text.splitlines():
        h = re.match(r"^\s*#{1,6}\s+(.*)", line)
        if h:
            out.append(c(h.group(1), BOLD))
            continue
        line = re.sub(r"\[`?([^\]`]+)`?\]\((?:file://)?[^)]*\)", lambda m: c(m.group(1), BLUE), line)  # [x](url) -> x
        line = re.sub(r"\*\*(.+?)\*\*", lambda m: c(m.group(1), BOLD), line)
        line = re.sub(r"`([^`]+)`", lambda m: c(m.group(1), BLUE), line)
        line = re.sub(r"^(\s*)[-*] ", r"\1• ", line)
        out.append(line)
    return "\n".join(out)


def wrap_block(text: str, indent: str = "  ", first: str = "") -> str:
    """Wrap each line to the terminal by *visible* width (colour codes don't count), keeping bullet indents."""
    limit = width() - 1
    out = []
    for i, line in enumerate(text.splitlines() or [""]):
        lead = re.match(r"^\s*(?:[-•*]\s+|\d+\.\s+)?", ANSI.sub("", line)).group(0)
        prefix = first if i == 0 else indent
        cont = indent + " " * len(lead)
        cur, used = prefix, vlen(prefix)
        start = used
        for word in line.split(" "):
            wl = vlen(word)
            if used > start and used + 1 + wl > limit:
                out.append(cur)
                cur, used = cont + word, vlen(cont) + wl
                start = vlen(cont)
            else:
                sep = " " if used > start else ""
                cur, used = cur + sep + word, used + len(sep) + wl
        out.append(cur)
    return "\n".join(out)


def hint_line(left: str, right: str) -> str:
    """'  left ……… right' under the input; the left side is shortened if both don't fit."""
    room = width() - vlen(right) - 6
    if vlen(left) > room:
        left = left[: max(0, room - 1)] + "…" if room > 8 else ""
    gap = width() - vlen(left) - vlen(right) - 4
    return "  " + c(left, GREY) + " " * max(2, gap) + c(right, GREY)


def bullet(text: str, color: str = "") -> str:
    """'◆ text' message, continuation lines indented under the text."""
    dot = c(MARK, color or ACCENT)
    return wrap_block(md(text), indent="  ", first=dot + " ") if not ANSI.search(text) else dot + " " + text


def tool(name: str, arg: str, meta: str = "", color: str = "") -> str:
    """Blank line, then '◆ Name(arg) · meta' on one line (meta truncated to fit)."""
    room = width() - len(name) - len(arg) - 8
    if meta and len(meta) > room:
        meta = meta[: max(0, room - 1)] + "…" if room > 10 else ""
    head = f"{c(MARK, color or ACCENT)} {c(name, BOLD)}({arg})"
    return "\n" + head + (c(f" · {meta}", GREY) if meta else "")


def result(text: str, *codes: str) -> str:
    lines = textwrap.wrap(ANSI.sub("", text), width() - 6) or [""]
    codes = codes or (GREY,)
    return "\n".join(c(f"  {LINK} " if i == 0 else "    ", FRAME) + c(l, *codes) for i, l in enumerate(lines[:3]))


def user_echo(text: str) -> str:
    return "\n".join(c("> " + l if i == 0 else "  " + l, GREY) for i, l in enumerate(text.splitlines()))


# ---------------------------------------------------------------------- input

def char_width(ch: str) -> int:
    import unicodedata
    if ch == "\n":
        return 1
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(s: str) -> int:
    return sum(char_width(ch) for ch in s)


class LineEditor:
    """Claude Code-style input: prompt between two rules, hint line below, live '/' command menu, history,
    bracketed paste. Raw terminal mode; small on purpose."""

    def __init__(self, commands: List[tuple], history_path: Optional[str] = None):
        self.commands = commands            # [(command, description)]
        self.history_path = history_path
        self.history: List[str] = []
        if history_path and os.path.exists(history_path):
            with open(history_path, encoding="utf-8", errors="replace") as f:
                self.history = [l.rstrip("\n").replace("\x1f", "\n") for l in f if l.strip()][-500:]

    def _save(self, line: str) -> None:
        if not line.strip() or (self.history and self.history[-1] == line):
            return
        self.history.append(line)
        if self.history_path:
            try:
                with open(self.history_path, "a", encoding="utf-8") as f:
                    f.write(line.replace("\n", "\x1f") + "\n")
            except OSError:
                pass

    def matches(self, buf: str) -> List[tuple]:
        if not buf.startswith("/") or " " in buf:
            return []
        return [cmd for cmd in self.commands if cmd[0].startswith(buf)]

    def read(self, hint_left: str = "", hint_right: str = "") -> str:
        if not (TTY and sys.stdin.isatty()):
            return input("> ")
        import codecs
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[0] &= ~(termios.IXON | termios.ICRNL)
        new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
        new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
        dec = codecs.getincrementaldecoder("utf-8")("ignore")
        self.buf, self.cur, self.sel, self.row = "", 0, 0, 0
        self.hist_i = len(self.history)
        self.hints = (hint_left, hint_right)
        write(c("─" * width(), ACCENT_DEEP) + "\n\033[?2004h")
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        pending = ""
        SCROLL.push(self)
        try:
            self._draw()
            while True:
                pending += dec.decode(os.read(fd, 4096))
                while pending:
                    before = pending
                    pending, result = self._key(pending)
                    if result is not None:
                        SCROLL.pop(self)
                        self._finish()
                        if result is KeyboardInterrupt:
                            raise KeyboardInterrupt
                        if result is EOFError:
                            raise EOFError
                        self._save(result)
                        return result
                    if pending is None:  # incomplete escape sequence: wait for the rest
                        pending = before
                        break
                with OUTPUT_LOCK:
                    if not SCROLL.flush():  # a scroll repaint already includes the input box
                        self._draw()
        finally:
            SCROLL.pop(self)
            write("\033[?2004l")
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    # live area for Scrollback: top rule, input rows, bottom rule, hint/menu rows
    def live_height(self) -> int:
        total = 2 + text_width(self.buf.replace("\n", "⏎"))
        return 2 + max(1, -(-total // width())) + len(self._below())

    def frame(self) -> str:
        self.row = 0
        return c("─" * width(), ACCENT_DEEP) + "\r\n" + self._frame(fresh=True)

    # key handling: returns (remaining input, result-or-None); remaining None = incomplete escape sequence
    def _key(self, s: str):
        menu = self.matches(self.buf)
        if s.startswith("\x1b[200~"):
            end = s.find("\x1b[201~")
            if end == -1:
                return None, None
            self._insert(s[6:end].replace("\r\n", "\n").replace("\r", "\n"))
            return s[end + 6:], None
        if s.startswith("\x1b"):
            m = SEQ_RE.match(s)
            if not m:
                if len(s) == 1:  # lone Esc: back to the bottom if scrolled, else clear the line (and / menu)
                    if SCROLL.offset:
                        SCROLL.bottom()
                    else:
                        self.buf, self.cur = "", 0
                    return "", None
                return (None, None) if PARTIAL_RE.fullmatch(s) else (s[1:], None)
            seq, rest = m.group(1), s[len(m.group(0)):]
            amount = scroll_amount(m.group(0))
            if amount is not None:  # mouse wheel / PgUp / PgDn scroll the transcript; other clicks are ignored
                SCROLL.scroll(amount)
                return rest, None
            SCROLL.bottom()
            if seq in ("[A", "OA"):
                if menu:
                    self.sel = (self.sel - 1) % len(menu)
                elif self.hist_i > 0:
                    self.hist_i -= 1
                    self.buf = self.history[self.hist_i]
                    self.cur = len(self.buf)
            elif seq in ("[B", "OB"):
                if menu:
                    self.sel = (self.sel + 1) % len(menu)
                elif self.hist_i < len(self.history):
                    self.hist_i += 1
                    self.buf = self.history[self.hist_i] if self.hist_i < len(self.history) else ""
                    self.cur = len(self.buf)
            elif seq in ("[C", "OC"):
                self.cur = min(len(self.buf), self.cur + 1)
            elif seq in ("[D", "OD"):
                self.cur = max(0, self.cur - 1)
            elif seq in ("[H", "OH", "[1~"):
                self.cur = 0
            elif seq in ("[F", "OF", "[4~"):
                self.cur = len(self.buf)
            elif seq == "[3~":
                self.buf = self.buf[:self.cur] + self.buf[self.cur + 1:]
            return rest, None
        ch, rest = s[0], s[1:]
        SCROLL.bottom()
        if ch in ("\r", "\n"):
            if menu and self.buf != menu[self.sel % len(menu)][0]:
                self.buf = menu[self.sel % len(menu)][0]
            return rest, self.buf
        if ch == "\t":
            if menu:
                self.buf = menu[self.sel % len(menu)][0] + " "
                self.cur = len(self.buf)
            return rest, None
        if ch == "\x03":
            if self.buf:
                self.buf, self.cur = "", 0
                return rest, None
            return rest, KeyboardInterrupt
        if ch == "\x04":
            if not self.buf:
                return rest, EOFError
            self.buf = self.buf[:self.cur] + self.buf[self.cur + 1:]
            return rest, None
        if ch in ("\x7f", "\x08"):
            if self.cur:
                self.buf = self.buf[:self.cur - 1] + self.buf[self.cur:]
                self.cur -= 1
        elif ch == "\x01":
            self.cur = 0
        elif ch == "\x05":
            self.cur = len(self.buf)
        elif ch == "\x15":
            self.buf, self.cur = self.buf[self.cur:], 0
        elif ch == "\x0b":
            self.buf = self.buf[:self.cur]
        elif ch == "\x17":
            left = self.buf[:self.cur].rstrip()
            cut = left.rfind(" ") + 1
            self.buf, self.cur = self.buf[:cut] + self.buf[self.cur:], cut
        elif ch >= " ":
            self._insert(ch)
            self.sel = 0
        return rest, None

    def _insert(self, text: str) -> None:
        self.buf = self.buf[:self.cur] + text + self.buf[self.cur:]
        self.cur += len(text)

    def _below(self) -> List[str]:
        menu = self.matches(self.buf)
        if menu:
            w = max(len(cmd) for cmd, _ in menu) + 3
            rows = []
            for i, (cmd, desc) in enumerate(menu[:8]):
                line = f"  {cmd.ljust(w)}{desc}"[: width() - 1]
                rows.append(c("›" + line[1:], BOLD, ACCENT) if i == self.sel % len(menu) else c(line, GREY))
            return rows
        return [hint_line(*self.hints)]

    def _draw(self) -> None:
        frame(self._frame())

    def _frame(self, fresh: bool = False) -> str:
        """Input rows, bottom rule and hint/menu, overwritten in place; leaves the cursor in the input."""
        cols = width()
        shown = self.buf.replace("\n", "⏎")
        total = 2 + text_width(shown)
        text_rows = max(1, -(-total // cols))
        below = self._below()
        up = f"\033[{self.row}A" if self.row and not fresh else ""
        out = [up, "\r", c(">", BOLD, ACCENT), " ", shown, "" if total % cols == 0 else "\033[K", "\r\n",
               c("─" * cols, ACCENT_DEEP), "\r\n", "\r\n".join(eol(b, cols) for b in below), "\033[J"]
        target = 2 + text_width(shown[: self.cur])
        trow, tcol = target // cols, target % cols
        out.append(f"\033[{text_rows + len(below) - trow}A\r" + (f"\033[{tcol}C" if tcol else ""))
        self.row = trow
        return "".join(out)

    def _finish(self) -> None:
        write((f"\033[{self.row}A" if self.row else "") + "\r\033[1A\033[J")


_EDITOR: Optional[LineEditor] = None


def set_editor(editor: LineEditor) -> None:
    global _EDITOR
    _EDITOR = editor


def read_input(hint_left: str = "", hint_right: str = "") -> str:
    """Read one message with the shared editor (plain input() when not interactive)."""
    editor = _EDITOR or LineEditor([])
    return editor.read(hint_left, hint_right)


def choose(title: str, body: List[str], options: List[str], allow_text: bool = True, start: int = 0,
           color: str = YELLOW, esc: str = "decide later") -> Optional[str]:
    """Arrow-key / number menu. Returns the chosen option, typed text ("" when skipped), or None on Esc."""
    if not TTY or not sys.stdin.isatty():
        return None
    import termios
    import tty as _tty
    items = options + (["Type something else…"] if allow_text else [])
    sel = max(0, min(start, len(items) - 1))
    out(box(body, title, color))

    def visible() -> int:
        """Items shown at once: all of them, or a window that fits the terminal (a menu taller than the screen
        can't be redrawn in place, and a wrapped row would make every redraw drift)."""
        return max(1, min(len(items), shutil.get_terminal_size((100, 24)).lines - 2))

    def lines() -> List[str]:
        n, cols = visible(), width()
        top = max(0, min(sel - n // 2, len(items) - n))
        rows = []
        for i in range(top, top + n):
            mark = c("›", ACCENT) if i == sel else " "
            label = c(f"{i + 1}. {items[i]}", BOLD) if i == sel else f"{i + 1}. {items[i]}"
            rows.append(clip(f" {mark} {label}", cols))
        more = f"{sel + 1}/{len(items)} · " if n < len(items) else ""
        return rows + [clip(c(f"   {more}↑/↓ or number, Enter to choose · Esc to {esc}", GREY), cols)]

    def body() -> str:
        return "".join("\r" + eol(l, width()) + "\n" for l in lines())

    drawn = [0]

    def draw(first=False):
        with OUTPUT_LOCK:
            up = "" if first else f"\033[{drawn[0]}A"
            drawn[0] = visible() + 1
            frame(up + body())

    class _Live:  # the menu stays at the bottom while the transcript above scrolls
        live_height = staticmethod(lambda: visible() + 1)
        frame = staticmethod(body)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    live, pending, cancelled = _Live(), "", False
    draw(first=True)
    SCROLL.push(live)
    try:
        _tty.setcbreak(fd)
        done = False
        while not done:
            keys, pending = split_keys(pending + os.read(fd, 64).decode("utf-8", "ignore"))
            for key in keys:
                amount = scroll_amount(key)
                if amount is not None:
                    SCROLL.scroll(amount)
                    continue
                if SCROLL.offset:
                    SCROLL.bottom()
                    if key == "\x1b":
                        continue
                if key in ("\x1b[A", "\x1bOA", "k"):
                    sel = (sel - 1) % len(items)
                elif key in ("\x1b[B", "\x1bOB", "j"):
                    sel = (sel + 1) % len(items)
                elif key.isdigit() and 1 <= int(key) <= len(items):
                    sel = int(key) - 1
                    done = True
                elif key in ("\r", "\n"):
                    done = True
                elif key in ("\x1b", "\x03"):
                    cancelled = done = True
                if done:
                    break
            if not SCROLL.flush():
                draw()
    finally:
        SCROLL.pop(live)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        SCROLL.add("\n".join(lines()))
    if cancelled:
        return None
    if allow_text and sel == len(items) - 1:
        try:
            return read_input("type your answer · Esc/Ctrl+C to cancel").strip()
        except (KeyboardInterrupt, EOFError):
            return None
    return items[sel]


# ---------------------------------------------------------------------- spinner

def shimmer(text: str, tick: int) -> str:
    """Base teal text with a brighter 3-character band moving left to right."""
    if not TTY:
        return text
    pos = (tick // 1) % (len(text) + 8) - 4
    out = []
    for i, ch in enumerate(text):
        out.append((SHIMMER if abs(i - pos) <= 1 else ACCENT_DEEP) + ch)
    return "".join(out) + RESET


class FullScreen:
    """Run inside the terminal's alternate screen (like vim/htop): on exit the shell view is restored as it was.
    The alternate screen has no scrollback, so the chat captures the mouse wheel and scrolls itself (Scrollback);
    selecting text with the mouse then needs Shift."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and TTY

    def __enter__(self):
        if self.enabled:
            SCROLL.clear()
            SCROLL.enabled = True
            write("\033[?1049h\033[H\033[2J\033[?1000h\033[?1006h")  # alt screen; wheel arrives as mouse events
        return self

    def __exit__(self, *exc):
        if self.enabled:
            SCROLL.enabled = False
            write("\033[?1000l\033[?1006l\033[?25h\033[?2004l\033[?1049l")
        return False


class Spinner:
    """Animated status line with a shimmer, a blank line, and the input box kept visible underneath
    (type-ahead: Enter queues a message that is sent when the current work finishes). Messages print above."""

    def __init__(self, verb: str = "Thinking", hint_right: str = "", footer: bool = True):
        self.verb = verb
        self.who = ""  # active provider, shown next to the timer
        self.hint_right = hint_right
        self.footer = footer and TTY
        self.start = time.time()
        self.typed = ""
        self.queued: List[str] = []
        self._lock = OUTPUT_LOCK
        self._stop = threading.Event()
        self._paused = False
        self._height = 0
        self._tick = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self.begin()
        return self

    def __exit__(self, *a):
        self.end()

    def begin(self):
        if TTY:
            write("\033[?25l")  # hide the real cursor; the box draws its own
            self._thread.start()
            if self.footer:
                SCROLL.push(self)

    # live area for Scrollback
    def live_height(self) -> int:
        return len(self._lines())

    shows_cursor = False

    def frame(self) -> str:
        if self._paused:
            return ""
        self._height = 0
        return self._frame()

    def _lines(self) -> List[str]:
        secs, w = int(time.time() - self.start), width()
        meta = (f"{self.who} · " if self.who else "") + f"{secs}s · ctrl+c to interrupt"
        if len(self.verb) + len(meta) + 6 > w:  # one row, always: a wrapped status line makes the box jump
            meta = (f"{self.who} · " if self.who else "") + f"{secs}s"
        verb = self.verb if len(self.verb) + len(meta) + 6 <= w else self.verb[: max(4, w - len(meta) - 8)] + "…"
        status = f"{c(SPIN[(self._tick // 2) % len(SPIN)], ACCENT, BOLD)} {shimmer(verb + '…', self._tick)} " \
                 f"{c(f'({meta})', GREY)}"
        if not self.footer:
            return [status]
        shown = self.typed.replace("\n", "⏎")[-(w - 4):]
        if self.queued:
            left = f"queued ({len(self.queued)}): " + " · ".join(q[:30] for q in self.queued)
        else:
            left = "type your next message; it's sent when this finishes"
        return [status, "", c("─" * w, ACCENT_DEEP), c(">", BOLD, ACCENT) + " " + shown + "\033[7m \033[0m",
                c("─" * w, ACCENT_DEEP), hint_line(left, self.hint_right)]

    def _clear(self):
        if self._height:
            write((f"\033[{self._height - 1}A" if self._height > 1 else "") + "\r\033[J")
            self._height = 0

    def _frame(self) -> str:
        """Overwrite the previous status/footer in place (moving up to its first row), erase only leftovers."""
        lines, cols = self._lines(), width()
        up = f"\033[{self._height - 1}A" if self._height > 1 else ""
        self._height = len(lines)
        return up + "\r" + "\r\n".join(eol(l, cols) for l in lines) + "\033[J"

    def _draw(self):
        frame(self._frame(), show_cursor=False)

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                if not self._paused:
                    self._draw()
            self._tick += 1
            time.sleep(0.12)

    def key(self, data: str) -> None:
        """Type-ahead editing: printable text, Backspace, Enter (queue), Ctrl+U (clear)."""
        with self._lock:
            for ch in split_keys(data)[0]:
                amount = scroll_amount(ch)
                if amount is not None:
                    SCROLL.scroll(amount)
                    continue
                SCROLL.bottom()
                if ch in ("\r", "\n"):
                    if self.typed.strip():
                        self.queued.append(self.typed.strip())
                    self.typed = ""
                elif ch in ("\x7f", "\x08"):
                    self.typed = self.typed[:-1]
                elif ch == "\x15":
                    self.typed = ""
                elif ch >= " " and not ch.startswith("\x1b"):
                    self.typed += ch
            if SCROLL.flush():
                return
            if not self._paused and TTY:
                self._draw()

    def set(self, verb: str, who: Optional[str] = None):
        self.verb = verb
        if who is not None:
            self.who = who

    def say(self, msg: str):
        with self._lock:
            if SCROLL.offset:  # the user is reading further up: record it, the view stays put
                SCROLL.add(msg)
                return
            SCROLL.add(msg)
            if not TTY:
                write(msg + "\n")
                return
            if self._paused or self._stop.is_set():
                self._clear()
                write(msg + "\n")
                return
            # one frame: the message takes the footer's place, the footer is drawn again right under it
            cols = width()
            up = f"\033[{self._height - 1}A" if self._height > 1 else ""
            self._height = 0
            frame(up + "\r" + "\r\n".join(eol(l, cols) for l in msg.split("\n")) + "\r\n" + self._frame(),
                  show_cursor=False)

    def pause(self):
        with self._lock:
            self._paused = True
            if TTY:
                self._clear()
                write("\033[?25h")

    def resume(self, verb: Optional[str] = None):
        with self._lock:
            self.verb = verb or self.verb
            self._paused = False
            if TTY:
                write("\033[?25l")

    def end(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(0.5)
        SCROLL.pop(self)
        with self._lock:
            if TTY:
                self._clear()
                write("\033[?25h")
