"""Integration tests for FastMCP tools and resource handlers."""

from pathlib import Path
from unittest.mock import patch

import pytest

from substack_saved_mcp.database import init_db, upsert_note, upsert_post
from substack_saved_mcp.mcp_server import (
    get_note_content,
    get_note_resource,
    get_post_content,
    get_post_resource,
    get_publications_resource,
    get_saved_note,
    get_saved_post,
    list_audiences,
    list_publications,
    list_saved_notes,
    list_saved_posts,
    save_note,
    save_post,
    saved_posts_status,
    search_saved_notes,
    search_saved_posts,
    unsave_note,
    unsave_post,
)
from substack_saved_mcp.models import SavedNote, SavedPost


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path: Path, monkeypatch):
    test_db = tmp_path / "mcp_test.sqlite"
    monkeypatch.setenv("SUBSTACK_SAVED_DB_PATH", str(test_db))
    init_db(test_db)
    return test_db


def test_mcp_tools_flow(setup_test_db: Path):
    # Insert sample posts
    p1 = SavedPost(
        url="https://tech.substack.com/p/ai-agents",
        title="Building AI Agents",
        publication_name="Tech Insight",
        author_name="Alice",
        published_at="2026-07-01T10:00:00Z",
        saved_at="2026-07-02T10:00:00Z",
        excerpt="An practical guide to building autonomous AI coding agents.",
        audience="only_paid",
        is_saved=1,
    )
    upsert_post(p1, setup_test_db)

    # 1. search_saved_posts
    search_res = search_saved_posts(query="agents")
    assert len(search_res) == 1
    assert search_res[0].title == "Building AI Agents"
    assert search_res[0].audience == "only_paid"

    # 1b. search_saved_posts with an audience filter
    assert search_saved_posts(query="agents", audience="everyone") == []
    assert len(search_saved_posts(query="agents", audience="only_paid")) == 1

    # 2. list_saved_posts
    list_res = list_saved_posts(limit=10)
    assert len(list_res) == 1
    assert list_res[0].publication_name == "Tech Insight"

    # 2b. list_saved_posts with an audience filter
    assert list_saved_posts(audience="everyone") == []
    assert len(list_saved_posts(audience="only_paid")) == 1

    # 2c. list_audiences
    tiers = list_audiences()
    assert len(tiers) == 1
    assert tiers[0].audience == "only_paid"
    assert tiers[0].post_count == 1

    # 3. get_saved_post
    post_res = get_saved_post("https://tech.substack.com/p/ai-agents")
    assert post_res is not None
    assert post_res.title == "Building AI Agents"

    # 4. list_publications
    pubs = list_publications()
    assert len(pubs) == 1
    assert pubs[0].publication_name == "Tech Insight"

    # 5. status
    st = saved_posts_status()
    assert st.total_saved_posts == 1

    # 6. resources
    res_str = get_post_resource("https://tech.substack.com/p/ai-agents")
    assert "Building AI Agents" in res_str

    pubs_res_str = get_publications_resource()
    assert "Tech Insight" in pubs_res_str


def test_mcp_read_state_filter_reaches_database(setup_test_db: Path):
    upsert_post(
        SavedPost(
            url="https://tech.substack.com/p/unread",
            title="Unread Post",
            publication_name="Tech Insight",
            excerpt="progress filter test",
            is_saved=1,
        ),
        setup_test_db,
    )
    upsert_post(
        SavedPost(
            url="https://tech.substack.com/p/finished",
            title="Finished Post",
            publication_name="Tech Insight",
            excerpt="progress filter test",
            word_count=1000,
            max_read_progress=0.99,
            is_viewed=1,
            is_saved=1,
        ),
        setup_test_db,
    )

    listed = list_saved_posts(read_state="finished")
    assert len(listed) == 1
    assert listed[0].title == "Finished Post"
    assert listed[0].is_fully_read is True
    assert listed[0].minutes_remaining == 1  # ceil(1000 * 0.01 / 200)

    searched = search_saved_posts(query="progress", read_state="unread")
    assert len(searched) == 1
    assert searched[0].title == "Unread Post"


def test_mcp_read_state_invalid_value_raises(setup_test_db: Path):
    with pytest.raises(ValueError):
        list_saved_posts(read_state="bogus")

    with pytest.raises(ValueError):
        search_saved_posts(query="progress", read_state="bogus")


def test_mcp_unsave_tool(setup_test_db: Path):
    p = SavedPost(
        url="https://test.substack.com/p/to-unsave",
        title="Post To Unsave",
        publication_name="Test Pub",
        is_saved=1,
    )
    saved = upsert_post(p, setup_test_db)

    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.unsave_post.return_value = "confirmed"
        res = unsave_post(saved.url)
        assert res["success"] is True
        assert res["remote_confirmed"] is True
        assert "Successfully unsaved" in res["message"]
        assert "Warning" not in res["message"]

    post_after = get_saved_post(saved.url)
    assert post_after.is_saved == 0


def test_mcp_unsave_tool_surfaces_unconfirmed_warning(setup_test_db: Path):
    """When the remote toggle can't be confirmed, local soft-delete must still
    happen, but the tool response should clearly flag the unconfirmed state."""
    p = SavedPost(
        url="https://test.substack.com/p/unconfirmed-unsave",
        title="Post With Unconfirmed Unsave",
        publication_name="Test Pub",
        is_saved=1,
    )
    saved = upsert_post(p, setup_test_db)

    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.unsave_post.return_value = "not_found"
        res = unsave_post(saved.url)
        assert res["success"] is True
        assert res["remote_confirmed"] is False
        assert "Warning" in res["message"]
        assert "not_found" in res["message"]

    # Local soft-delete must still have happened despite the unconfirmed remote state.
    post_after = get_saved_post(saved.url)
    assert post_after.is_saved == 0


def test_mcp_get_post_content_not_found(setup_test_db: Path):
    res = get_post_content("https://missing.substack.com/p/nope")
    assert res["success"] is False
    assert "not found" in res["message"]


def test_mcp_get_post_content_fetches_and_caches(setup_test_db: Path):
    p = SavedPost(
        url="https://test.substack.com/p/full-content",
        title="Full Content Post",
        publication_name="Test Pub",
        is_saved=1,
    )
    saved = upsert_post(p, setup_test_db)

    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.fetch_post_content.return_value = {
            "body_html": "<p>Hello world.</p>",
            "title": "Full Content Post",
            "audience": "everyone",
        }
        res = get_post_content(saved.url)
        assert res["success"] is True
        assert res["cached"] is False
        assert "Hello world." in res["content"]
        assert "Title: Full Content Post" in res["content"]

    # Second call should use the now-cached content_text without calling the client again.
    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        res2 = get_post_content(saved.url)
        assert res2["success"] is True
        assert res2["cached"] is True
        assert "Hello world." in res2["content"]
        mock_client_cls.return_value.fetch_post_content.assert_not_called()


def test_mcp_get_post_content_reports_missing_body(setup_test_db: Path):
    p = SavedPost(
        url="https://test.substack.com/p/no-body",
        title="No Body Post",
        publication_name="Test Pub",
        is_saved=1,
    )
    saved = upsert_post(p, setup_test_db)

    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.fetch_post_content.return_value = {
            "body_html": None
        }
        res = get_post_content(saved.url)
        assert res["success"] is False
        assert "inspect-network" in res["message"]


def test_mcp_save_tool_surfaces_unconfirmed_warning(setup_test_db: Path):
    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.save_post.return_value = (
            SavedPost(
                url="https://test.substack.com/p/new-post",
                title="Newly Saved Post",
                publication_name="Test Pub",
                is_saved=1,
            ),
            "unconfirmed",
        )
        res = save_post("https://test.substack.com/p/new-post")
        assert res["success"] is True
        assert res["remote_confirmed"] is False
        assert "warning" in res
        assert res["post"].title == "Newly Saved Post"

    cached = get_saved_post("https://test.substack.com/p/new-post")
    assert cached is not None
    assert cached.is_saved == 1


def test_mcp_notes_flow(setup_test_db: Path):
    n1 = SavedNote(
        substack_note_id="1",
        url="https://substack.com/@alice/note/c-1",
        body_text="A note about building AI agents",
        author_name="Alice",
        author_handle="alice",
        posted_at="2026-07-01T10:00:00Z",
        is_saved=1,
    )
    upsert_note(n1, setup_test_db)

    search_res = search_saved_notes(query="agents")
    assert len(search_res) == 1
    assert search_res[0].author_handle == "alice"

    list_res = list_saved_notes(limit=10)
    assert len(list_res) == 1

    full = get_saved_note("https://substack.com/@alice/note/c-1")
    assert full is not None
    assert full.body_text == "A note about building AI agents"

    resource_json = get_note_resource("https://substack.com/@alice/note/c-1")
    assert "alice" in resource_json

    missing_resource = get_note_resource("https://substack.com/@nobody/note/c-999")
    assert "not found" in missing_resource


def test_mcp_save_note_tool(setup_test_db: Path):
    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.save_note.return_value = (
            SavedNote(
                substack_note_id="1",
                url="https://substack.com/@alice/note/c-1",
                body_text="A new note",
                author_handle="alice",
                is_saved=1,
            ),
            "confirmed",
        )
        res = save_note("https://substack.com/@alice/note/c-1")
        assert res["success"] is True
        assert res["remote_confirmed"] is True
        assert "warning" not in res
        assert res["note"].author_handle == "alice"

    cached = get_saved_note("https://substack.com/@alice/note/c-1")
    assert cached is not None


def test_mcp_save_note_tool_surfaces_unconfirmed_warning(setup_test_db: Path):
    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.save_note.return_value = (
            SavedNote(
                substack_note_id="1",
                url="https://substack.com/@alice/note/c-1",
                body_text="A new note",
                author_handle="alice",
                is_saved=1,
            ),
            "unconfirmed",
        )
        res = save_note("https://substack.com/@alice/note/c-1")
        assert res["success"] is True
        assert res["remote_confirmed"] is False
        assert "warning" in res


def test_mcp_unsave_note_tool(setup_test_db: Path):
    n = SavedNote(
        substack_note_id="1",
        url="https://substack.com/@alice/note/c-1",
        author_handle="alice",
        is_saved=1,
    )
    saved = upsert_note(n, setup_test_db)

    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.unsave_note.return_value = "confirmed"
        res = unsave_note(saved.url)
        assert res["success"] is True
        assert res["remote_confirmed"] is True
        assert "Warning" not in res["message"]

    note_after = get_saved_note(saved.url)
    assert note_after.is_saved == 0


def test_mcp_unsave_note_tool_surfaces_unconfirmed_warning(setup_test_db: Path):
    n = SavedNote(
        substack_note_id="1",
        url="https://substack.com/@alice/note/c-1",
        author_handle="alice",
        is_saved=1,
    )
    saved = upsert_note(n, setup_test_db)

    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.unsave_note.return_value = "not_found"
        res = unsave_note(saved.url)
        assert res["success"] is True
        assert res["remote_confirmed"] is False
        assert "Warning" in res["message"]

    note_after = get_saved_note(saved.url)
    assert note_after.is_saved == 0


def test_mcp_get_note_content_not_found(setup_test_db: Path):
    res = get_note_content("https://substack.com/@nobody/note/c-999")
    assert res["success"] is False
    assert "not found" in res["message"]


def test_mcp_get_note_content_fetches_and_caches(setup_test_db: Path):
    n = SavedNote(
        substack_note_id="1",
        url="https://substack.com/@alice/note/c-1",
        author_handle="alice",
    )
    upsert_note(n, setup_test_db)

    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        mock_client_cls.return_value.fetch_note_content.return_value = {
            "body_text": "Full note content.",
            "body_raw": "Full note content.",
            "body_format": "text",
        }
        res = get_note_content("https://substack.com/@alice/note/c-1")
        assert res["success"] is True
        assert "Full note content." in res["content"]
        assert res["cached"] is False

    cached = get_saved_note("https://substack.com/@alice/note/c-1")
    assert cached.body_text == "Full note content."

    # A second call should serve from cache without touching the client.
    with patch(
        "substack_saved_mcp.mcp_server.SubstackSavedPostsClient"
    ) as mock_client_cls:
        res2 = get_note_content("https://substack.com/@alice/note/c-1")
        assert res2["cached"] is True
        mock_client_cls.assert_not_called()
