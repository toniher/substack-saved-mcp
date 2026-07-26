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

    class MockElement:
        def __init__(self, present=True, href=None, text=None):
            self._present = present
            self._href = href
            self._text = text

        @property
        def first(self):
            return self

        def count(self):
            return 1 if self._present else 0

        def get_attribute(self, attr):
            return self._href

        def inner_text(self):
            return self._text or ""

        def is_visible(self):
            return False

        def click(self, **kwargs):
            pass

    class MockCard:
        """Simulates a div.reader2-post-container with child selectors."""

        def __init__(self, i):
            self.i = i

        def locator(self, sel):
            if "a[href" in sel:
                return MockElement(href=f"https://pub.substack.com/p/post-{self.i}")
            if "reader2-post-title" in sel:
                return MockElement(text=f"Title Post {self.i}")
            if "pub-name" in sel:
                return MockElement(text="Pub Name")
            if "reader2-paragraph" in sel:
                return MockElement(text="An excerpt.")
            if "inbox-item-timestamp" in sel:
                return MockElement(text="1 de jul.")
            if "reader2-item-meta" in sel:
                return MockElement(text="Author Name∙8 min read")
            return MockElement(present=False)

    class MockLoc:
        def __init__(self, cards=None):
            self._cards = cards or []

        def count(self):
            return 0

        def all(self):
            return self._cards

        @property
        def first(self):
            return MockElement(present=False)

    class MockPage:
        url = "https://substack.com/saved"

        def __init__(self, total_cards):
            self.total_cards = total_cards
            self.scroll_count = 0

        def goto(self, url, **kwargs):
            pass

        def locator(self, sel):
            if "reader2-post-container" in sel:
                # Simulate 12 cards on initial load, then +12 per scroll
                available = min(12 + self.scroll_count * 12, self.total_cards)
                return MockLoc(cards=[MockCard(i) for i in range(available)])
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
    # Publication date and title must be extracted from the card, not left None.
    assert results[0]["title"] == "Title Post 0"
    assert results[0]["published_at"] == "1 de jul."
    assert results[0]["publication_name"] == "Pub Name"
    assert results[0]["author_name"] == "Author Name"


def test_reader_api_cursor_pagination(tmp_path: Path):
    state_file = tmp_path / "storage_state.json"
    state_file.write_text('{"cookies": [], "origins": []}')
    client = SubstackSavedPostsClient(storage_state_path=state_file)

    # Two pages of 2 posts each, ordered newest-saved first; the second page is
    # requested with after=<oldest saved_at of page 1>.
    pages = {
        "2999-01-01T00:00:00.000Z": {
            "posts": [
                {"id": 1, "publication_id": 10, "title": "P1",
                 "canonical_url": "https://a.substack.com/p/one",
                 "post_date": "2026-06-10T00:00:00Z", "saved_at": "2026-06-20T00:00:00Z",
                 "publishedBylines": [{"name": "Alice"}]},
                {"id": 2, "publication_id": 11, "title": "P2",
                 "canonical_url": "https://b.substack.com/p/two",
                 "post_date": "2026-06-09T00:00:00Z", "saved_at": "2026-06-18T00:00:00Z",
                 "publishedBylines": [{"name": "Bob"}]},
            ],
            "publications": [{"id": 10, "name": "Pub A"}, {"id": 11, "name": "Pub B"}],
            "more": True,
        },
        "2026-06-18T00:00:00Z": {
            "posts": [
                {"id": 3, "publication_id": 12, "title": "P3",
                 "canonical_url": "https://c.substack.com/p/three",
                 "post_date": "2026-06-08T00:00:00Z", "saved_at": "2026-06-15T00:00:00Z",
                 "publishedBylines": [{"name": "Carol"}]},
            ],
            "publications": [{"id": 12, "name": "Pub C"}],
            "more": False,
        },
    }

    class MockResponse:
        def __init__(self, payload):
            self._payload = payload
            self.status = 200
            self.ok = True
            self.url = "https://substack.com/api/v1/reader/posts"

        def json(self):
            return self._payload

    class MockApiContext:
        def get(self, url):
            after = None
            if "after=" in url:
                from urllib.parse import unquote
                after = unquote(url.split("after=")[1].split("&")[0])
            return MockResponse(pages[after])

    posts = client._fetch_all_saved_via_reader_api(MockApiContext(), page_size=2)

    assert [p["id"] for p in posts] == [1, 2, 3]
    # Real inline bookmark timestamps are preserved.
    assert posts[0]["saved_at"] == "2026-06-20T00:00:00Z"
    assert posts[2]["saved_at"] == "2026-06-15T00:00:00Z"
    # Client enriches each post with its publication object and author.
    assert posts[0]["publication"]["name"] == "Pub A"
    assert posts[0]["author_name"] == "Alice"

