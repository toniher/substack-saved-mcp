"""Unit tests for remote Substack API payload parsing and normalization."""

from substack_saved_mcp.sync import parse_remote_post


def test_parse_remote_post_json_payload():
    raw_payload = {
        "created_at": "2026-05-10T14:30:00.000Z",
        "post": {
            "id": 1234567,
            "title": "Understanding Neural Networks",
            "canonical_url": "https://deeplearning.substack.com/p/neural-networks?utm_source=substack",
            "description": "A thorough guide to backpropagation and gradient descent.",
            "post_date": "2026-05-01T08:00:00.000Z",
            "audience": "only_paid",
            "publication": {
                "name": "Deep Learning Weekly",
                "subdomain": "deeplearning",
                "author_name": "Dr. AI",
            },
        },
    }

    post = parse_remote_post(raw_payload)
    assert post.substack_post_id == "1234567"
    assert post.url == "https://deeplearning.substack.com/p/neural-networks"
    assert post.title == "Understanding Neural Networks"
    assert post.publication_name == "Deep Learning Weekly"
    assert post.author_name == "Dr. AI"
    assert post.published_at == "2026-05-01T08:00:00.000Z"
    assert post.saved_at == "2026-05-10T14:30:00.000Z"
    assert post.is_paywalled == 1
