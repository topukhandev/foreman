"""Pasted text and images for the chat input: long pastes collapse to a chip such as `[Pasted text #1 +545 lines]`,
dragged-in or pasted image files become `[Image #1]`. The chips are what the input box shows; `expand` turns a message
back into the full text (with image paths) for the AI. Pure functions and a small state class, no terminal here."""

import filecmp
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

MAX_PASTE_LINES = 2000          # per message, over all pastes
COLLAPSE_LINES = 4              # pastes with at least this many lines (or CHARS characters) become a chip
COLLAPSE_CHARS = 400
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".svg")
CHIP_RE = re.compile(r"\[(?:Pasted text|Image|File) #\d+[^\]\n]*\]")
IMAGE_REF_RE = re.compile(r"\[(?:Image|File) #\d+: ([^\]\n]+)\]")  # how an attachment appears in the text the AI receives
IMAGE_PATH_RE = re.compile(r"(?<![\w/.])(/[^\s\[\]'\"]+\.(?:" + "|".join(e[1:] for e in IMAGE_EXTS) + r"))\b", re.I)  # a bare path


def dropped_files(text: str) -> List[str]:
    """Files or folders named by a paste. Terminals paste the path when a file is dragged in: bare, 'quoted',
    backslash-escaped or as a file:// URI, one or several. Empty unless every item is an existing file or folder."""
    text = text.strip()
    if not text or len(text) > 8000:
        return []
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    paths = []
    for tok in tokens:
        if tok.startswith("file://"):
            tok = unquote(tok[7:])
        tok = os.path.expanduser(tok)
        if not (os.path.isabs(tok) and os.path.exists(tok)):
            return []
        paths.append(tok.rstrip("/") or "/")
    return paths


def dropped_images(text: str) -> List[str]:
    """Like dropped_files, but only when every item is an image file."""
    paths = dropped_files(text)
    return paths if all(p.lower().endswith(IMAGE_EXTS) and os.path.isfile(p) for p in paths) else []


def image_dirs(text: str) -> List[str]:
    """Folders the AI is given read access to for a request's attachments: the folder of each image or file named as
    `[Image #n: path]` / `[File #n: path]` or as a bare image path, or the folder itself when one was attached."""
    paths = IMAGE_REF_RE.findall(text) + IMAGE_PATH_RE.findall(text)
    return list(dict.fromkeys(p if os.path.isdir(p) else os.path.dirname(p) for p in paths if os.path.exists(p)))


class Attachments:
    """Chips created in this input session and the content behind each."""

    def __init__(self) -> None:
        self.items: Dict[str, Tuple[str, str]] = {}   # chip -> (kind, content or file path)
        self._n = {"text": 0, "image": 0, "file": 0}

    def known(self, buf: str):
        return [m for m in CHIP_RE.finditer(buf) if m.group(0) in self.items]

    def pasted_lines(self, buf: str) -> int:
        return sum(self.items[m.group(0)][1].count("\n") + 1 for m in self.known(buf) if self.items[m.group(0)][0] == "text")

    @staticmethod
    def normalize(text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text[:-1] if text.endswith("\n") and text.count("\n") > 1 else text

    def last_chip(self, before: str, kind: str):
        """The chip of this kind that ends the text before the cursor (spaces after it allowed): pasting the same
        thing again right after it expands it instead of adding a second chip."""
        return next((m for m in self.known(before) if self.items[m.group(0)][0] == kind and not before[m.end():].strip(" ")), None)

    def repeats(self, chip: str, text: str) -> bool:
        """Whether pasting text again is the paste this chip stands for (a paste that was cut at the limit counts)."""
        kept, text = self.items[chip][1], self.normalize(text)
        return text == kept or (text.startswith(kept + "\n") and kept.count("\n") + 1 >= MAX_PASTE_LINES - self.pasted_lines(""))

    def same_image(self, known_path: str, new_path: str) -> bool:
        try:
            return known_path == new_path or filecmp.cmp(known_path, new_path, shallow=False)
        except OSError:
            return False

    def paste(self, text: str, buf: str = "") -> Tuple[str, str]:
        """What to insert for pasted text and a notice ("" if none). Short pastes stay as they are; long ones become
        a chip; the whole message never holds more than MAX_PASTE_LINES pasted lines."""
        text = self.normalize(text)
        lines = text.count("\n") + 1
        room = MAX_PASTE_LINES - self.pasted_lines(buf)
        note = ""
        if lines >= COLLAPSE_LINES and room <= 0:
            return "", f"Paste limit reached: at most {MAX_PASTE_LINES} lines per message"
        if lines > room:
            text, note = "\n".join(text.split("\n")[:room]), (
                f"Kept {room} of {lines} pasted lines (limit {MAX_PASTE_LINES} per message)")
            lines = room
        if lines < COLLAPSE_LINES and len(text) < COLLAPSE_CHARS:
            return text, note
        self._n["text"] += 1
        size = f"+{lines} lines" if lines > 1 else f"{len(text):,} chars"
        chip = f"[Pasted text #{self._n['text']} {size}]"
        self.items[chip] = ("text", text)
        return chip, note

    def image(self, path: str) -> str:
        self._n["image"] += 1
        chip = f"[Image #{self._n['image']}]"
        self.items[chip] = ("image", path)
        return chip

    def file(self, path: str) -> str:
        """A dragged-in file or folder that is not an image: `[File #1 report.csv]`."""
        self._n["file"] += 1
        chip = f"[File #{self._n['file']} {os.path.basename(path) or path}]"
        self.items[chip] = ("file", path)
        return chip

    def expand(self, buf: str) -> str:
        """The message with every chip replaced by what it stands for."""
        def fill(m):
            kind, content = self.items[m.group(0)]
            if kind == "text":
                return content
            num = re.match(r"\[\w+ #(\d+)", m.group(0)).group(1)
            return f"[{'Image' if kind == 'image' else 'File'} #{num}: {content}]"
        return CHIP_RE.sub(lambda m: fill(m) if m.group(0) in self.items else m.group(0), buf)


# ---------------------------------------------------------------------- clipboard images

_GTK_SCRIPT = """
IMAGE_EXTS = %r
import sys, gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk
board = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
img = board.wait_for_image()
if img is not None:
    img.savev(sys.argv[1], "png", [], [])
    sys.exit(0)
from gi.repository import Gio
for uri in board.wait_for_uris() or []:  # an image file copied in the file manager
    path = Gio.File.new_for_uri(uri).get_path()
    if path and path.lower().endswith(IMAGE_EXTS):
        print(path)
        sys.exit(0)
sys.exit(3)
"""


def _capture(cmd: List[str], dest: str, input_check: Optional[List[str]] = None) -> bool:
    try:
        if input_check:
            probe = subprocess.run(input_check, capture_output=True, text=True, timeout=5)
            if probe.returncode != 0 or "image/png" not in probe.stdout:
                return False
        res = subprocess.run(cmd, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if res.returncode != 0 or not res.stdout.startswith(b"\x89PNG"):
        return False
    with open(dest, "wb") as f:
        f.write(res.stdout)
    return True


def clipboard_image(dest_dir: str) -> Tuple[Optional[str], str]:
    """Save the clipboard's image as a PNG in dest_dir. Returns (path, "") or (None, reason). Uses wl-paste (Wayland),
    xclip (X11), pngpaste (macOS), else GTK through python3-gi when it is installed."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, time.strftime("clipboard-%Y%m%d-%H%M%S.png"))
    if shutil.which("wl-paste") and os.environ.get("WAYLAND_DISPLAY"):
        ok = _capture(["wl-paste", "--type", "image/png"], dest, ["wl-paste", "--list-types"])
    elif shutil.which("xclip") and os.environ.get("DISPLAY"):
        ok = _capture(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], dest,
                      ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
    elif sys.platform == "darwin" and shutil.which("pngpaste"):
        ok = _capture(["pngpaste", "-"], dest)
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        try:
            res = subprocess.run([sys.executable, "-c", _GTK_SCRIPT % (IMAGE_EXTS,), dest], capture_output=True, text=True,
                                 timeout=10)
            ok = res.returncode == 0
            if ok and res.stdout.strip() and not os.path.exists(dest):  # a copied file: use it where it is
                return res.stdout.strip().splitlines()[0], ""
        except (OSError, subprocess.TimeoutExpired):
            ok = False
    else:
        return None, "Image paste needs a graphical session (and xclip, wl-clipboard or pngpaste)"
    if ok and os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest, ""
    if not (shutil.which("xclip") or shutil.which("wl-paste") or shutil.which("pngpaste")):
        return None, "No image on the clipboard (if there is one, install xclip or wl-clipboard to paste it)"
    return None, "No image on the clipboard"


# ---------------------------------------------------------------------- copying text

_GTK_HOLD = """
import sys, time, gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib
start = time.time()
board = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
board.set_text(sys.stdin.read(), -1)
board.store()  # hand the text to the desktop's clipboard manager when there is one
def lost(*_):
    if time.time() - start > 1:  # someone else copied (or the manager took over): nothing left to serve
        Gtk.main_quit()
board.connect("owner-change", lost)
GLib.timeout_add_seconds(3600, Gtk.main_quit)
Gtk.main()
"""


def copy_text(text: str) -> bool:
    """Put text on the system clipboard: wl-copy (Wayland), xclip / xsel (X11), pbcopy (macOS), else GTK through
    python3-gi (a small background process keeps serving the text). False when none of them is available."""
    data = text.encode("utf-8")
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        cmds = [["wl-copy"]]
    elif os.environ.get("DISPLAY") and shutil.which("xclip"):
        cmds = [["xclip", "-selection", "clipboard", "-i"]]
    elif os.environ.get("DISPLAY") and shutil.which("xsel"):
        cmds = [["xsel", "--clipboard", "--input"]]
    elif sys.platform == "darwin" and shutil.which("pbcopy"):
        cmds = [["pbcopy"]]
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        cmds = [[sys.executable, "-c", _GTK_HOLD]]
    else:
        return False
    try:
        proc = subprocess.Popen(cmds[0], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)  # xclip / wl-copy / the GTK holder stay alive to serve pastes
        proc.stdin.write(data)
        proc.stdin.close()
        time.sleep(0.15)
        return proc.poll() in (None, 0)
    except OSError:
        return False
