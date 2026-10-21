"""Terminal Markdown renderer with ANSI colors, clean indentation, and boxed code/tables.
Designed for high-readability CLI output without external dependencies.
"""

import os
import re
import shutil
import textwrap
from typing import List, Optional, Tuple


def get_terminal_width(default: int = 80, max_width: int = 95) -> int:
    """Detect terminal column width with sensible boundaries."""
    try:
        cols = shutil.get_terminal_size((default, 24)).columns
        return max(40, min(cols - 2, max_width))
    except Exception:
        return default


def format_inline(text: str) -> str:
    """
    Format inline markdown elements:
    - Bold with status color heuristics: **text**
    - Code and file paths: `code`
    - Italics: *text* or _text_
    - Links: [title](url)
    """
    # 1. Bold: **text** or __text__
    def _bold_repl(match: re.Match) -> str:
        content = match.group(1)
        lower = content.lower().strip()
        # Semantic status coloring
        if any(w in lower for w in ["idle", "approved", "complete", "completed", "success", "passed", "ok", "ready"]):
            return f"\033[1;32m{content}\033[0m"
        if any(w in lower for w in ["fail", "failed", "rejected", "error", "blocker", "blocked", "timeout"]):
            return f"\033[1;31m{content}\033[0m"
        if any(w in lower for w in ["active", "running", "warning", "pending", "in progress"]):
            return f"\033[1;33m{content}\033[0m"
        # Standard bold bright white
        return f"\033[1;37m{content}\033[0m"

    text = re.sub(r"\*\*(.+?)\*\*", _bold_repl, text)
    text = re.sub(r"__(.+?)__", _bold_repl, text)

    # 2. Inline code: `code`
    def _code_repl(match: re.Match) -> str:
        code = match.group(1)
        # Slash commands like `/run`, `/provider`, `/run <goal>` -> bright gold
        if re.match(r"^/[a-zA-Z0-9_-]+(\s+.*)?$", code) and "/" not in code[1:]:
            return f"\033[1;33m{code}\033[0m"
        # File paths or directory references -> cyan
        if "/" in code or "\\" in code or code.startswith("."):
            return f"\033[36m{code}\033[0m"
        # Commands or code identifiers -> bright gold / yellow
        return f"\033[1;33m{code}\033[0m"

    text = re.sub(r"`([^`]+)`", _code_repl, text)

    # 3. Italics: *text* or _text_ (single asterisk not followed by space)
    text = re.sub(r"(?<!\*)\*([^*\s][^*]*[^*\s])\*(?!\*)", r"\033[3m\1\033[0m", text)

    # 4. Links: [text](url) -> underlined text + dimmed url
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\033[4;34m\1\033[0m \033[90m(\2)\033[0m", text)

    return text


def render_code_box(lines: List[str], lang: str = "", width: int = 80, indent: str = "  ") -> List[str]:
    """Render syntax block inside an elegant rounded container."""
    box_w = min(width - len(indent), 85)
    inner_w = max(20, box_w - 4)
    lang_tag = f" {lang.strip()} " if lang.strip() else " code "

    top_border = (
        f"{indent}\033[90m╭─\033[1;36m{lang_tag}\033[0;90m"
        + ("─" * max(0, box_w - 2 - len(lang_tag) - 2))
        + "╮\033[0m"
    )
    bottom_border = f"{indent}\033[90m╰" + ("─" * (box_w - 2)) + "╯\033[0m"

    out = [top_border]
    for cl in lines:
        # Wrap long lines inside code block cleanly if they exceed width
        sub_lines = textwrap.wrap(cl, width=inner_w) if len(cl) > inner_w else [cl]
        for sl in (sub_lines or [""]):
            pad = " " * max(0, inner_w - len(sl))
            out.append(f"{indent}\033[90m│\033[0m \033[1;33m{sl}\033[0m{pad} \033[90m│\033[0m")
    out.append(bottom_border)
    return out


def render_table(table_lines: List[str], indent: str = "  ") -> List[str]:
    """Parse and render markdown pipe tables with aligned borders and highlighted headers."""
    raw_rows = []
    for line in table_lines:
        parts = [c.strip() for c in line.strip().strip("|").split("|")]
        raw_rows.append(parts)

    if not raw_rows:
        return []

    # Separate delimiter row (---) from data rows
    data_rows = []
    for r in raw_rows:
        if all(re.match(r"^:?-+:?$", cell) for cell in r if cell):
            continue
        data_rows.append(r)

    if not data_rows:
        return []

    num_cols = max(len(r) for r in data_rows)
    # Normalize rows
    for r in data_rows:
        while len(r) < num_cols:
            r.append("")

    col_widths = [max(len(r[i]) for r in data_rows) for i in range(num_cols)]
    col_widths = [max(w, 3) for w in col_widths]

    top_border = indent + "\033[90m┌─" + "─┬─".join("─" * w for w in col_widths) + "─┐\033[0m"
    sep_border = indent + "\033[90m├─" + "─┼─".join("─" * w for w in col_widths) + "─┤\033[0m"
    bot_border = indent + "\033[90m└─" + "─┴─".join("─" * w for w in col_widths) + "─┘\033[0m"

    out = [top_border]
    is_header = True

    for r in data_rows:
        cells = []
        for i, w in enumerate(col_widths):
            val = r[i]
            pad = " " * (w - len(val))
            if is_header:
                cells.append(f"\033[1;37m{val}\033[0m{pad}")
            else:
                low = val.lower()
                if low in ("running", "active", "in progress"):
                    cells.append(f"\033[1;33m{val}\033[0m{pad}")
                elif low in ("complete", "completed", "approved", "idle", "success", "ok"):
                    cells.append(f"\033[1;32m{val}\033[0m{pad}")
                elif low in ("failed", "rejected", "error", "blocker"):
                    cells.append(f"\033[1;31m{val}\033[0m{pad}")
                else:
                    cells.append(f"{val}{pad}")

        out.append(indent + "\033[90m│\033[0m " + " \033[90m│\033[0m ".join(cells) + " \033[90m│\033[0m")
        if is_header:
            out.append(sep_border)
            is_header = False

    out.append(bot_border)
    return out


def render_markdown(text: str, indent: str = "  ", width: Optional[int] = None) -> str:
    """
    Render markdown text into beautifully formatted, colorized terminal output.
    Ensures consistent indentation, elegant list wraps, syntax containers, and headers.
    """
    if not text:
        return ""

    # Sanitize any raw or orphaned ANSI artifacts from incoming LLM text
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'(?<!\w)(?:\[)?[0-9]{1,2}(?:;[0-9]{1,2})+m', '', text)

    if width is None:
        width = get_terminal_width()

    lines = text.splitlines()
    out: List[str] = []

    in_code = False
    code_lang = ""
    code_lines: List[str] = []

    in_table = False
    table_lines: List[str] = []

    def flush_table() -> None:
        nonlocal in_table, table_lines
        if in_table and table_lines:
            rendered = render_table(table_lines, indent=indent)
            out.extend(rendered)
            table_lines = []
            in_table = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 1. Code Fence (```)
        if stripped.startswith("```"):
            flush_table()
            if not in_code:
                in_code = True
                code_lang = stripped[3:].strip()
                code_lines = []
            else:
                in_code = False
                box = render_code_box(code_lines, lang=code_lang, width=width, indent=indent)
                out.extend(box)
                code_lines = []
            i += 1
            continue

        if in_code:
            code_lines.append(line)
            i += 1
            continue

        # 2. Markdown Pipe Table row
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            in_table = True
            table_lines.append(line)
            i += 1
            continue
        elif in_table:
            flush_table()

        # 3. Empty lines / Spacing (prevent duplicate blanks)
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            i += 1
            continue

        # 4. Standalone single backticked command (e.g. `/run <goal>`)
        if re.match(r"^`([^`]+)`$", stripped):
            cmd_content = stripped.strip("`")
            box = render_code_box([cmd_content], lang="command", width=width, indent=indent)
            out.extend(box)
            i += 1
            continue

        # 5. Headings with single-line separation
        def add_heading(h_line: str) -> None:
            if out and out[-1] != "":
                out.append("")
            out.append(h_line)

        if stripped.startswith("# "):
            h_text = stripped[2:].strip()
            add_heading(f"{indent}\033[1;35m◆ \033[4m{h_text.upper()}\033[0m")
            i += 1
            continue
        elif stripped.startswith("## "):
            h_text = stripped[3:].strip()
            add_heading(f"{indent}\033[1;34m■ {h_text}\033[0m")
            i += 1
            continue
        elif stripped.startswith("### "):
            h_text = stripped[4:].strip()
            add_heading(f"{indent}\033[1;36m▸ {h_text}\033[0m")
            i += 1
            continue
        elif stripped.startswith("#### "):
            h_text = stripped[5:].strip()
            add_heading(f"{indent}\033[1;37m• {h_text}\033[0m")
            i += 1
            continue

        # 6. Horizontal Rules (---, ===, ***)
        if re.match(r"^[-=_*]{3,}$", stripped):
            hr_len = max(20, width - len(indent))
            out.append(f"{indent}\033[90m" + ("─" * hr_len) + "\033[0m")
            i += 1
            continue

        # 7. Blockquotes (> quote)
        if stripped.startswith(">"):
            q_text = stripped.lstrip("> ").strip()
            wrapped = textwrap.wrap(q_text, width=max(20, width - len(indent) - 4))
            for ql in wrapped:
                out.append(f"{indent}\033[90m│\033[0m \033[3m{format_inline(ql)}\033[0m")
            i += 1
            continue

        # 8. Unordered List Items (*, -, +)
        list_match = re.match(r"^([*+-])\s+(.*)", stripped)
        if list_match:
            item_body = list_match.group(2).strip()
            wrap_w = max(20, width - len(indent) - 4)
            wrapped = textwrap.wrap(item_body, width=wrap_w)
            if wrapped:
                first = f"{indent}  \033[1;36m•\033[0m {format_inline(wrapped[0])}"
                out.append(first)
                for sub in wrapped[1:]:
                    # Indent child lines 4 spaces to align perfectly with first line text!
                    out.append(f"{indent}    {format_inline(sub)}")
            i += 1
            continue

        # 9. Ordered List Items (1. item, 2. item)
        num_match = re.match(r"^(\d+\.)\s+(.*)", stripped)
        if num_match:
            num_str = num_match.group(1)
            item_body = num_match.group(2).strip()
            wrap_w = max(20, width - len(indent) - len(num_str) - 3)
            wrapped = textwrap.wrap(item_body, width=wrap_w)
            if wrapped:
                first = f"{indent}  \033[1;36m{num_str}\033[0m {format_inline(wrapped[0])}"
                out.append(first)
                pad_sub = " " * (len(num_str) + 3)
                for sub in wrapped[1:]:
                    out.append(f"{indent}{pad_sub}{format_inline(sub)}")
            i += 1
            continue

        # 10. Standard Paragraph
        wrap_w = max(20, width - len(indent))
        wrapped = textwrap.wrap(stripped, width=wrap_w)
        for pl in wrapped:
            out.append(f"{indent}{format_inline(pl)}")
        i += 1

    flush_table()

    # Clean redundant trailing empty lines
    while out and not out[-1]:
        out.pop()

    return "\n".join(out)


def render_user_message(query: str, width: Optional[int] = None) -> str:
    """Render a clean, modern user message card with clear boundaries."""
    if width is None:
        width = get_terminal_width()
    box_w = min(width - 2, 85)
    inner_w = max(20, box_w - 4)

    title = " 👤 You "
    top = f"\033[90m╭─\033[1;36m{title}\033[0;90m{'─' * max(0, box_w - 2 - len(title) - 2)}╮\033[0m"
    bot = f"\033[90m╰{'─' * (box_w - 2)}╯\033[0m"

    lines = [top]
    for l in query.splitlines() or [""]:
        wrapped = textwrap.wrap(l, width=inner_w) or [""]
        for wl in wrapped:
            pad = " " * max(0, inner_w - len(wl))
            lines.append(f"\033[90m│\033[0m  \033[1;37m{wl}\033[0m{pad}\033[90m│\033[0m")
    lines.append(bot)
    return "\n".join(lines)


def render_assistant_response(text: str, provider: str, duration_sec: Optional[float] = None, width: Optional[int] = None) -> str:
    """Render a distinct, clean assistant response container."""
    if width is None:
        width = get_terminal_width()
    box_w = min(width - 2, 85)
    inner_w = max(20, box_w - 4)

    title = f" 🤖 Foreman ({provider}) "
    dur_tag = f" Responded in {duration_sec:.1f}s " if duration_sec else ""
    top = f"\033[90m╭─\033[1;32m{title}\033[0;90m{'─' * max(0, box_w - 2 - len(title) - 2)}╮\033[0m"

    if dur_tag:
        bot_fill = max(0, box_w - 2 - len(dur_tag) - 3)
        bot = f"\033[90m╰{'─' * bot_fill} \033[90m{dur_tag}\033[0;90m──╯\033[0m"
    else:
        bot = f"\033[90m╰{'─' * (box_w - 2)}╯\033[0m"

    rendered_body = render_markdown(text, indent="  ", width=inner_w)
    out = [top]
    for bl in rendered_body.splitlines():
        out.append(f"\033[90m│\033[0m{bl}")
    out.append(bot)
    return "\n".join(out)

