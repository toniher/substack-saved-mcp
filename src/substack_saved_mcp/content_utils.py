"""Convert Substack post HTML content into clean text suitable for feeding to an LLM."""

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
_HEADING_PREFIX = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}


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
        raw = "".join(self._out)
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
