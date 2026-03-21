"""Convert Claude's markdown output to Telegram HTML."""
from __future__ import annotations

import re

# Placeholder to protect code blocks from markdown processing
_CODE_BLOCK_PH = "\x00CODEBLOCK{}\x00"
_INLINE_CODE_PH = "\x00INLINE{}\x00"


def md_to_telegram(text: str) -> str:
    """Convert markdown to Telegram-compatible HTML."""
    code_blocks: list[str] = []
    inline_codes: list[str] = []

    # 1. Extract markdown tables → monospace <pre> blocks
    def _save_table(m: re.Match) -> str:
        block = _render_table(m.group(0))
        code_blocks.append(block)
        return _CODE_BLOCK_PH.format(len(code_blocks) - 1)

    text = re.sub(
        r"(?:^\|.+\|[ \t]*\n){2,}",
        _save_table, text, flags=re.MULTILINE,
    )

    # 2. Extract fenced code blocks (``` ... ```)
    def _save_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = _escape_html(m.group(2))
        if lang:
            block = f'<pre><code class="language-{lang}">{code}</code></pre>'
        else:
            block = f"<pre>{code}</pre>"
        code_blocks.append(block)
        return _CODE_BLOCK_PH.format(len(code_blocks) - 1)

    text = re.sub(r"```(\w*)\n(.*?)```", _save_block, text, flags=re.DOTALL)

    # 3. Extract inline code (` ... `)
    def _save_inline(m: re.Match) -> str:
        code = _escape_html(m.group(1))
        inline_codes.append(f"<code>{code}</code>")
        return _INLINE_CODE_PH.format(len(inline_codes) - 1)

    text = re.sub(r"`([^`]+)`", _save_inline, text)

    # 4. Escape HTML in remaining text
    text = _escape_html(text)

    # 5. Convert markdown patterns
    # Headers → bold
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # Bold **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    # Italic *text* or _text_ (but not inside words like file_name)
    text = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", r"<i>\1</i>", text)
    # Strikethrough ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    # Links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    # Blockquotes > text
    text = re.sub(r"^&gt;\s?(.+)$", r"<blockquote>\1</blockquote>", text, flags=re.MULTILINE)

    # 6. Restore code blocks and inline code
    for i, block in enumerate(code_blocks):
        text = text.replace(_escape_html(_CODE_BLOCK_PH.format(i)), block)
    for i, code in enumerate(inline_codes):
        text = text.replace(_escape_html(_INLINE_CODE_PH.format(i)), code)

    return text.strip()


def split_html(html: str, limit: int = 4096) -> list[str]:
    """Split HTML into chunks that don't break <pre> blocks."""
    if len(html) <= limit:
        return [html]

    # Separate code blocks from surrounding text
    segments: list[str] = []
    parts = re.split(r"(<pre(?:\s[^>]*)?>.*?</pre>)", html, flags=re.DOTALL)
    for part in parts:
        if not part:
            continue
        if part.startswith("<pre"):
            segments.append(part)
        else:
            segments.extend(part.split("\n"))

    chunks: list[str] = []
    current = ""
    for seg in segments:
        candidate = (current + "\n" + seg) if current else seg
        if len(candidate) <= limit:
            current = candidate
        else:
            if current.strip():
                chunks.append(current)
            if len(seg) > limit:
                while seg:
                    chunks.append(seg[:limit])
                    seg = seg[limit:]
                current = ""
            else:
                current = seg
    if current.strip():
        chunks.append(current)

    return chunks or [html[:limit]]


def strip_html(html: str) -> str:
    """Strip HTML tags for plain-text fallback."""
    text = re.sub(r"<[^>]+>", "", html)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def _render_table(table_text: str) -> str:
    """Render a markdown table as an aligned monospace <pre> block."""
    rows: list[list[str]] = []
    for line in table_text.strip().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # Skip separator rows (----, :---:, etc.)
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        rows.append(cells)

    if not rows:
        return f"<pre>{_escape_html(table_text.strip())}</pre>"

    # Calculate column widths
    n_cols = max(len(r) for r in rows)
    widths = [0] * n_cols
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Build aligned text
    lines = []
    for ri, row in enumerate(rows):
        parts = []
        for i in range(n_cols):
            val = row[i] if i < len(row) else ""
            parts.append(val.ljust(widths[i]))
        lines.append("  ".join(parts).rstrip())
        # Add underline after header row
        if ri == 0:
            lines.append("  ".join("─" * w for w in widths))

    return f"<pre>{_escape_html(chr(10).join(lines))}</pre>"


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
