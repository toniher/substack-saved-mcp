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


def test_sync_saved_posts_multipage(tmp_path: Path):
    db_path = tmp_path / "multipage_test.sqlite"
    # Page 1 has 50 posts, Page 2 has 15 posts
    page1 = [
        {
            "created_at": f"2026-06-01T10:{i:02d}:00Z",
            "post": {
                "id": 1000 + i,
                "title": f"Post {i}",
                "canonical_url": f"https://pub1.substack.com/p/post-{i}",
                "publication": {"name": "Pub 1"},
            },
        }
        for i in range(50)
    ]
    page2 = [
        {
            "created_at": f"2026-06-02T10:{i:02d}:00Z",
            "post": {
                "id": 2000 + i,
                "title": f"Post Page 2-{i}",
                "canonical_url": f"https://pub1.substack.com/p/post-page2-{i}",
                "publication": {"name": "Pub 1"},
            },
        }
        for i in range(15)
    ]

    client = MockSubstackClient(pages=[page1, page2])
    run = sync_saved_posts(force=True, db_path=db_path, client=client)

    assert run.status == "success"
    assert run.fetched_count == 65
    assert run.upserted_count == 65

    st = get_status(db_path)
    assert st.total_saved_posts == 65


def test_dom_scrolling_mock(tmp_path: Path):
    state_file = tmp_path / "storage_state.json"
    state_file.write_text('{"cookies": [], "origins": []}')
    client = SubstackSavedPostsClient(storage_state_path=state_file)

    class MockCard:
        def __init__(self, href, text):
            self.href = href
            self.text = text

        def get_attribute(self, attr):
            return self.href

        def inner_text(self):
            return self.text

    class MockPage:
        url = "https://substack.com/saved"

        def __init__(self, total_cards):
            self.total_cards = total_cards
            self.scroll_count = 0

        def goto(self, url, wait_until=None):
            pass

        def locator(self, sel):
            # Simulate 12 cards on initial load, then +12 per scroll
            available = min(12 + self.scroll_count * 12, self.total_cards)
            cards = [
                MockCard(f"https://pub.substack.com/p/post-{i}", f"Title Post {i}")
                for i in range(available)
            ]
            class MockLoc:
                def count(self):
                    return 0
                def all(self_loc):
                    return cards
            return MockLoc()

        def evaluate(self, script):
            self.scroll_count += 1

        def wait_for_timeout(self, ms):
            pass

    class MockContext:
        def new_page(self):
            return MockPage(total_cards=30)

    class MockBrowser:
        def new_context(self, storage_state):
            return MockContext()

        def close(self):
            pass

    class MockPlaywright:
        class chromium:
            @staticmethod
            def launch(headless):
                return MockBrowser()

    results = client._fetch_via_dom(offset=0, limit=50, playwright_instance=MockPlaywright())
    assert len(results) == 30
    assert results[0]["canonical_url"] == "https://pub.substack.com/p/post-0"
    assert results[29]["canonical_url"] == "https://pub.substack.com/p/post-29"

