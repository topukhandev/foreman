"""Terminal UI primitives for the chat. Inspired by Claude Code's layout, with Foreman's own look:
teal palette, a turning-circle spinner with a shimmering status line, "◆ Tool(arg)" blocks with "╰ result" lines.
Plain ANSI + termios; no dependencies.
"""

import base64
import codecs
import os
import re
import shutil
import sys
import textwrap
import threading
import time
from typing import List, Optional

from orchestrator.clipboard import (CHIP_RE, MAX_PASTE_LINES, Attachments, clipboard_image, copy_text, dropped_files,
                                    dropped_images)

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


def set_title(text: str) -> None:
    """The terminal window/tab title (FullScreen saves the previous one and puts it back on exit)."""
    if TTY:
        write("\033]0;" + re.sub(r"[\x00-\x1f\x7f]", " ", text)[:120] + "\007")


def clear_screen() -> None:
    with OUTPUT_LOCK:
        if TTY:
            write("\033[H\033[2J")
        SCROLL.clear()


class busy:
    """`with busy("Asking x for its models…"):` a one-line notice that is wiped when the work is done (not kept in the
    transcript)."""

    def __init__(self, text: str):
        self.text = text

    def __enter__(self):
        if TTY:
            write("\r" + clip(c(f"  {self.text}", GREY), width()) + "\033[K")

    def __exit__(self, *exc):
        if TTY:
            write("\r\033[K")
        return False


# ---------------------------------------------------------------------- scrollback

SEQ_RE = re.compile(r"\x1b(\[<[0-9;]*[Mm]|\[[0-9;]*[A-Za-z~]|O[A-Za-z])")
PARTIAL_RE = re.compile(r"\x1b(\[[<0-9;]*|O)")
MOUSE_RE = re.compile(r"\x1b\[<(\d+);\d+;\d+[Mm]")
MOUSE_FULL_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
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


def cells(row: str) -> List[tuple]:
    """(character, first column, width) for every visible character of a row."""
    out, col = [], 0
    for ch in ANSI.sub("", row):
        w = char_width(ch)
        out.append((ch, col, w))
        col += w
    return out


def highlight(row: str, first: int, last: int) -> str:
    """The row with the characters in columns first..last (inclusive) in reverse video."""
    out, col, on = [], 0, False
    for tok in re.findall(r"\x1b\[[0-9;?]*[A-Za-z]|.", row, re.S):
        if tok.startswith("\x1b"):
            out.append(tok)
            if on and tok in (RESET, "\033[m"):  # a reset inside the selection must not end it
                out.append("\033[7m")
            continue
        inside = first <= col <= last
        if inside != on:
            out.append("\033[7m" if inside else "\033[27m")
            on = inside
        out.append(tok)
        col += char_width(tok)
    return "".join(out) + ("\033[27m" if on else "")


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
        self.details: List[str] = []  # the same transcript with shortened outputs in full (the Ctrl+O view)
        self.offset = 0          # rows scrolled up from the bottom; 0 = following the output
        self.unseen = 0          # messages that arrived while scrolled up
        self._sticky: Optional[int] = None  # row of the prompt shown pinned at the top while scrolled up
        self.enabled = False     # only inside FullScreen (mouse reporting on)
        self._live: List[object] = []
        self._cache = (0, 0, [], [])  # (logical lines wrapped, columns, rows, row continues the previous one)
        self.dirty = False       # offset changed, repaint pending
        self.select_enabled = False  # mouse selection with copy to the clipboard (config ui.copy_on_select)
        self.sel: Optional[tuple] = None   # ((row, col), (row, col)) on screen, while a selection is highlighted
        self._anchor: Optional[tuple] = None
        self._shown: List[str] = []        # transcript rows currently on screen (top to bottom)
        self._shown_cont: List[bool] = []

    def add(self, text: str) -> None:
        with OUTPUT_LOCK:
            if self.offset and text.strip():
                self.unseen += 1  # counted for the "N new messages" bar
            before = len(self.rows()) if self.offset else 0
            self.lines.extend(text.split("\n"))
            self.details.extend((getattr(text, "full", "") or text).split("\n"))
            del self.details[:-20000]
            self.sel = self._anchor = None  # the rows moved
            if self.offset:  # keep the view where the user scrolled it
                self.offset += len(self.rows()) - before
                self.render()

    def clear(self) -> None:
        self.lines, self.details, self.offset, self._cache = [], [], 0, (0, 0, [], [])
        self.sel = self._anchor = None

    def rows(self) -> List[str]:
        cols = max(10, shutil.get_terminal_size((100, 24)).columns)
        done, wrapped_at, rows, conts = self._cache
        if wrapped_at != cols or done > len(self.lines):
            done, rows, conts = 0, [], []
        for line in self.lines[done:]:
            pieces = hard_wrap(line, cols)
            rows.extend(pieces)
            conts.extend([False] + [True] * (len(pieces) - 1))
        self._cache = (len(self.lines), cols, rows, conts)
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
                self.sel = self._anchor = None

    def bottom(self) -> None:
        if self.sel:  # any key ends the highlight
            self.sel, self.dirty = None, True
        if self.offset:
            self.offset, self.dirty, self.unseen = 0, True, 0

    def flush(self) -> bool:
        """Repaint once after a batch of keys (a fast wheel sends many events); True if it repainted."""
        if not self.dirty:
            return False
        self.render()
        return True

    def _visible(self) -> List[str]:
        """Transcript rows that are on screen, top to bottom (also remembered for mouse selection)."""
        rows, region, conts = self.rows(), self._region(), self._cache[3]
        self._sticky = None
        if self.offset:
            end = len(rows) - self.offset
            first = max(0, end - (region - 1))
            prompt = self._prompt_above(first, rows, conts)
            if prompt is not None and end - first > 1:  # the top row pins the message you were reading about
                self._sticky = prompt
                pinned = c("↑ " + ANSI.sub("", rows[prompt])[:width() - 3], GREY, ITAL)
                rows, conts = [pinned] + rows[first + 1:end], [False] + conts[first + 1:end]
            else:
                rows, conts = rows[first:end], conts[first:end]
        else:
            rows, conts = rows[-region:], conts[-region:]
        self._shown, self._shown_cont = list(rows), list(conts)
        return list(rows)

    @staticmethod
    def _prompt_above(first: int, rows: List[str], conts: List[bool]) -> Optional[int]:
        """Row of the last message you sent that starts above row `first` (your messages are the '> ' lines)."""
        for i in range(first - 1, -1, -1):
            if not conts[i] and ANSI.sub("", rows[i]).startswith("> "):
                return i
        return None

    def jump_bar(self) -> str:
        """The bar under the transcript while scrolled up."""
        new = f" · {self.unseen} new message{'s' if self.unseen != 1 else ''}" if self.unseen else ""
        return c(f"  ↓ Jump to bottom (Esc or click){new} · {self.offset} line{'s' if self.offset != 1 else ''} below",
                 BOLD, ACCENT)

    def render(self) -> None:
        self.dirty = False
        if not (self.enabled and self._live and TTY):
            return
        with OUTPUT_LOCK:
            body = self._visible()
            if self.offset:
                body.append(self.jump_bar())
            if self.sel:
                body = [self._highlighted(i, r) for i, r in enumerate(body)]
            cols = max(10, shutil.get_terminal_size((100, 24)).columns)
            live = self._live[-1]
            frame("\033[H" + "".join(eol(r + RESET, cols) + "\r\n" for r in body) + live.frame(),
                  show_cursor=getattr(live, "shows_cursor", True))


    # ---- mouse selection: press, drag, release copies the highlighted text
    def _span(self, i: int):
        """Columns (first, last) selected in screen row i, or None."""
        (r0, c0), (r1, c1) = sorted(self.sel)
        if not r0 <= i <= r1:
            return None
        return (c0 if i == r0 else 0, c1 if i == r1 else 10 ** 6)

    def _highlighted(self, i: int, row: str) -> str:
        span = self._span(i) if i < len(self._shown) else None
        return highlight(row, *span) if span else row

    def selected_text(self) -> str:
        parts = []
        for i, row in enumerate(self._shown):
            span = self._span(i)
            if span is None:
                continue
            text = "".join(ch for ch, col, _w in cells(row) if span[0] <= col <= span[1]).rstrip()
            if parts and self._shown_cont[i]:
                parts[-1] += text  # a wrapped line is one line again
            else:
                parts.append(text)
        return "\n".join(parts).strip("\n")

    def mouse(self, key: str) -> bool:
        """Left-button press / drag / release over the transcript selects text; releasing copies it. False for
        everything else (the wheel scrolls through scroll())."""
        m = MOUSE_FULL_RE.fullmatch(key)
        if not (m and self._live):
            return False
        button, x, y, kind = int(m.group(1)), int(m.group(2)) - 1, int(m.group(3)) - 1, m.group(4)
        if button >= 64:
            return False
        if kind == "M" and not button & 35 and self.offset:  # a click on the jump bar or the pinned message
            self._visible()
            if y == len(self._shown):
                self.bottom()
                return True
            if y == 0 and self._sticky is not None:  # scroll so that message is at the top
                self.offset = max(1, len(self.rows()) - self._sticky - (self._region() - 1))
                self.dirty, self.sel, self._anchor = True, None, None
                return True
        if not self.select_enabled:
            return False
        if button & 3 != 0:
            return True  # other buttons are ignored
        self._visible()
        if not self._shown:
            return True
        point = (max(0, min(y, len(self._shown) - 1)), max(0, x))
        if kind == "M" and not button & 32:  # press
            self._anchor = point if y < len(self._shown) else None
            if self.sel:
                self.sel, self.dirty = None, True
        elif kind == "M" and self._anchor:  # drag
            self.sel, self.dirty = (self._anchor, point), True
        elif kind == "m" and self._anchor:  # release
            if self.sel:
                text = self.selected_text()
                if text:
                    self.toast(copy_message(text))
                else:
                    self.sel, self.dirty = None, True
            self._anchor = None
        return True

    def live_top(self) -> int:
        """Screen row (0-based) of the live area's top rule: the rows above are the transcript."""
        return len(self._visible())

    def toast(self, message: str) -> None:
        """A one-line notice in place of the live area's hint (it goes away with the next key)."""
        if self._live:
            self._live[-1].notice = message
            self.dirty = True


def copy_message(text: str) -> str:
    n = len(text)
    if copy_text(text):
        return f"{n:,} character{'s' if n != 1 else ''} copied to clipboard"
    write("\033]52;c;" + base64.b64encode(text.encode("utf-8")).decode() + "\a")  # terminals that allow it
    return (f"{n:,} character{'s' if n != 1 else ''} sent to the terminal's clipboard; if nothing arrives, install xclip or "
            f"wl-clipboard (Shift+drag selects with the terminal itself)")


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
    out, fenced = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):  # a code block: dim, without the fences
            fenced = not fenced
            continue
        if fenced:
            out.append(c("  " + line, GREY))
            continue
        h = re.match(r"^(\s*)#{1,6}\s+(.*)", line)
        if h:
            out.append(h.group(1) + c(h.group(2), BOLD, ACCENT))
            continue
        quote = re.match(r"^\s*>\s?(.*)", line)
        if quote:
            out.append(c("│ " + quote.group(1), GREY))
            continue
        line = re.sub(r"\[`?([^\]`]+)`?\]\((?:file://)?[^)]*\)", lambda m: c(m.group(1), BLUE), line)  # [x](url) -> x
        line = re.sub(r"\*\*(.+?)\*\*", lambda m: c(m.group(1), BOLD), line)
        line = re.sub(r"`([^`]+)`", lambda m: c(m.group(1), BLUE), line)
        line = re.sub(r"^(\s*)[-*] ", lambda m: m.group(1) + c("•", ACCENT) + " ", line)
        line = re.sub(r"^(\s*)(\d+\.) ", lambda m: m.group(1) + c(m.group(2), ACCENT) + " ", line)
        out.append(line)
    return "\n".join(out)


# Status words coloured wherever a state is shown (task lists, reports, lifecycle).
STATUS_COLORS = [
    (GREEN, r"done|answered|approved|approve|passed|ready|confirmed|ok|closed"),
    (YELLOW, r"needs input|waiting for you|waiting|blocked|paused|partial|pending|planned|in progress|revise|skipped"),
    (RED, r"failed|FAILED|stopped|interrupted|error|not installed|cancelled"),
    (BLUE, r"running|planning|queued|started"),
]
_STATUS_RE = [(color, re.compile(rf"(?<![\w-])({words})(?![\w-])")) for color, words in STATUS_COLORS]


def emphasize(text: str, statuses: bool = False) -> str:
    """Inline highlights for plain text: task numbers (#12), short "quoted" commands, → arrows, ✔/✘ marks and dates;
    with statuses=True also state words (done, failed, needs input…) in their colour. Text that already has colour
    codes is left alone."""
    if ANSI.search(text):
        return text
    text = re.sub(r"(?<![\w#])#\d+\b", lambda m: c(m.group(0), BOLD, ACCENT), text)
    if not re.search(r'"\s*:\s*["{\[\d]', text):  # "commands" in prose; JSON keys and values stay plain
        text = re.sub(r"\"([^\"\n]{1,30})\"", lambda m: c(f'"{m.group(1)}"', BLUE), text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b", lambda m: c(m.group(0), GREY), text)
    text = text.replace("→", c("→", ACCENT)).replace("✔", c("✔", GREEN)).replace("✘", c("✘", RED))
    if statuses:
        for color, rx in _STATUS_RE:
            text = rx.sub(lambda m, col=color: c(m.group(1), col), text)
    return text


def pretty(text: str) -> str:
    """Plain text from a command, made readable: 'Heading:' lines bold, 'Label: …' labels bold, '  key   value' rows
    with the key in grey, bullets/bold/code as Markdown, plus emphasize(). Already coloured lines stay as they are."""
    out = []
    for line in text.split("\n"):
        if ANSI.search(line) or not line.strip():
            out.append(line)
            continue
        head = re.match(r"^(\S[^:]{0,60}):$", line)
        label = re.match(r"^(\s*)([A-Z][\w '/&-]{0,24}):(\s+)(.*)$", line)
        row = re.match(r"^(\s{2,})(\S.{0,28}?)(\s{2,})(\S.*)$", line)
        if head:
            out.append(c(head.group(1), BOLD, ACCENT))
        elif row and not re.match(r"^\s*([-•*]|\d+\.)\s", line):
            out.append(row.group(1) + c(row.group(2), BOLD) + row.group(3) + md(emphasize(row.group(4), True)))
        elif label:
            out.append(label.group(1) + c(label.group(2) + ":", BOLD) + label.group(3) + md(emphasize(label.group(4))))
        else:
            out.append(md(emphasize(line)))
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
    return wrap_block(pretty(text), indent="  ", first=dot + " ") if not ANSI.search(text) else dot + " " + text


def tool(name: str, arg: str, meta: str = "", color: str = "") -> str:
    """Blank line, then '◆ Name(arg) · meta' on one line (meta truncated to fit)."""
    room = width() - len(name) - len(arg) - 8
    if meta and len(meta) > room:
        meta = meta[: max(0, room - 1)] + "…" if room > 10 else ""
    head = f"{c(MARK, color or ACCENT)} {c(name, BOLD)}({arg})"
    return "\n" + head + (c(f" · {meta}", GREY) if meta else "")


class Expandable(str):
    """Transcript text shown shortened; `.full` is the whole of it, for the details view (Ctrl+O)."""
    full = ""


def join(*parts: str) -> str:
    """Lines joined, keeping the full form of any shortened part."""
    text = Expandable("\n".join(parts))
    text.full = "\n".join(getattr(p, "full", "") or p for p in parts)
    return text


def result(text: str, *codes: str, limit: int = 3) -> str:
    """'╰ text' under a block, wrapped; past `limit` rows it ends with '… +N lines (ctrl+o to expand)'."""
    lines = [w for part in ANSI.sub("", text).splitlines() or [""] for w in textwrap.wrap(part, width() - 6) or [""]]
    codes = codes or (GREY,)

    def show(rows: List[str]) -> str:
        return "\n".join(c(f"  {LINK} " if i == 0 else "    ", FRAME) + c(l, *codes) for i, l in enumerate(rows))
    if len(lines) <= limit:
        return show(lines)
    out = Expandable(show(lines[:limit]) + "\n" + c(f"    … +{len(lines) - limit} lines (ctrl+o to expand)", GREY))
    out.full = show(lines)
    return out


def call(text: str, first: bool = True, where: str = "") -> str:
    """A worker's tool call under its step, Claude Code-style: '╰ Read(src/app.py)' for the first one, the following
    ones aligned under it; the tool name stands out, its argument is grey. Text starting with '· ' is the model's
    own words between calls (grey italic). where: a '[project] ' tag when several steps run at once."""
    m = re.match(r"(\w+)\((.*)\)$", text, re.S)
    body = c(m.group(1), BOLD) + c(f"({m.group(2)})", GREY) if m else c(text.lstrip("· "), GREY, ITAL)
    return clip(c(f"  {LINK} " if first else "    ", FRAME) + (c(where, GREY) if where else "") + body, width())


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


class Typed(str):
    """What the user submitted, in full (pastes expanded); `.shown` is the compact form with chips such as
    `[Pasted text #1 +545 lines]`, for the transcript."""
    shown = ""


class LineEditor:
    """Claude Code-style input: prompt between two rules, hint line below, live '/' command menu and '@' file menu,
    history, bracketed paste, several lines (`\\` then Enter starts a new one). Long pastes, images and other files
    become chips (see clipboard.py): drag a file in, or press Ctrl+V with an image on the clipboard. A suggested next
    message can wait in grey in the empty input (Tab or → takes it). Raw terminal mode; small on purpose."""

    def __init__(self, commands: List[tuple], history_path: Optional[str] = None, image_dir: Optional[str] = None,
                 root: Optional[str] = None):
        self.commands = commands            # [(command, description)]
        self.image_dir = image_dir or os.path.join(os.path.expanduser("~"), ".foreman", "images")
        self.root = root or os.getcwd()     # where '@' looks for files
        self._files: Optional[List[str]] = None
        self.attach = Attachments()
        self.killed = ""                    # text removed by Ctrl+K / Ctrl+U / Ctrl+W, back with Ctrl+Y
        self.suggestion = ""                # grey suggested message in the empty input (Tab or → takes it)
        self.status = ""                    # the status line under the hint ("" = off)
        self._menu_top = 0                  # first row of the '/' or '@' menu on screen (it scrolls)
        self.notice = ""                    # one-line message in place of the hint until the next key
        self.span: Optional[tuple] = None   # (start, end) of the part of the input selected with the mouse; end exclusive
        self._anchor: Optional[int] = None
        self._dragged = False
        self.history_path = history_path
        self.history: List[str] = []
        if history_path and os.path.exists(history_path):
            with open(history_path, encoding="utf-8", errors="replace") as f:
                self.history = [l.rstrip("\n").replace("\x1f", "\n") for l in f if l.strip()][-500:]

    def _save(self, line: str, persist: bool = True) -> None:
        if not line.strip() or (self.history and self.history[-1] == line):
            return
        self.history.append(line)
        if self.history_path and persist:
            try:
                with open(self.history_path, "a", encoding="utf-8") as f:
                    f.write(line.replace("\n", "\x1f") + "\n")
            except OSError:
                pass

    def matches(self, buf: str) -> List[tuple]:
        if not buf.startswith("/") or " " in buf:
            return []
        return [cmd for cmd in self.commands if cmd[0].startswith(buf)]

    MENTION_RE = re.compile(r"(?:^|(?<=\s))@([^\s@]*)$")

    def mention(self) -> Optional[re.Match]:
        """The `@partial/path` being typed at the cursor, if any."""
        return self.MENTION_RE.search(self.buf[:self.cur])

    def files(self) -> List[str]:
        """Files and folders under root, relative (git's list when it is a repository: ignored files left out)."""
        if self._files is None:
            import subprocess
            found: List[str] = []
            try:
                res = subprocess.run(["git", "-C", self.root, "ls-files", "--cached", "--others", "--exclude-standard"],
                                     capture_output=True, text=True, timeout=3)
                found = res.stdout.splitlines() if res.returncode == 0 else []
            except (OSError, subprocess.TimeoutExpired):
                pass
            if not found:
                skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".foreman"}
                for base, dirs, names in os.walk(self.root):
                    dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
                    rel = os.path.relpath(base, self.root)
                    found += [n if rel == "." else os.path.join(rel, n) for n in names]
                    if len(found) > 20000:
                        break
            folders = {os.path.dirname(f) for f in found} - {""}
            self._files = sorted(found[:20000]) + sorted(d + "/" for d in folders)
        return self._files

    def file_matches(self) -> List[tuple]:
        """'@' menu rows: names starting with what was typed first, then paths containing it."""
        m = self.mention()
        if not m:
            return []
        q = m.group(1).lower()
        files = self.files()
        first = [f for f in files if os.path.basename(f.rstrip("/")).lower().startswith(q)]
        rest = [f for f in files if q in f.lower() and f not in first] if q else []
        return [("@" + f, "") for f in sorted(first, key=len)[:8] + rest[:8 - min(8, len(first))]]

    def _menu(self) -> List[tuple]:
        return self.matches(self.buf) or self.file_matches()

    def _line_start(self, pos: Optional[int] = None) -> int:
        return self.buf.rfind("\n", 0, self.cur if pos is None else pos) + 1

    def _line_end(self, pos: Optional[int] = None) -> int:
        end = self.buf.find("\n", self.cur if pos is None else pos)
        return len(self.buf) if end == -1 else end

    def _cut(self, start: int, end: int) -> None:
        """Remove buf[start:end], keep it for Ctrl+Y, cursor at start."""
        if end > start:
            self.killed = self.buf[start:end]
            self.buf, self.cur = self.buf[:start] + self.buf[end:], start

    def _vertical(self, step: int) -> bool:
        """Up/Down inside a message of several lines (same column where possible); False on its first/last line."""
        start = self._line_start()
        if step < 0 and start == 0 or step > 0 and self._line_end() == len(self.buf):
            return False
        col = self.cur - start
        if step < 0:
            prev = self._line_start(start - 1)
            self.cur = min(prev + col, start - 1)
        else:
            nxt = self._line_end() + 1
            self.cur = min(nxt + col, self._line_end(nxt))
        return True

    def read(self, hint_left: str = "", hint_right: str = "", suggestion: str = "", status: str = "") -> str:
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
        self.buf, self.cur, self.sel, self.row, self.notice = "", 0, 0, 0, ""
        self.span = self._anchor = None
        self.hist_i = len(self.history)
        self.hints = (hint_left, hint_right)
        self.suggestion = " ".join(suggestion.split())
        self.status = status
        self._files = None  # files may have changed since the last message
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
                        typed = Typed(self.attach.expand(result))
                        typed.shown = result
                        self._save(result, persist=not self.attach.known(result))  # chips only mean something now
                        return typed
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
    def _counts(self, cols: int) -> List[int]:
        """Screen rows taken by each line of the input (each starts after a 2-column prompt and wraps)."""
        return [max(1, -(-(2 + text_width(line)) // cols)) for line in self.buf.split("\n")]

    def live_height(self) -> int:
        return 2 + sum(self._counts(width())) + len(self._below())

    def frame(self) -> str:
        self.row = 0
        return c("─" * width(), ACCENT_DEEP) + "\r\n" + self._frame(fresh=True)

    # key handling: returns (remaining input, result-or-None); remaining None = incomplete escape sequence
    def _key(self, s: str):
        self.notice = ""
        if not s.startswith("\x1b[<"):
            self.span = None  # any key ends a mouse selection
        menu = self._menu()
        files = bool(menu) and menu[0][0].startswith("@")
        if s.startswith("\x1b[200~"):
            end = s.find("\x1b[201~")
            if end == -1:
                return None, None
            self._paste(s[6:end])
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
            if self._mouse(m.group(0)) or SCROLL.mouse(m.group(0)):
                return rest, None
            amount = scroll_amount(m.group(0))
            if amount is not None:  # mouse wheel / PgUp / PgDn scroll the transcript; other clicks are ignored
                SCROLL.scroll(amount)
                return rest, None
            SCROLL.bottom()
            if seq in ("[A", "OA"):
                if menu:
                    self.sel = (self.sel - 1) % len(menu)
                elif self._vertical(-1):
                    pass
                elif self.hist_i > 0:
                    self.hist_i -= 1
                    self.buf = self.history[self.hist_i]
                    self.cur = len(self.buf)
            elif seq in ("[B", "OB"):
                if menu:
                    self.sel = (self.sel + 1) % len(menu)
                elif self._vertical(1):
                    pass
                elif self.hist_i < len(self.history):
                    self.hist_i += 1
                    self.buf = self.history[self.hist_i] if self.hist_i < len(self.history) else ""
                    self.cur = len(self.buf)
            elif seq in ("[C", "OC"):
                if not self.buf and self.suggestion:
                    self._take_suggestion()
                self.cur = min(len(self.buf), self._chip_end(self.cur) or self.cur + 1)
            elif seq in ("[D", "OD"):
                self.cur = max(0, self._chip_start(self.cur) if self._chip_start(self.cur) is not None else self.cur - 1)
            elif seq in ("[H", "OH", "[1~"):
                self.cur = 0
            elif seq in ("[F", "OF", "[4~"):
                self.cur = len(self.buf)
            elif seq == "[3~":
                self._delete_forward()
            return rest, None
        ch, rest = s[0], s[1:]
        SCROLL.bottom()
        if ch in ("\r", "\n"):
            if files:  # Enter picks the file; the message is sent with the next Enter
                self._complete(menu[self.sel % len(menu)][0])
                return rest, None
            if self.buf[:self.cur].endswith("\\"):  # backslash + Enter: a new line, like Claude Code
                self.buf = self.buf[:self.cur - 1] + "\n" + self.buf[self.cur:]
                return rest, None
            if menu and self.buf != menu[self.sel % len(menu)][0]:
                self.buf = menu[self.sel % len(menu)][0]
            return rest, self.buf
        if ch == "\t":
            if files:
                self._complete(menu[self.sel % len(menu)][0])
            elif menu:
                self.buf = menu[self.sel % len(menu)][0] + " "
                self.cur = len(self.buf)
            elif not self.buf and self.suggestion:
                self._take_suggestion()
            return rest, None
        if ch == "\x03":
            if self.buf:
                self.buf, self.cur = "", 0
                return rest, None
            return rest, KeyboardInterrupt
        if ch == "\x04":
            if not self.buf:
                return rest, EOFError
            self._delete_forward()
            return rest, None
        if ch == "\x0f":  # Ctrl+O: everything so far, with shortened outputs in full
            details()
        elif ch == "\x16":  # Ctrl+V: an image on the clipboard (text is pasted by the terminal itself)
            self._paste_clipboard_image()
        elif ch in ("\x7f", "\x08"):
            if self.cur:
                start = self._chip_start(self.cur)
                start = self.cur - 1 if start is None else start
                self.buf = self.buf[:start] + self.buf[self.cur:]
                self.cur = start
        elif ch == "\x01":  # Ctrl+A / Ctrl+E: start / end of the current line
            self.cur = self._line_start()
        elif ch == "\x05":
            self.cur = self._line_end()
        elif ch == "\x15":  # Ctrl+U: delete to the start of the line (at its start: join it with the line above)
            start = self._line_start()
            self._cut(start if self.cur > start else max(0, self.cur - 1), self.cur)
        elif ch == "\x0b":  # Ctrl+K: delete to the end of the line (at its end: join the next line)
            end = self._line_end()
            self._cut(self.cur, end if end > self.cur else min(len(self.buf), self.cur + 1))
        elif ch == "\x17":  # Ctrl+W: delete the word before the cursor
            left = self.buf[:self.cur].rstrip()
            self._cut(max(left.rfind(" "), left.rfind("\n")) + 1, self.cur)
        elif ch == "\x19":  # Ctrl+Y: paste back what Ctrl+K / Ctrl+U / Ctrl+W removed
            self._insert(self.killed)
        elif ch >= " ":
            self._insert(ch)
            self.sel = 0
        return rest, None

    def _insert(self, text: str) -> None:
        self.buf = self.buf[:self.cur] + text + self.buf[self.cur:]
        self.cur += len(text)
        if text:
            self.suggestion = ""  # typing dismisses the suggestion

    def _take_suggestion(self) -> None:
        self.buf, self.suggestion = self.suggestion, ""
        self.cur = len(self.buf)

    def _complete(self, choice: str) -> None:
        """Replace the `@partial` at the cursor with the chosen `@path` (and a space after a file)."""
        m = self.mention()
        if not m:
            return
        start = m.start() + (1 if self.buf[m.start()] != "@" else 0)
        insert = choice + ("" if choice.endswith("/") else " ")
        self.buf = self.buf[:start] + insert + self.buf[self.cur:]
        self.cur, self.sel = start + len(insert), 0

    def _index_at(self, row: int, x: int) -> int:
        """Index in the input text of the character under screen column x on input row `row`."""
        cols, start, first = width(), 0, 0
        lines, counts = self.buf.split("\n"), self._counts(width())
        for line, n in zip(lines, counts):
            if row < first + n or line is lines[-1]:
                col, used = (row - first) * cols + x - 2, 0  # 2 = the "> " prompt (or its indent)
                for i, ch in enumerate(line):
                    used += char_width(ch)
                    if col < used:
                        return start + i
                return start + len(line)
            start, first = start + len(line) + 1, first + n
        return len(self.buf)

    def _mouse(self, key: str) -> bool:
        """Select text typed in the input box: press, drag, release copies it (like the transcript)."""
        m = MOUSE_FULL_RE.fullmatch(key)
        if not (m and SCROLL.select_enabled and self.buf):
            return False
        button, x, y, kind = int(m.group(1)), int(m.group(2)) - 1, int(m.group(3)) - 1, m.group(4)
        if button >= 64 or button & 3:
            return False
        top = SCROLL.live_top() + 1
        rows = sum(self._counts(width()))
        if kind == "M" and not button & 32:  # press
            if not top <= y < top + rows:
                self._anchor = None
                return False
            self._anchor, self.span, self._dragged = self._index_at(y - top, x), None, False
            return True
        if self._anchor is None:
            return False
        here = self._index_at(max(0, min(y - top, rows - 1)), x)
        first, last = min(self._anchor, here), min(len(self.buf), max(self._anchor, here) + 1)
        for chip in self.attach.known(self.buf):  # a chip is selected whole
            if chip.start() < last and chip.end() > first:
                first, last = min(first, chip.start()), max(last, chip.end())
        if kind == "M":  # drag
            self.span, self._dragged = (first, last), True
        else:  # release: a plain click selects nothing
            if self._dragged and last > first:
                self.span = (first, last)
                self.notice = copy_message(self.attach.expand(self.buf[first:last]))
            else:
                self.span = None
            self._anchor = None
        return True

    def _chip_start(self, pos: int) -> Optional[int]:
        """Start of the chip that ends exactly at pos (Backspace and ← treat a chip as one character)."""
        return next((m.start() for m in self.attach.known(self.buf) if m.end() == pos), None)

    def _chip_end(self, pos: int) -> Optional[int]:
        return next((m.end() for m in self.attach.known(self.buf) if m.start() == pos), None)

    def _delete_forward(self) -> None:
        end = self._chip_end(self.cur) or self.cur + 1
        self.buf = self.buf[:self.cur] + self.buf[end:]

    def _paste(self, text: str) -> None:
        """Bracketed paste: dragged-in image files and long text become chips, short text goes in as typed. Pasting
        the same thing again right after its chip expands it: the full text, or the image's path."""
        if not text.strip():  # terminals send an empty paste when the clipboard holds only an image
            self._paste_clipboard_image()
            return
        images = dropped_images(text)
        if images:
            for path in images:
                self._add_image(path)
            return
        dropped = dropped_files(text)
        if dropped:
            self._insert(" ".join(self.attach.file(p) for p in dropped) + " ")
            return
        before = self.buf[:self.cur]
        again = self.attach.last_chip(before, "text")
        if again and self.attach.repeats(again.group(0), text):
            end = len(before)  # the chip (and any spaces after it) gives way to the text itself
            room = MAX_PASTE_LINES - self.attach.pasted_lines(self.buf[:again.start()] + self.buf[end:])
            full = "\n".join(self.attach.normalize(text).split("\n")[:max(room, 0)])
            self.buf = self.buf[:again.start()] + full + self.buf[self.cur:]
            self.cur = again.start() + len(full)
            return
        insert, self.notice = self.attach.paste(text, self.buf)
        self._insert(insert)
        if insert in self.attach.items and not self.notice:
            self.notice = "Paste it again to show the full text"

    def _add_image(self, path: str, temporary: bool = False) -> None:
        before = self.buf[:self.cur]
        again = self.attach.last_chip(before, "image")
        if again and self.attach.same_image(self.attach.items[again.group(0)][1], path):
            shown = self.attach.items[again.group(0)][1]
            if temporary and path != shown:
                try:
                    os.remove(path)  # the clipboard image was the same picture again
                except OSError:
                    pass
            self.buf = self.buf[:again.start()] + shown + " " + self.buf[self.cur:]
            self.cur = again.start() + len(shown) + 1
            return
        self._insert(self.attach.image(path) + " ")
        self.notice = "Paste it again to show the file path"

    def _paste_clipboard_image(self) -> None:
        path, why = clipboard_image(self.image_dir)
        if path:
            self._add_image(path, temporary=path.startswith(self.image_dir))
        else:
            self.notice = why

    def _below(self) -> List[str]:
        return self._hint_rows() + ([status_row(self.status)] if self.status else [])

    def _hint_rows(self) -> List[str]:
        if self.notice:
            return [c("  " + self.notice[: width() - 4], YELLOW)]
        menu = self._menu()
        if menu:
            # A window of up to 8 rows that scrolls with the selection (only when it would leave the window), like
            # Claude Code's; ↑ / ↓ in the margin say more commands are above / below.
            n = max(1, min(len(menu), 8, shutil.get_terminal_size((100, 24)).lines - 8))
            sel = self.sel % len(menu)
            top = min(max(0, self._menu_top, sel - n + 1), sel, len(menu) - n)
            self._menu_top = top
            w = max(len(cmd) for cmd, _ in menu) + 3
            rows = []
            for i in range(top, top + n):
                cmd, desc = menu[i]
                edge = "↑" if i == top and top > 0 else "↓" if i == top + n - 1 and top + n < len(menu) else " "
                line = f"{edge} {cmd.ljust(w)}{desc}"[: width() - 1]
                rows.append(c("›" + line[1:], BOLD, ACCENT) if i == sel else c(line, GREY))
            return rows
        self._menu_top = 0
        if self.suggestion and not self.buf:
            return [hint_line("tab to use the suggestion · or type your own", self.hints[1])]
        return [hint_line(*self.hints)]

    def _draw(self) -> None:
        frame(self._frame())

    def _frame(self, fresh: bool = False) -> str:
        """Input rows, bottom rule and hint/menu, overwritten in place; leaves the cursor in the input. Each line of
        the message starts on its own row, under the "> " prompt."""
        cols = width()
        lines, counts = self.buf.split("\n"), self._counts(cols)
        below = self._below()
        up = f"\033[{self.row}A" if self.row and not fresh else ""

        def chips(text: str) -> str:
            return CHIP_RE.sub(lambda m: c(m.group(0), ACCENT) if m.group(0) in self.attach.items else m.group(0), text)
        if self.span:
            a, b = self.span
            styled = chips(self.buf[:a]) + "\033[7m" + self.buf[a:b] + "\033[27m" + chips(self.buf[b:])
        else:
            styled = chips(self.buf)
        if self.suggestion and not self.buf:
            styled = c(self.suggestion[: cols - 4], GREY)
        out = [up, "\r"]
        for i, (line, shown) in enumerate(zip(lines, styled.split("\n"))):
            out += [c(">", BOLD, ACCENT) + " " if i == 0 else "  ", shown,
                    "" if (2 + text_width(line)) % cols == 0 else "\033[K", "\r\n"]
        out += [c("─" * cols, ACCENT_DEEP), "\r\n", "\r\n".join(eol(b, cols) for b in below), "\033[J"]
        li = self.buf.count("\n", 0, self.cur)
        target = 2 + text_width(self.buf[self._line_start():self.cur])
        trow = sum(counts[:li]) + min(target // cols, counts[li] - 1)
        tcol = target % cols if target // cols < counts[li] else cols - 1
        out.append(f"\033[{sum(counts) + len(below) - trow}A\r" + (f"\033[{tcol}C" if tcol else ""))
        self.row = trow
        return "".join(out)

    def _finish(self) -> None:
        write((f"\033[{self.row}A" if self.row else "") + "\r\033[1A\033[J")


_EDITOR: Optional[LineEditor] = None


def set_editor(editor: LineEditor) -> None:
    global _EDITOR
    _EDITOR = editor


def read_input(hint_left: str = "", hint_right: str = "", suggestion: str = "", status: str = "") -> str:
    """Read one message with the shared editor (plain input() when not interactive)."""
    editor = _EDITOR or LineEditor([])
    return editor.read(hint_left, hint_right, suggestion, status)


def status_row(text: str) -> str:
    """The status line under the input: '◈ project · mode · task · models', cut to one row."""
    return clip("  " + c("◈ ", ACCENT) + c(text, GREY), width())


PREV, NEXT = "\x00prev", "\x00next"  # what choose(nav=...) returns for ← / →


def chips(labels: List[str], current: int, boxes: bool = True) -> str:
    """'← ☐ Scope  ✔ Titles  ☐ Submit →': one chip per question (✔ = answered), the current one highlighted.
    boxes=False: plain tabs ('← Settings  Status →')."""
    out = []
    for i, label in enumerate(labels):
        done = label.startswith("✔ ")
        text = f" {label if done or not boxes else '☐ ' + label} "
        out.append(c(text, BOLD, "\033[7m") if i == current else c(text, GREEN if done else GREY))
    return clip(c("← ", GREY) + " ".join(out) + c(" →", GREY), width())


def choose(title: str, body: List[str], options: List[str], allow_text: bool = True, start: int = 0,
           color: str = YELLOW, esc: str = "decide later", transient: bool = False,
           enter: str = "choose", descriptions: Optional[List[str]] = None, tabs: str = "",
           nav: str = "", typed_text: str = "", search: str = "") -> Optional[str]:
    """Arrow-key / number menu. Returns the chosen option, typed text ("" when skipped), or None on Esc.
    transient: a panel at the bottom (title and text included) that disappears when it closes and leaves nothing in
    the transcript, for settings and questions; otherwise the question and the answer stay in the transcript.
    descriptions: a grey line (or more) under each option, like Claude Code's questions. tabs: a row above the
    panel (see chips()). nav: what ← / → move between ("questions", "tabs"); they return PREV / NEXT. typed_text: what the
    "Type something else…" row already holds. search: a placeholder such as "Search settings…": typing filters the
    options (like Claude Code's /config)."""
    if not TTY or not sys.stdin.isatty():
        return None
    import termios
    import tty as _tty
    items = options + (["Type something else…"] if allow_text else [])
    sel = max(0, min(start, len(items) - 1))
    head = ([tabs] if tabs else []) + box(body, title, color).split("\n") if transient else []
    if not transient:
        out(box(body, title, color))

    typed = [typed_text]  # the answer typed into the "Type something else…" row itself (no separate input box)
    notes = list(descriptions or [])
    base, base_notes, query = list(items), list(notes), [""]  # search: items/notes hold the matching options
    extra = 1 if search else 0  # the search row under the panel's head

    def is_text(i: int) -> bool:
        return allow_text and i == len(items) - 1

    def item_rows(i: int, cols: int) -> List[str]:
        """One option, wrapped under its number so a long one is read in full (each row fits the width: a row the
        terminal wraps itself would make every redraw drift)."""
        num = f"{i + 1}. "
        wrap = max(10, cols - 5 - len(num))
        if is_text(i) and (typed[0] or i == sel):  # the text row is an input field while selected
            parts = textwrap.wrap(typed[0], wrap, break_on_hyphens=False, drop_whitespace=False) or [""]
            if i == sel:
                parts[-1] += "\033[7m \033[27m"
            if not typed[0]:
                parts = [c(items[i], GREY) + " \033[7m \033[27m"]
        else:
            parts = textwrap.wrap(items[i], wrap, break_on_hyphens=False) or [""]
        mark = c("›", ACCENT) if i == sel else " "
        style = (lambda s: c(s, BOLD)) if i == sel and not is_text(i) else (lambda s: s)
        rows = [clip(f" {mark if k == 0 else ' '} " + style((num if k == 0 else " " * len(num)) + p), cols)
                for k, p in enumerate(parts)]
        note = notes[i] if i < len(notes) and not is_text(i) else ""
        rows += [clip(" " * (3 + len(num)) + c(p, GREY), cols)
                 for p in textwrap.wrap(note, wrap, break_on_hyphens=False)]
        return rows

    peak = [0]

    def room() -> int:
        """Rows for the options: all of them, or as many as fit the terminal (a menu taller than the screen can't
        be redrawn in place). It only grows while the menu is open (typing a long answer), never shrinks."""
        peak[0] = max(peak[0], sum(len(item_rows(i, width())) for i in range(len(items))))
        return max(1, min(peak[0], shutil.get_terminal_size((100, 24)).lines - 2 - len(head) - extra))

    def lines() -> List[str]:
        cols, n = width(), room()
        find = [clip("  " + c("⌕ ", ACCENT) + (query[0] if query[0] else c(search, GREY)) + "\033[7m \033[27m", cols)] \
            if search else []
        if not items:
            return head + find + [c("   No setting matches", GREY)] + [""] * (n - 1) + [c(f"   Esc to {esc}", GREY)]
        rows = {i: item_rows(i, cols) for i in range(len(items))}
        top = bottom = sel  # grow a window around the selection until it is full
        used = len(rows[sel])
        while True:
            grew = False
            if bottom + 1 < len(items) and used + len(rows[bottom + 1]) <= n:
                bottom, used, grew = bottom + 1, used + len(rows[bottom + 1]), True
            if top > 0 and used + len(rows[top - 1]) <= n:
                top, used, grew = top - 1, used + len(rows[top - 1]), True
            if not grew:
                break
        shown = [r for i in range(top, bottom + 1) for r in rows[i]]
        shown = (shown[-n:] if is_text(sel) else shown[:n]) + [""] * (n - len(shown))  # a long answer: its end
        more = f"{sel + 1}/{len(items)} · " if top > 0 or bottom < len(items) - 1 else ""
        keys = "type your answer, Enter to send" if is_text(sel) else \
            f"↑/↓ · Enter to {enter}" if nav else f"↑/↓ or number, Enter to {enter}"
        keys += (f" · ←/→ {nav}" if nav and not (is_text(sel) and typed[0]) else "") + \
            (" · type to search" if search else "")
        return head + find + shown + [clip(c(f"   {more}{keys} · Esc to {esc}", GREY), cols)]

    def body() -> str:
        return "".join("\r" + eol(l, width()) + "\n" for l in lines())

    drawn = [0]

    def draw(first=False):
        with OUTPUT_LOCK:
            up = "" if first else f"\033[{drawn[0]}A"
            drawn[0] = len(head) + extra + room() + 1
            frame(up + body())

    closed = [False]

    class _Live:  # the menu stays at the bottom while the transcript above scrolls
        live_height = staticmethod(lambda: 0 if closed[0] else len(head) + extra + room() + 1)
        frame = staticmethod(lambda: "" if closed[0] else body())

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    live, pending, cancelled, moved = _Live(), "", False, None
    decode = codecs.getincrementaldecoder("utf-8")("ignore").decode  # a character split across reads stays whole
    draw(first=True)
    SCROLL.push(live)
    try:
        _tty.setcbreak(fd)
        done = False
        while not done:
            keys, pending = split_keys(pending + decode(os.read(fd, 64)))
            for key in keys:
                if SCROLL.mouse(key):
                    continue
                amount = scroll_amount(key)
                if amount is not None:
                    SCROLL.scroll(amount)
                    continue
                if SCROLL.offset:
                    SCROLL.bottom()
                    if key == "\x1b":
                        continue
                if nav and key in ("\x1b[D", "\x1bOD", "\x1b[C", "\x1bOC") and not (is_text(sel) and typed[0]):
                    moved = PREV if key in ("\x1b[D", "\x1bOD") else NEXT
                    done = True
                    break
                if search and (key in ("\x7f", "\b") or len(key) == 1 and key.isprintable()):  # typing filters
                    query[0] = query[0][:-1] if key in ("\x7f", "\b") else query[0] + key
                    hits = [i for i, label in enumerate(base) if query[0].lower() in label.lower()]
                    items[:] = [base[i] for i in hits]
                    notes[:] = [base_notes[i] for i in hits if i < len(base_notes)]
                    sel = 0
                    continue
                if not items:
                    if key in ("\x1b", "\x03"):
                        cancelled = done = True
                        break
                    continue
                if is_text(sel) and key not in ("\r", "\n", "\x1b", "\x03") and not key.startswith("\x1b"):
                    if key in ("\x7f", "\b"):
                        typed[0] = typed[0][:-1]
                    elif key == "\x15":  # Ctrl+U: clear
                        typed[0] = ""
                    elif key.isprintable():
                        typed[0] += key
                    continue
                if is_text(sel) and key in ("\r", "\n") and not typed[0].strip():
                    continue  # nothing typed yet
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
        if transient:  # take the panel away: the screen is as it was before it opened
            closed[0] = True
            with OUTPUT_LOCK:
                frame(f"\033[{drawn[0]}A\r\033[J")
        SCROLL.pop(live)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if not transient:
            SCROLL.add("\n".join(lines()))
    if cancelled:
        return None
    if moved:
        return moved
    return typed[0].strip() if is_text(sel) else items[sel]


def details() -> None:
    """Ctrl+O: the whole conversation with every shortened output in full, on its own screen (it opens at the end;
    ↑/↓, PgUp/PgDn or the wheel scroll; q, Esc or Ctrl+O close it and the chat is back as it was)."""
    if not (TTY and sys.stdin.isatty()):
        return
    import termios
    import tty as _tty
    own = not SCROLL.enabled  # outside the full-screen chat it opens (and closes) a screen of its own
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    decode = codecs.getincrementaldecoder("utf-8")("ignore").decode
    with OUTPUT_LOCK:
        cols = width()
        rows = [r for line in SCROLL.details or [""] for r in hard_wrap(line, cols)]
    height = lambda: max(1, shutil.get_terminal_size((100, 24)).lines - 1)  # noqa: E731
    top = max(0, len(rows) - height())
    try:
        if own:
            write("\033[?1049h")
        _tty.setcbreak(fd)
        pending = ""
        while True:
            h = height()
            top = max(0, min(top, len(rows) - h))
            foot = c(f"  Details · {min(len(rows), top + h)}/{len(rows)} · ↑/↓ PgUp/PgDn to scroll · q or Esc to close", GREY)
            with OUTPUT_LOCK:
                frame("\033[H" + "".join(eol(r + RESET, cols) + "\r\n" for r in rows[top:top + h])
                      + "\033[J" * (len(rows) - top < h) + clip(foot, cols), show_cursor=False)
            keys, pending = split_keys(pending + decode(os.read(fd, 1024)))
            for key in keys:
                amount = scroll_amount(key)
                if key in ("q", "\x1b", "\x0f", "\x03"):
                    return
                if amount:
                    top -= h - 1 if amount >= PAGE else -(h - 1) if amount <= -PAGE else amount
                elif key in ("\x1b[A", "\x1bOA", "k"):
                    top -= 1
                elif key in ("\x1b[B", "\x1bOB", "j"):
                    top += 1
                elif key in ("g", "\x1b[H"):
                    top = 0
                elif key in ("G", "\x1b[F"):
                    top = len(rows)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        with OUTPUT_LOCK:
            if own:
                write("\033[?1049l")
            else:
                write("\033[H\033[2J")
                SCROLL.dirty = True
                SCROLL.flush()


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
    selecting text with the mouse is done by the chat itself (copied on release), or with Shift when that is off."""

    def __init__(self, enabled: bool = True, select: bool = True):
        self.enabled = enabled and TTY
        self.select = select

    def __enter__(self):
        if self.enabled:
            SCROLL.clear()
            SCROLL.enabled = True
            SCROLL.select_enabled = self.select
            # alt screen; the wheel (and, to select text, button presses and drags) arrive as mouse events
            write("\033[22;0t\033[?1049h\033[H\033[2J\033[?1000h" + ("\033[?1002h" if self.select else "") + "\033[?1006h")
        return self

    def __exit__(self, *exc):
        if self.enabled:
            SCROLL.enabled = SCROLL.select_enabled = False
            write("\033[?1002l\033[?1000l\033[?1006l\033[?25h\033[?2004l\033[?1049l\033[23;0t")  # title back too
        return False


class DoubleEsc:
    """Esc pressed twice within `window` seconds. Only a lone Esc counts (arrow keys and the like also start with it)."""

    def __init__(self, window: float = 1.0):
        self.window, self._last = window, 0.0

    def feed(self, data: str, now: Optional[float] = None) -> str:
        """"" for other input, "first" for the first Esc, "second" for the confirming one."""
        if data != "\x1b":
            return ""
        now = time.time() if now is None else now
        if now - self._last <= self.window:
            self._last = 0.0
            return "second"
        self._last = now
        return "first"


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
        self.attach = _EDITOR.attach if _EDITOR else Attachments()  # chips stay valid across the editor and the spinner
        self.notice = ""
        self.status = ""  # the status line under the hint ("" = off)
        self._pasting: Optional[str] = None  # text of a bracketed paste still arriving
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
            write("\033[?25l\033[?2004h")  # hide the real cursor (the box draws its own); bracketed paste on
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
        meta = (f"{self.who} · " if self.who else "") + f"{secs}s · esc to interrupt · ctrl+o details"
        if len(self.verb) + len(meta) + 6 > w:  # one row, always: a wrapped status line makes the box jump
            meta = (f"{self.who} · " if self.who else "") + f"{secs}s"
        verb = self.verb if len(self.verb) + len(meta) + 6 <= w else self.verb[: max(4, w - len(meta) - 8)] + "…"
        status = f"{c(SPIN[(self._tick // 2) % len(SPIN)], ACCENT, BOLD)} {shimmer(verb + '…', self._tick)} " \
                 f"{c(f'({meta})', GREY)}"
        if not self.footer:
            return [status]
        shown = self.typed.replace("\n", "⏎")[-(w - 4):]
        if self.notice:
            left = self.notice
        elif self.queued:
            left = f"queued ({len(self.queued)}): " + " · ".join(getattr(q, "shown", q)[:30] for q in self.queued) \
                + (" · ↑ to edit" if not self.typed else "")
        else:
            left = "type your next message; it's sent when this finishes"
        return [status, "", c("─" * w, ACCENT_DEEP), c(">", BOLD, ACCENT) + " " + shown + "\033[7m \033[0m",
                c("─" * w, ACCENT_DEEP), hint_line(left, self.hint_right)] + ([status_row(self.status)] if self.status else [])

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
        """Type-ahead editing: printable text, Backspace, Enter (queue), Ctrl+U (clear), pasted text and images."""
        with self._lock:
            self.notice = ""
            while data:
                if self._pasting is not None:
                    end = data.find("\x1b[201~")
                    if end == -1:
                        self._pasting, data = self._pasting + data, ""
                        break
                    text, self._pasting, data = self._pasting + data[:end], None, data[end + 6:]
                    self._paste(text)
                    continue
                start = data.find("\x1b[200~")
                chunk, data = (data, "") if start == -1 else (data[:start], data[start + 6:])
                if start != -1:
                    self._pasting = ""
                self._type(chunk)
            if SCROLL.flush():
                return
            if not self._paused and TTY:
                self._draw()

    def _type(self, data: str) -> None:
        for ch in split_keys(data)[0]:
            if SCROLL.mouse(ch):
                continue
            amount = scroll_amount(ch)
            if amount is not None:
                SCROLL.scroll(amount)
                continue
            SCROLL.bottom()
            if ch in ("\r", "\n") and self.typed.endswith("\\"):  # backslash + Enter: a new line
                self.typed = self.typed[:-1] + "\n"
            elif ch in ("\r", "\n"):
                if self.typed.strip():
                    self.queued.append(self._compose(self.typed.strip()))
                self.typed = ""
            elif ch in ("\x1b[A", "\x1bOA") and not self.typed and self.queued:  # ↑ takes a queued message back
                last = self.queued.pop()
                self.typed = getattr(last, "shown", "") or str(last)
            elif ch in ("\x7f", "\x08"):
                chip = next((m for m in self.attach.known(self.typed) if m.end() == len(self.typed)), None)
                self.typed = self.typed[:chip.start() if chip else -1]
            elif ch == "\x15":  # Ctrl+U: the current line (an empty one: joins the line above)
                cut = self.typed.rfind("\n") + 1
                self.typed = self.typed[:cut] if cut < len(self.typed) else self.typed[:-1]
            elif ch >= " " and not ch.startswith("\x1b"):
                self.typed += ch

    def _paste(self, text: str) -> None:
        images, dropped = dropped_images(text), dropped_files(text)
        if images:
            self.typed += " ".join(self.attach.image(p) for p in images) + " "
        elif dropped:
            self.typed += " ".join(self.attach.file(p) for p in dropped) + " "
        else:
            insert, self.notice = self.attach.paste(text, self.typed)
            self.typed += insert

    def _compose(self, text: str) -> "Typed":
        typed = Typed(self.attach.expand(text))
        typed.shown = text
        return typed

    def pending(self) -> List[str]:
        """Messages typed while working: the queued ones plus whatever was left in the input."""
        return self.queued + ([self._compose(self.typed.strip())] if self.typed.strip() else [])

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
                write("\033[?25h\033[?2004l")
