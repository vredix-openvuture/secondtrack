"""Minimal, safe Markdown renderer for short note fields.

Supports: task lists (- [ ] / - [x]), bullet & numbered lists, headings (#..###),
**bold**, *italic*, `code`, and paragraphs. Input is HTML-escaped first, so it is
safe to render as Markup."""
from __future__ import annotations

import html
import re

from markupsafe import Markup

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_CODE = re.compile(r"`([^`]+?)`")

_TASK = re.compile(r"^[-*] \[([ xX])\] (.*)$")
_UL = re.compile(r"^[-*] (.*)$")
_OL = re.compile(r"^(\d+)\. (.*)$")
_H = re.compile(r"^(#{1,3}) (.*)$")


def _inline(s: str) -> str:
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    s = _ITALIC.sub(r"<em>\1</em>", s)
    s = _CODE.sub(r"<code>\1</code>", s)
    return s


def render(text: str | None) -> Markup:
    if not text:
        return Markup("")
    lines = html.escape(text).replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    i, n = 0, len(lines)

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()

    while i < n:
        s = lines[i].strip()

        m = _TASK.match(s)
        if m:
            flush_para()
            items = []
            while i < n and _TASK.match(lines[i].strip()):
                mm = _TASK.match(lines[i].strip())
                chk = " checked" if mm.group(1).lower() == "x" else ""
                items.append(f'<li><input type="checkbox" disabled{chk}> {_inline(mm.group(2))}</li>')
                i += 1
            out.append('<ul class="md-task">' + "".join(items) + "</ul>")
            continue

        if _UL.match(s) and not _TASK.match(s):
            flush_para()
            items = []
            while i < n and _UL.match(lines[i].strip()) and not _TASK.match(lines[i].strip()):
                items.append("<li>" + _inline(_UL.match(lines[i].strip()).group(1)) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue

        if _OL.match(s):
            flush_para()
            items = []
            while i < n and _OL.match(lines[i].strip()):
                items.append("<li>" + _inline(_OL.match(lines[i].strip()).group(2)) + "</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue

        m = _H.match(s)
        if m:
            flush_para()
            lvl = len(m.group(1)) + 2
            out.append(f"<h{lvl}>" + _inline(m.group(2)) + f"</h{lvl}>")
            i += 1
            continue

        if not s:
            flush_para()
            i += 1
            continue

        para.append(s)
        i += 1

    flush_para()
    return Markup("".join(out))
