"""Unit tests for URL canonicalization and tracking parameter stripping."""

from substack_saved_mcp.url_utils import canonicalize_url


def test_canonicalize_url_strips_tracking_params():
    url_with_tracking = "https://example.substack.com/p/my-awesome-post?utm_source=substack&utm_medium=email&r=1a2b3&s=r#comments"
    clean = canonicalize_url(url_with_tracking)
    assert clean == "https://example.substack.com/p/my-awesome-post"


def test_canonicalize_url_trailing_slash():
    url_with_slash = "https://testpub.substack.com/p/another-post/"
    clean = canonicalize_url(url_with_slash)
    assert clean == "https://testpub.substack.com/p/another-post"


def test_canonicalize_url_empty_and_normal():
    assert canonicalize_url("") == ""
    assert canonicalize_url("https://normal.com/post") == "https://normal.com/post"
