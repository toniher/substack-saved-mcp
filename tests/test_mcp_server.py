"""Integration tests for FastMCP tools and resource handlers."""

from pathlib import Path
from unittest.mock import patch
import pytest

from substack_saved_mcp.database import init_db, upsert_post
from substack_saved_mcp.mcp_server import (
    get_saved_post,
    get_post_resource,
    get_publications_resource,
    list_publications,
    list_saved_posts,
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
        is_saved=1,
    )
    upsert_post(p1, setup_test_db)

    # 1. search_saved_posts
    search_res = search_saved_posts(query="agents")
    assert len(search_res) == 1
    assert search_res[0].title == "Building AI Agents"

    # 2. list_saved_posts
    list_res = list_saved_posts(limit=10)
    assert len(list_res) == 1
    assert list_res[0].publication_name == "Tech Insight"

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
        mock_client_cls.return_value.unsave_post.return_value = True
        res = unsave_post(saved.url)
        assert res["success"] is True
        assert "Successfully unsaved" in res["message"]

    post_after = get_saved_post(saved.url)
    assert post_after.is_saved == 0
