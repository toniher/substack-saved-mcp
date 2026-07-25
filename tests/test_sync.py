"""Unit and integration tests for the sync engine using mock Substack client."""

from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

from substack_saved_mcp.database import get_status, init_db, list_posts
from substack_saved_mcp.models import SavedPost
from substack_saved_mcp.substack_client import AuthRequiredError, SubstackSavedPostsClient
from substack_saved_mcp.sync import sync_saved_posts


class MockSubstackClient(SubstackSavedPostsClient):
    """Mock client returning static test fixture payloads."""

    def __init__(self, pages: List[List[Dict[str, Any]]], should_raise_auth: bool = False):
        self.pages = pages
        self.should_raise_auth = should_raise_auth

    def fetch_saved_posts_page(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        if self.should_raise_auth:
            raise AuthRequiredError("Session expired in mock.")
        page_idx = offset // limit
        if page_idx < len(self.pages):
            return self.pages[page_idx]
        return []


def test_sync_saved_posts_success(tmp_path: Path):
    db_path = tmp_path / "sync_test.sqlite"
    mock_payloads = [[
        {
            "created_at": "2026-06-01T10:00:00Z",
            "post": {
                "id": 101,
                "title": "Post 1",
                "canonical_url": "https://pub1.substack.com/p/post-1",
                "publication": {"name": "Pub 1"},
            },
        },
        {
            "created_at": "2026-06-02T10:00:00Z",
            "post": {
                "id": 102,
                "title": "Post 2",
                "canonical_url": "https://pub1.substack.com/p/post-2",
                "publication": {"name": "Pub 1"},
            },
        },
    ]]

    client = MockSubstackClient(pages=mock_payloads)
    run = sync_saved_posts(force=True, db_path=db_path, client=client)

    assert run.status == "success"
    assert run.fetched_count == 2
    assert run.upserted_count == 2

    posts = list_posts(db_path=db_path)
    assert len(posts) == 2


def test_sync_saved_posts_auth_required(tmp_path: Path):
    db_path = tmp_path / "auth_test.sqlite"
    client = MockSubstackClient(pages=[], should_raise_auth=True)

    run = sync_saved_posts(force=False, db_path=db_path, client=client)
    assert run.status == "auth_required"
    assert "Session expired" in run.error_message

    st = get_status(db_path)
    assert st.last_sync_status == "auth_required"
