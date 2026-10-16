"""正文渲染：HTML 转义 + 三种轻量结构 + 邮件模板。

只识别段落、-/* 列表、| 表格三种结构，不做完整 Markdown 解析。
"""
from __future__ import annotations

import html
from typing import List

from .message import Message

LEVEL_COLORS = {
    "info": "#1a73e8",
    "warn": "#e8710a",
    "error": "#d93025",
}

_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f5f6f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f6f8;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border-radius:8px;overflow:hidden;font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
<tr><td style="background:{COLOR};color:#ffffff;padding:16px 24px;font-size:18px;font-weight:600;">{TITLE}</td></tr>
<tr><td style="padding:8px 24px 0;color:#5f6368;font-size:12px;">{TIMESTAMP} · {HOSTNAME}</td></tr>
<tr><td style="padding:16px 24px 24px;color:#202124;font-size:14px;line-height:1.7;">{BODY}</td></tr>
<tr><td style="padding:12px 24px;background:#fafafa;color:#9aa0a6;font-size:12px;border-top:1px solid #eceff1;">由 notify 自动发送</td></tr>
</table>
</td></tr></table>
</body></html>"""


def escape(text: str) -> str:
    """转义 HTML 元字符（不转义引号，保持正文可读）。"""
    return html.escape(text, quote=False)


def _fill(template: str, **values: str) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _normalise(body: str) -> List[str]:
    return body.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _is_list_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("- ") or stripped.startswith("* ")


def _is_separator(line: str) -> bool:
    """表格分隔行：每个单元格只由 - : 空格组成，且至少含一个 -。"""
    cells = _cells(line)
    if not cells:
        return False
    for cell in cells:
        stripped = cell.strip()
        if not stripped or "-" not in stripped:
            return False
        if set(stripped) - set("-: "):
            return False
    return True


def _is_table_start(lines: List[str], index: int) -> bool:
    if "|" not in lines[index]:
        return False
    if index + 1 >= len(lines):
        return False
    return _is_separator(lines[index + 1])


def _cells(line: str) -> List[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def render_blocks(body: str) -> str:
    """把正文渲染成 HTML 片段。"""
    lines = _normalise(body)
    out: List[str] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]

        if _is_list_line(line):
            items: List[str] = []
            while index < total and _is_list_line(lines[index]):
                items.append("<li>%s</li>" % escape(lines[index].strip()[2:].strip()))
                index += 1
            out.append("<ul>%s</ul>" % "".join(items))
            continue

        if _is_table_start(lines, index):
            header = _cells(lines[index])
            index += 2
            rows: List[List[str]] = []
            while index < total and "|" in lines[index] and lines[index].strip():
                rows.append(_cells(lines[index]))
                index += 1
            head_html = "".join(
                '<th align="left" style="padding:6px 10px;border-bottom:2px solid #eceff1;">%s</th>'
                % escape(cell)
                for cell in header
            )
            body_html = "".join(
                "<tr>%s</tr>"
                % "".join(
                    '<td style="padding:6px 10px;border-bottom:1px solid #f1f3f4;">%s</td>'
                    % escape(cell)
                    for cell in row
                )
                for row in rows
            )
            # 注意：这里不能用 % 格式化，样式里的 width:100% 会被当成格式符
            out.append(
                '<table cellpadding="0" cellspacing="0" '
                'style="border-collapse:collapse;width:100%;font-size:14px;">'
                + "<tr>"
                + head_html
                + "</tr>"
                + body_html
                + "</table>"
            )
            continue

        if not line.strip():
            index += 1
            continue

        paragraph: List[str] = []
        while (
            index < total
            and lines[index].strip()
            and not _is_list_line(lines[index])
            and not _is_table_start(lines, index)
        ):
            paragraph.append(escape(lines[index]))
            index += 1
        out.append("<p>%s</p>" % "<br>".join(paragraph))

    return "\n".join(out)


def render_email_text(message: Message, hostname: str, timestamp: str) -> str:
    return "\n".join(
        [
            message.title,
            "%s · %s" % (timestamp, hostname),
            "",
            message.body.rstrip(),
            "",
            "--",
            "由 notify 自动发送",
            "",
        ]
    )


def render_email_html(message: Message, hostname: str, timestamp: str) -> str:
    return _fill(
        _HTML_TEMPLATE,
        COLOR=LEVEL_COLORS[message.level],
        TITLE=escape(message.title),
        TIMESTAMP=escape(timestamp),
        HOSTNAME=escape(hostname),
        BODY=render_blocks(message.body),
    )
