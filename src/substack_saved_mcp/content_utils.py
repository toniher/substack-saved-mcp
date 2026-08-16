"""Convert Substack post/note content into clean text suitable for feeding to an LLM."""

import json
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "blockquote",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "tr",
    "table",
    "figure",
    "figcaption",
    "hr",
}
_SKIP_CONTENT_TAGS = {"script", "style", "noscript", "iframe"}
_HEADING_PREFIX = {
    "h1": "# ",
    "h2": "## ",
    "h3": "### ",
    "h4": "#### ",
    "h5": "##### ",
    "h6": "###### ",
}


class _PostBodyToTextParser(HTMLParser):
    """Minimal HTML-to-markdown-ish-text converter for Substack post body HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0
        self._tag_stack: list[str] = []
        self._link_href_stack: list[str | None] = []
        self._list_item_open = False

    def _write(self, text: str) -> None:
        if text:
            self._out.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        self._tag_stack.append(tag)

        if tag == "br":
            self._write("\n")
        elif tag in _HEADING_PREFIX:
            self._write("\n\n" + _HEADING_PREFIX[tag])
        elif tag == "li":
            self._write("\n- ")
            self._list_item_open = True
        elif tag in _BLOCK_TAGS:
            self._write("\n\n")
        elif tag in ("strong", "b"):
            self._write("**")
        elif tag in ("em", "i"):
            self._write("*")
        elif tag == "a":
            href = dict(attrs).get("href")
            self._link_href_stack.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return

        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()

        if tag in ("strong", "b"):
            self._write("**")
        elif tag in ("em", "i"):
            self._write("*")
        elif tag == "a" and self._link_href_stack:
            href = self._link_href_stack.pop()
            if href and not href.startswith("#"):
                self._write(f" ({href})")
        elif tag in _BLOCK_TAGS:
            self._write("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._write(data)

    def get_text(self) -> str:
        return _collapse_blank_lines("".join(self._out))


def _collapse_blank_lines(raw: str) -> str:
    """Strip each line, then collapse runs of blank lines down to one."""
    lines = [line.strip() for line in raw.splitlines()]
    collapsed: list[str] = []
    blank_run = 0
    for line in lines:
        if not line:
            blank_run += 1
            if blank_run <= 1:
                collapsed.append("")
        else:
            blank_run = 0
            collapsed.append(line)
    return "\n".join(collapsed).strip()


def html_to_llm_text(html: str) -> str:
    """Strip a Substack post's ``body_html`` down to clean text for LLM consumption.

    Converts headings to markdown-style ``#`` prefixes, list items to ``- ``
    bullets, links to ``text (url)``, and collapses excess whitespace, while
    dropping script/style/iframe content entirely.
    """
    parser = _PostBodyToTextParser()
    parser.feed(html or "")
    parser.close()
    return parser.get_text()


def format_post_for_llm(
    title: str,
    publication_name: str,
    url: str,
    body_text: str,
    author_name: str | None = None,
    published_at: str | None = None,
) -> str:
    """Assemble a post's metadata and cleaned body text into one LLM-ready document."""
    header_lines = [f"Title: {title}", f"Publication: {publication_name}"]
    if author_name:
        header_lines.append(f"Author: {author_name}")
    if published_at:
        header_lines.append(f"Published: {published_at}")
    header_lines.append(f"URL: {url}")

    return "\n".join(header_lines) + "\n\n" + body_text


_MARK_PREFIX_SUFFIX = {
    "bold": ("**", "**"),
    "italic": ("*", "*"),
}


def _prosemirror_node_to_text(node: dict, *, in_list_item: bool = False) -> str:
    node_type = node.get("type")

    if node_type == "text":
        text = node.get("text") or ""
        link_href = None
        for mark in node.get("marks") or []:
            mark_type = mark.get("type")
            if mark_type == "link":
                link_href = (mark.get("attrs") or {}).get("href")
            elif mark_type in _MARK_PREFIX_SUFFIX:
                prefix, suffix = _MARK_PREFIX_SUFFIX[mark_type]
                text = f"{prefix}{text}{suffix}"
        if link_href:
            text = f"{text} ({link_href})"
        return text

    if node_type == "hard_break":
        return "\n"

    children = node.get("content") or []

    if node_type == "heading":
        level = (node.get("attrs") or {}).get("level", 1)
        prefix = "#" * max(1, min(level, 6)) + " "
        return "\n\n" + prefix + "".join(_prosemirror_node_to_text(c) for c in children)

    if node_type == "listItem":
        inner = "".join(
            _prosemirror_node_to_text(c, in_list_item=True) for c in children
        )
        return "\n- " + inner.strip()

    if node_type in ("bulletList", "orderedList"):
        return "".join(_prosemirror_node_to_text(c) for c in children)

    if node_type in ("paragraph", "doc"):
        joined = "".join(_prosemirror_node_to_text(c) for c in children)
        return joined if in_list_item else "\n\n" + joined

    # Unknown node types: recurse into any content rather than dropping it silently.
    return "".join(_prosemirror_node_to_text(c) for c in children)


def prosemirror_to_llm_text(doc: dict) -> str:
    """Convert a Substack note's ProseMirror ``body_json`` document to clean text.

    Handles paragraphs, headings, ordered/unordered lists, hard breaks, and the
    bold/italic/link marks Substack's note editor produces, matching the
    ``**bold**`` / ``*italic*`` / ``text (url)`` conventions ``html_to_llm_text``
    uses for posts.
    """
    return _collapse_blank_lines(_prosemirror_node_to_text(doc))


def note_body_to_text(raw: object) -> tuple[str, str]:
    """Return (plain_text, body_format) for a note body of unknown provenance.

    Substack's saved-notes API exposes both a pre-flattened plain-text
    ``body`` and a richer ProseMirror ``body_json``; this dispatches on
    whatever the caller actually has. A dict (or a JSON string parsing to
    one) is treated as a ProseMirror doc; a string containing HTML tags
    falls back to the post HTML converter; anything else is used as-is.
    """
    if isinstance(raw, dict):
        return prosemirror_to_llm_text(raw), "prosemirror_json"

    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                return prosemirror_to_llm_text(parsed), "prosemirror_json"
        if "<p" in raw or "<div" in raw or "<a " in raw:
            return html_to_llm_text(raw), "html"
        return _collapse_blank_lines(raw), "text"

    return "", "text"


def format_note_for_llm(
    author_name: str | None,
    author_handle: str | None,
    url: str | None,
    body_text: str,
    posted_at: str | None = None,
    restacked_post_title: str | None = None,
    restacked_post_url: str | None = None,
) -> str:
    """Assemble a note's metadata and cleaned body text into one LLM-ready document."""
    header_lines = []
    if author_name:
        header_lines.append(f"Author: {author_name}")
    if author_handle:
        header_lines.append(f"Handle: @{author_handle}")
    if posted_at:
        header_lines.append(f"Posted: {posted_at}")
    if url:
        header_lines.append(f"URL: {url}")
    if restacked_post_title:
        restack_line = f"Restacked: {restacked_post_title}"
        if restacked_post_url:
            restack_line += f" ({restacked_post_url})"
        header_lines.append(restack_line)

    return "\n".join(header_lines) + "\n\n" + body_text
