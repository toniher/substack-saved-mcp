"""Integration tests for FastMCP tools and resource handlers."""

from pathlib import Path
from unittest.mock import patch
import pytest

from substack_saved_mcp.database import init_db, upsert_post
from substack_saved_mcp.mcp_server import (
    get_saved_post,
    get_post_resource,
    get_publications_resource,
    list_audiences,
    list_publications,
    list_saved_posts,
    save_post,
    saved_posts_status,
    search_saved_posts,
    unsave_post,
)
from substack_saved_mcp.models import SavedPost


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


def test_mcp_unsave_tool(setup_test_db: Path):
    p = SavedPost(
        url="https://test.substack.com/p/to-unsave",
        title="Post To Unsave",
        publication_name="Test Pub",
        is_saved=1,
    )
    saved = upsert_post(p, setup_test_db)

    with patch("substack_saved_mcp.mcp_server.SubstackSavedPostsClient") as mock_client_cls:
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

    with patch("substack_saved_mcp.mcp_server.SubstackSavedPostsClient") as mock_client_cls:
        mock_client_cls.return_value.unsave_post.return_value = "not_found"
        res = unsave_post(saved.url)
        assert res["success"] is True
        assert res["remote_confirmed"] is False
        assert "Warning" in res["message"]
        assert "not_found" in res["message"]

    # Local soft-delete must still have happened despite the unconfirmed remote state.
    post_after = get_saved_post(saved.url)
    assert post_after.is_saved == 0


def test_mcp_save_tool_surfaces_unconfirmed_warning(setup_test_db: Path):
    with patch("substack_saved_mcp.mcp_server.SubstackSavedPostsClient") as mock_client_cls:
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
