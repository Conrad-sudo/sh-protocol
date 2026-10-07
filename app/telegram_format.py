"""
The assistant's replies as Telegram shows them.

The agent writes Markdown, which the web app renders (web/src/components/assistant/ChatMarkdown.tsx).
Telegram has no tables or headings, and its own Markdown dialect refuses a whole message over one
stray character, so a reply is parsed here and rebuilt in the small HTML subset Telegram accepts,
under the web app's rules: raw HTML in the text stays text, an image is never loaded (its alt text
shows instead), and only web and mail links become links.

A table becomes one line per row -- "<b>USDC</b>: 25" -- since a phone has no room for columns.
"""
import html
import re

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

# Telegram takes messages of up to 4096 characters. Replies are cut a little shorter, at a line
# break where there is one, since Telegram counts some characters (most emoji) as two.
MAX_MESSAGE_CHARS = 4000

# Raw HTML stays text, as in the web app; tables and strikethrough are read, as remark-gfm reads them.
_markdown = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

# Link schemes that become links. Anything else keeps its words and loses the link.
_LINK_SCHEMES = ("http://", "https://", "mailto:")

# How far each level of a nested list is indented.
_INDENT = "   "

_TAG = re.compile(r"<[^>]+>")


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """
    Cuts plain text into messages Telegram will take: at the last line break in the second half of
    each piece, or at the limit when there is none.

    @param text   The text.
    @param limit  The longest piece.
    @return       The pieces, in order. None for empty text.
    """
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", limit // 2, limit + 1)
        if cut == -1:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text.strip():
        parts.append(text)
    return parts


def plain_text(fragment: str) -> str:
    """The words of a piece of Telegram HTML made here, without its formatting."""
    return html.unescape(_TAG.sub("", fragment))


def to_telegram_html(markdown: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """
    A reply as Telegram messages, in the HTML Telegram accepts (send with parse_mode HTML).

    Messages break between paragraphs, list items and table rows, so formatting is never cut in
    half. A single block longer than `limit` on its own goes out as plain text, split at line breaks.

    @param markdown  The agent's reply.
    @param limit     The most visible characters in one message.
    @return          The messages, in order. None for an empty reply.
    """
    pieces = []
    for block in SyntaxTreeNode(_markdown.parse(markdown)).children:
        for i, piece in enumerate(_block(block)):
            # Paragraphs stand apart; the items of one list, or the rows of one table, don't.
            pieces.append(("\n" if i else "\n\n", piece))
    return _pack(pieces, limit)


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _inline(node: SyntaxTreeNode, in_link: bool = False) -> str:
    """The text inside `node` with its formatting, as Telegram HTML."""
    out = []
    for child in node.children:
        kind = child.type
        if kind == "text":
            out.append(_escape(child.content))
        elif kind in ("softbreak", "hardbreak"):
            out.append("\n")
        elif kind == "code_inline":
            # Telegram lets nothing sit inside a link but bold, italic and the like.
            out.append(_escape(child.content) if in_link else f"<code>{_escape(child.content)}</code>")
        elif kind == "strong":
            out.append(f"<b>{_inline(child, in_link)}</b>")
        elif kind == "em":
            out.append(f"<i>{_inline(child, in_link)}</i>")
        elif kind == "s":
            out.append(f"<s>{_inline(child, in_link)}</s>")
        elif kind == "link":
            href = child.attrs.get("href", "")
            text = _inline(child, in_link=True)
            if isinstance(href, str) and href.lower().startswith(_LINK_SCHEMES):
                out.append(f'<a href="{html.escape(href)}">{text}</a>')
            else:
                out.append(text)
        elif kind == "image":
            # Never loaded, as in the web app: an image is fetched the moment it shows.
            alt = plain_text(_inline(child))
            out.append(f"[image: {_escape(alt)}]" if alt else "")
        else:
            out.append(_inline(child, in_link) if child.children else _escape(child.content or ""))
    return "".join(out)


def _text_of(node: SyntaxTreeNode) -> str:
    """The formatted text of a block that holds one run of text (a paragraph, heading or cell)."""
    return _inline(node.children[0]) if node.children else ""


def _block(node: SyntaxTreeNode, quoted: bool = False) -> list[str]:
    """
    One block of the reply as Telegram HTML: a single piece, or one per list item or table row.
    """
    kind = node.type
    if kind == "paragraph":
        return [_text_of(node)]
    if kind == "heading":
        return [f"<b>{_text_of(node)}</b>"]
    if kind in ("bullet_list", "ordered_list"):
        return _list_items(node, depth=0)
    if kind == "table":
        return _table_rows(node)
    if kind in ("fence", "code_block"):
        return [f"<pre>{_escape(node.content.rstrip(chr(10)))}</pre>"]
    if kind == "blockquote":
        inner = "\n\n".join("\n".join(_block(child, quoted=True)) for child in node.children)
        # Telegram can't put one quote inside another; an inner one is just its text.
        return [inner if quoted else f"<blockquote>{inner}</blockquote>"]
    if kind == "hr":
        return ["———"]
    return [_escape(node.content)] if node.content else []


def _list_items(node: SyntaxTreeNode, depth: int) -> list[str]:
    """Each item of a list as one piece, its marker in front and any nested list inside it."""
    start = int(node.attrs.get("start", 1)) if node.type == "ordered_list" else None
    indent = _INDENT * depth
    items = []
    for number, item in enumerate(node.children, start=start or 0):
        marker = f"{number}." if start is not None else "•"
        lines = []
        for child in item.children:
            if child.type in ("bullet_list", "ordered_list"):
                lines.extend(_list_items(child, depth + 1))
                continue
            for block in _block(child):
                # The item's later lines line up under its first one.
                block = block.replace("\n", "\n" + indent + _INDENT)
                lines.append(f"{indent}{marker} {block}" if not lines else indent + _INDENT + block)
        items.append("\n".join(lines) if lines else f"{indent}{marker}")
    return items


def _table_rows(node: SyntaxTreeNode) -> list[str]:
    """
    Each row of a table as one line: its first cell in bold, then the others. With more than two
    columns, each cell after the first is labelled with its column's header.
    """
    head = next((part for part in node.children if part.type == "thead"), None)
    headers = [_text_of(cell) for cell in head.children[0].children] if head and head.children else []
    rows = []
    for body in (part for part in node.children if part.type == "tbody"):
        for row in body.children:
            cells = [_text_of(cell) for cell in row.children]
            if not cells:
                continue
            first = re.sub(r"</?b>", "", cells[0])
            rest = cells[1:]
            if len(rest) > 1:
                rest = [
                    f"{headers[i]}: {cell}" if i < len(headers) and headers[i] else cell
                    for i, cell in enumerate(cells[1:], start=1)
                    if cell
                ]
            shown = " · ".join(cell for cell in rest if cell)
            if not first:
                rows.append(shown)
            elif not shown:
                rows.append(f"<b>{first}</b>")
            else:
                rows.append(f"<b>{first}</b>{':' if len(cells) == 2 else ' —'} {shown}")
    if not rows and headers:
        rows.append(" · ".join(header for header in headers if header))
    return rows


def _pack(pieces: list[tuple[str, str]], limit: int) -> list[str]:
    """Fits the pieces into as few messages as the limit allows, keeping each piece whole."""
    messages, current, size = [], "", 0
    for glue, piece in pieces:
        length = len(plain_text(piece))
        if length > limit:
            # Too long even alone: its words go out plain, split at line breaks.
            if current:
                messages.append(current)
                current, size = "", 0
            messages.extend(_escape(part) for part in split_message(plain_text(piece), limit))
        elif current and size + len(glue) + length <= limit:
            current += glue + piece
            size += len(glue) + length
        else:
            if current:
                messages.append(current)
            current, size = piece, length
    if current:
        messages.append(current)
    return [message for message in messages if plain_text(message).strip()]
