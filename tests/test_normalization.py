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
    assert post.audience == "only_paid"
    assert post.is_paywalled == 1


def test_parse_remote_post_reader_api_shape():
    """Flat post object from /api/v1/reader/posts, enriched by the client with a
    publication object and author_name, carrying the real inline saved_at."""
    raw_post = {
        "id": 173764217,
        "publication_id": 289208,
        "title": "How to share your point of view",
        "canonical_url": "https://newsletter.weskao.com/p/fundamentals-how-to-share-your-point?utm_source=%2Finbox%2Fsaved",
        "post_date": "2026-06-10T12:01:41.003Z",
        "description": "Sharing your point of view is one of the best ways to add value.",
        "audience": "everyone",
        "is_saved": True,
        "saved_at": "2026-06-10T13:46:19.832Z",
        # Fields the client attaches during enrichment:
        "publication": {"name": "Wes Kao's Newsletter", "subdomain": "newsletter.weskao"},
        "author_name": "Wes Kao",
    }

    post = parse_remote_post(raw_post)
    assert post.substack_post_id == "173764217"
    assert post.url == "https://newsletter.weskao.com/p/fundamentals-how-to-share-your-point"
    assert post.title == "How to share your point of view"
    assert post.publication_name == "Wes Kao's Newsletter"
    assert post.author_name == "Wes Kao"
    assert post.published_at == "2026-06-10T12:01:41.003Z"
    # The real Substack bookmark time, not the sync moment.
    assert post.saved_at == "2026-06-10T13:46:19.832Z"
    assert post.audience == "everyone"
    assert post.is_paywalled == 0
