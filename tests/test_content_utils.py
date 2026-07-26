"""Unit tests for HTML-to-LLM-text conversion of Substack post body content."""

from substack_saved_mcp.content_utils import format_post_for_llm, html_to_llm_text


def test_html_to_llm_text_paragraphs_and_headings():
    html = "<h2>Section</h2><p>First paragraph.</p><p>Second paragraph.</p>"
    text = html_to_llm_text(html)
    assert text == "## Section\n\nFirst paragraph.\n\nSecond paragraph."


def test_html_to_llm_text_list_items():
    html = "<p>Intro:</p><ul><li>One</li><li>Two</li></ul>"
    text = html_to_llm_text(html)
    assert "- One" in text
    assert "- Two" in text
    assert "Intro:" in text


def test_html_to_llm_text_links_keep_url():
    html = "<p>See <a href='https://example.com/post'>this post</a> for more.</p>"
    text = html_to_llm_text(html)
    assert "this post (https://example.com/post)" in text


def test_html_to_llm_text_bold_and_italic_markers():
    html = "<p><strong>Bold</strong> and <em>italic</em> text.</p>"
    text = html_to_llm_text(html)
    assert "**Bold**" in text
    assert "*italic*" in text


def test_html_to_llm_text_strips_script_and_style():
    html = "<p>Visible</p><script>alert('x')</script><style>.a{color:red}</style>"
    text = html_to_llm_text(html)
    assert text == "Visible"


def test_html_to_llm_text_empty_input():
    assert html_to_llm_text("") == ""
    assert html_to_llm_text(None) == ""


def test_format_post_for_llm_includes_metadata_header():
    doc = format_post_for_llm(
        title="My Post",
        publication_name="My Pub",
        url="https://pub.substack.com/p/my-post",
        body_text="Body content here.",
        author_name="Jane Doe",
        published_at="2026-07-01T10:00:00Z",
    )
    assert doc.startswith("Title: My Post")
    assert "Publication: My Pub" in doc
    assert "Author: Jane Doe" in doc
    assert "Published: 2026-07-01T10:00:00Z" in doc
    assert "URL: https://pub.substack.com/p/my-post" in doc
    assert doc.endswith("Body content here.")


def test_format_post_for_llm_omits_missing_optional_fields():
    doc = format_post_for_llm(
        title="My Post",
        publication_name="My Pub",
        url="https://pub.substack.com/p/my-post",
        body_text="Body content here.",
    )
    assert "Author:" not in doc
    assert "Published:" not in doc
