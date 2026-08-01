"""Unit tests for remote Substack API payload parsing and normalization."""

import pytest

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
        "publication": {
            "name": "Wes Kao's Newsletter",
            "subdomain": "newsletter.weskao",
        },
        "author_name": "Wes Kao",
    }

    post = parse_remote_post(raw_post)
    assert post.substack_post_id == "173764217"
    assert (
        post.url
        == "https://newsletter.weskao.com/p/fundamentals-how-to-share-your-point"
    )
    assert post.title == "How to share your point of view"
    assert post.publication_name == "Wes Kao's Newsletter"
    assert post.author_name == "Wes Kao"
    assert post.published_at == "2026-06-10T12:01:41.003Z"
    # The real Substack bookmark time, not the sync moment.
    assert post.saved_at == "2026-06-10T13:46:19.832Z"
    assert post.audience == "everyone"
    assert post.is_paywalled == 0


@pytest.mark.parametrize("field", ["wordcount", "word_count", "words"])
def test_parse_remote_post_maps_word_count_candidates(field):
    """Word count is picked up from any of the likely field-name variants."""
    post = parse_remote_post(
        {
            "id": 1,
            "canonical_url": "https://x.substack.com/p/a",
            field: 850,
        }
    )
    assert post.word_count == 850


def test_parse_remote_post_derives_reading_time_from_word_count():
    """Reading time is derived (ceil at ~200 wpm), not read from a field."""
    # 850 words -> ceil(850/200) = 5 minutes.
    post = parse_remote_post(
        {
            "id": 1,
            "canonical_url": "https://x.substack.com/p/a",
            "wordcount": 850,
        }
    )
    assert post.reading_time_minutes == 5

    # A very short post still rounds up to at least 1 minute.
    short = parse_remote_post(
        {
            "id": 2,
            "canonical_url": "https://x.substack.com/p/b",
            "wordcount": 10,
        }
    )
    assert short.reading_time_minutes == 1


@pytest.mark.parametrize("field", ["cover_image", "image_url"])
def test_parse_remote_post_maps_image_url_candidates(field):
    """image_url is picked up from either likely field-name variant."""
    post = parse_remote_post(
        {
            "id": 1,
            "canonical_url": "https://x.substack.com/p/a",
            field: "https://substackcdn.com/image/fetch/example.jpeg",
        }
    )
    assert post.image_url == "https://substackcdn.com/image/fetch/example.jpeg"


def test_parse_remote_post_word_count_absent_leaves_fields_none():
    """No word count field and no bogus values -> both stay None (not 0)."""
    post = parse_remote_post(
        {
            "id": 1,
            "canonical_url": "https://x.substack.com/p/a",
            "wordcount": 0,  # non-positive is ignored
        }
    )
    assert post.word_count is None
    assert post.reading_time_minutes is None
