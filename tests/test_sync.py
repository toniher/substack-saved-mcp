"""Unit and integration tests for the sync engine using mock Substack client."""

from pathlib import Path
from typing import Any

from substack_saved_mcp.database import (
    get_note,
    get_post,
    get_status,
    init_db,
    list_notes,
    list_posts,
    upsert_note,
    upsert_post,
)
from substack_saved_mcp.models import SavedNote, SavedPost
from substack_saved_mcp.substack_client import (
    AuthRequiredError,
    SubstackSavedPostsClient,
)
from substack_saved_mcp.sync import (
    parse_remote_note,
    parse_remote_post,
    sync_saved_notes,
    sync_saved_posts,
)


class MockSubstackClient(SubstackSavedPostsClient):
    """Mock client returning static test fixture payloads."""

    def __init__(
        self, pages: list[list[dict[str, Any]]], should_raise_auth: bool = False
    ):
        self.pages = pages
        self.should_raise_auth = should_raise_auth

    def fetch_saved_posts_page(
        self, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        if self.should_raise_auth:
            raise AuthRequiredError("Session expired in mock.")
        page_idx = offset // limit
        if page_idx < len(self.pages):
            return self.pages[page_idx]
        return []


def test_sync_saved_posts_success(tmp_path: Path):
    db_path = tmp_path / "sync_test.sqlite"
    mock_payloads = [
        [
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
        ]
    ]

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


def test_force_sync_reconciles_removed_posts(tmp_path: Path):
    """A post no longer present in the complete remote saved list must be
    soft-deleted by a force/full sync, not left dangling as is_saved=1."""
    db_path = tmp_path / "reconcile_test.sqlite"
    init_db(db_path)

    stale_post = SavedPost(
        url="https://pub1.substack.com/p/removed-post",
        title="Removed Post",
        publication_name="Pub 1",
        is_saved=1,
    )
    upsert_post(stale_post, db_path=db_path)

    mock_payloads = [
        [
            {
                "created_at": "2026-06-01T10:00:00Z",
                "post": {
                    "id": 101,
                    "title": "Post 1",
                    "canonical_url": "https://pub1.substack.com/p/post-1",
                    "publication": {"name": "Pub 1"},
                },
            },
        ]
    ]
    client = MockSubstackClient(pages=mock_payloads)
    run = sync_saved_posts(force=True, db_path=db_path, client=client)

    assert run.status == "success"
    assert run.reconciled_count == 1

    stale = get_post("https://pub1.substack.com/p/removed-post", db_path=db_path)
    assert stale.is_saved == 0
    assert stale.unsaved_at is not None

    remaining = get_post("https://pub1.substack.com/p/post-1", db_path=db_path)
    assert remaining.is_saved == 1

    posts = list_posts(db_path=db_path)
    assert len(posts) == 1
    assert posts[0].url == "https://pub1.substack.com/p/post-1"


def test_incremental_sync_does_not_reconcile(tmp_path: Path):
    """An incremental sync may only see a partial remote list, so it must never
    soft-delete posts missing from that partial fetch."""
    db_path = tmp_path / "no_reconcile_test.sqlite"
    init_db(db_path)

    other_post = SavedPost(
        url="https://pub1.substack.com/p/still-saved-elsewhere",
        title="Still Saved",
        publication_name="Pub 1",
        is_saved=1,
    )
    upsert_post(other_post, db_path=db_path)

    mock_payloads = [
        [
            {
                "created_at": "2026-06-01T10:00:00Z",
                "post": {
                    "id": 101,
                    "title": "Post 1",
                    "canonical_url": "https://pub1.substack.com/p/post-1",
                    "publication": {"name": "Pub 1"},
                },
            },
        ]
    ]
    client = MockSubstackClient(pages=mock_payloads)
    run = sync_saved_posts(force=False, db_path=db_path, client=client)

    assert run.status == "success"
    assert run.reconciled_count == 0

    other = get_post(
        "https://pub1.substack.com/p/still-saved-elsewhere", db_path=db_path
    )
    assert other.is_saved == 1


def _note_item(
    note_id: int,
    handle: str = "alice",
    body: str = "hello world",
    restacked_post: dict | None = None,
    restacked_pub: dict | None = None,
) -> dict:
    """Build a note item matching the confirmed live shape of
    GET /api/v1/reader/saved?filter=notes."""
    return {
        "entity_key": f"c-{note_id}",
        "type": "comment",
        "publication": restacked_pub,
        "post": restacked_post,
        "comment": {
            "name": handle.capitalize(),
            "handle": handle,
            "id": note_id,
            "body": body,
            "body_json": None,
            "user_id": 1000 + note_id,
            "date": "2026-07-24T16:44:59.938Z",
            "ancestor_path": "",
            "reaction_count": 5,
            "restacks": 2,
            "children_count": 1,
            "attachments": [],
            "is_saved": True,
        },
    }


def test_parse_remote_note_api_shape():
    note = parse_remote_note(_note_item(300984381, handle="nathanbaugh"))
    assert note is not None
    assert note.substack_note_id == "300984381"
    assert note.url == "https://substack.com/@nathanbaugh/note/c-300984381"
    assert note.body_text == "hello world"
    assert note.author_handle == "nathanbaugh"
    assert note.is_restack == 0
    assert note.saved_at is None
    assert note.like_count == 5
    assert note.restack_count == 2
    assert note.reply_count == 1


def test_parse_remote_note_restack():
    item = _note_item(
        1,
        restacked_post={
            "canonical_url": "https://pub.substack.com/p/some-post",
            "title": "Some Post",
        },
        restacked_pub={"name": "Some Pub"},
    )
    note = parse_remote_note(item)
    assert note is not None
    assert note.is_restack == 1
    assert note.restacked_post_url == "https://pub.substack.com/p/some-post"
    assert note.restacked_post_title == "Some Post"
    assert note.restacked_publication_name == "Some Pub"


def test_parse_remote_note_missing_id_is_skipped():
    item = _note_item(1)
    item["comment"]["id"] = None
    assert parse_remote_note(item) is None


def test_parse_remote_note_saved_at_never_fabricated():
    """Substack's saved-notes endpoint never exposes a bookmark timestamp;
    saved_at must stay None rather than being stamped from the sync moment."""
    note = parse_remote_note(_note_item(1))
    assert note is not None
    assert note.saved_at is None


class MockNotesClient(SubstackSavedPostsClient):
    """Mock client returning static test fixture note payloads."""

    def __init__(self, pages: list[list[dict]], should_raise_auth: bool = False):
        self.pages = pages
        self.should_raise_auth = should_raise_auth

    def fetch_saved_notes_page(self, limit: int = 50, offset: int = 0) -> list[dict]:
        if self.should_raise_auth:
            raise AuthRequiredError("Session expired in mock.")
        page_idx = offset // limit
        if page_idx < len(self.pages):
            return self.pages[page_idx]
        return []


def test_sync_saved_notes_success(tmp_path: Path):
    db_path = tmp_path / "notes_sync_test.sqlite"
    client = MockNotesClient(pages=[[_note_item(1), _note_item(2, handle="bob")]])
    run = sync_saved_notes(force=True, db_path=db_path, client=client)

    assert run.status == "success"
    assert run.entity == "note"
    assert run.fetched_count == 2
    assert run.upserted_count == 2

    notes = list_notes(db_path=db_path)
    assert len(notes) == 2


def test_sync_saved_notes_auth_required(tmp_path: Path):
    db_path = tmp_path / "notes_auth_test.sqlite"
    client = MockNotesClient(pages=[], should_raise_auth=True)

    run = sync_saved_notes(force=False, db_path=db_path, client=client)
    assert run.status == "auth_required"
    assert run.entity == "note"

    st = get_status(db_path)
    assert st.last_note_sync_status == "auth_required"
    # The posts side must stay untouched by a notes-only sync.
    assert st.last_sync_status is None


def test_sync_notes_records_entity_note(tmp_path: Path):
    db_path = tmp_path / "notes_entity_test.sqlite"
    client = MockNotesClient(pages=[[_note_item(1)]])
    sync_saved_notes(force=True, db_path=db_path, client=client)

    st = get_status(db_path)
    assert st.last_note_sync_status == "success"
    assert st.last_sync_status is None  # posts sync_runs are untouched


def test_sync_notes_force_reconciles(tmp_path: Path):
    """A note no longer present in the complete remote saved list must be
    soft-deleted by a force/full sync."""
    db_path = tmp_path / "notes_reconcile_test.sqlite"
    init_db(db_path)

    stale = SavedNote(
        substack_note_id="999",
        url="https://substack.com/@stale/note/c-999",
        body_text="stale note",
        is_saved=1,
    )
    upsert_note(stale, db_path=db_path)

    client = MockNotesClient(pages=[[_note_item(1)]])
    run = sync_saved_notes(force=True, db_path=db_path, client=client)

    assert run.status == "success"
    assert run.reconciled_count == 1

    stale_after = get_note("https://substack.com/@stale/note/c-999", db_path=db_path)
    assert stale_after.is_saved == 0
    assert stale_after.unsaved_at is not None

    notes = list_notes(db_path=db_path)
    assert len(notes) == 1


def test_sync_notes_incremental_never_reconciles(tmp_path: Path):
    db_path = tmp_path / "notes_no_reconcile_test.sqlite"
    init_db(db_path)

    other = SavedNote(
        substack_note_id="999",
        url="https://substack.com/@other/note/c-999",
        body_text="still saved elsewhere",
        is_saved=1,
    )
    upsert_note(other, db_path=db_path)

    client = MockNotesClient(pages=[[_note_item(1)]])
    run = sync_saved_notes(force=False, db_path=db_path, client=client)

    assert run.status == "success"
    assert run.reconciled_count == 0

    other_after = get_note("https://substack.com/@other/note/c-999", db_path=db_path)
    assert other_after.is_saved == 1


def test_sync_notes_incremental_early_stop(tmp_path: Path):
    """Three consecutive already-saved notes must stop the incremental loop
    before reaching further pages."""
    db_path = tmp_path / "notes_early_stop_test.sqlite"
    init_db(db_path)

    for i in range(1, 4):
        upsert_note(
            parse_remote_note(_note_item(i, handle=f"handle{i}")), db_path=db_path
        )

    page1 = [_note_item(i, handle=f"handle{i}") for i in range(1, 4)] + [
        _note_item(4, handle="newcomer")
    ]
    client = MockNotesClient(pages=[page1])
    run = sync_saved_notes(force=False, db_path=db_path, client=client)

    assert run.status == "success"
    # Stops after 3 consecutive matches, before reaching the 4th (new) note.
    assert get_note("https://substack.com/@newcomer/note/c-4", db_path=db_path) is None


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

    results = client._fetch_via_dom(
        offset=0, limit=50, playwright_instance=MockPlaywright()
    )
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
                {
                    "id": 1,
                    "publication_id": 10,
                    "title": "P1",
                    "canonical_url": "https://a.substack.com/p/one",
                    "post_date": "2026-06-10T00:00:00Z",
                    "saved_at": "2026-06-20T00:00:00Z",
                    "publishedBylines": [{"name": "Alice"}],
                },
                {
                    "id": 2,
                    "publication_id": 11,
                    "title": "P2",
                    "canonical_url": "https://b.substack.com/p/two",
                    "post_date": "2026-06-09T00:00:00Z",
                    "saved_at": "2026-06-18T00:00:00Z",
                    "publishedBylines": [{"name": "Bob"}],
                },
            ],
            "publications": [{"id": 10, "name": "Pub A"}, {"id": 11, "name": "Pub B"}],
            "more": True,
        },
        "2026-06-18T00:00:00Z": {
            "posts": [
                {
                    "id": 3,
                    "publication_id": 12,
                    "title": "P3",
                    "canonical_url": "https://c.substack.com/p/three",
                    "post_date": "2026-06-08T00:00:00Z",
                    "saved_at": "2026-06-15T00:00:00Z",
                    "publishedBylines": [{"name": "Carol"}],
                },
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
    # A clean "no more pages" completion is never mistaken for a truncation.
    assert client._api_truncated is False


class _RetryResponse:
    """Minimal reader-API response double with status/headers/json."""

    def __init__(self, status=200, payload=None, headers=None):
        self.status = status
        self.ok = 200 <= status < 300
        self.url = "https://substack.com/api/v1/reader/posts"
        self._payload = payload if payload is not None else {"posts": [], "more": False}
        self.headers = headers or {}

    def json(self):
        return self._payload


def _reader_client(tmp_path: Path) -> SubstackSavedPostsClient:
    state_file = tmp_path / "storage_state.json"
    state_file.write_text('{"cookies": [], "origins": []}')
    return SubstackSavedPostsClient(storage_state_path=state_file)


def test_reader_api_retries_on_429_then_succeeds(tmp_path: Path):
    """A 429 with a Retry-After header is retried (after honoring the header delay)
    rather than being silently treated as an empty/unavailable saved list."""
    ok_payload = {
        "posts": [
            {
                "id": 1,
                "publication_id": 10,
                "title": "P1",
                "canonical_url": "https://a.substack.com/p/one",
                "saved_at": "2026-06-20T00:00:00Z",
            }
        ],
        "publications": [{"id": 10, "name": "Pub A"}],
        "more": False,
    }
    responses = [
        _RetryResponse(status=429, headers={"retry-after": "2"}),
        _RetryResponse(status=200, payload=ok_payload),
    ]

    class MockApiContext:
        def __init__(self):
            self.calls = 0

        def get(self, url):
            res = responses[self.calls]
            self.calls += 1
            return res

    slept: list[float] = []
    client = _reader_client(tmp_path)
    posts = client._fetch_all_saved_via_reader_api(
        MockApiContext(), page_size=2, sleep_func=slept.append
    )

    assert [p["id"] for p in posts] == [1]
    assert slept == [2.0]  # honored the Retry-After header value


def test_reader_api_gives_up_after_max_retries(tmp_path: Path):
    """A 429 that never clears is treated as 'unavailable' (returns None so the
    caller can fall back), not as an empty success."""

    class MockApiContext:
        def __init__(self):
            self.calls = 0

        def get(self, url):
            self.calls += 1
            return _RetryResponse(status=429)  # no Retry-After -> backoff

        _seen = None

    ctx = MockApiContext()
    slept: list[float] = []
    client = _reader_client(tmp_path)
    result = client._fetch_all_saved_via_reader_api(
        ctx, page_size=2, max_retries=3, sleep_func=slept.append
    )

    assert result is None  # nothing fetched -> signal DOM fallback
    assert ctx.calls == 4  # 1 initial + 3 retries
    assert slept == [0.5, 1.0, 2.0]  # capped exponential backoff


def test_retry_after_seconds_caps_and_falls_back(tmp_path: Path):
    client = _reader_client(tmp_path)
    # Honors an integer Retry-After, clamped to the cap.
    assert (
        client._retry_after_seconds(_RetryResponse(headers={"retry-after": "5"}), 0)
        == 5.0
    )
    assert (
        client._retry_after_seconds(_RetryResponse(headers={"retry-after": "999"}), 0)
        == 30.0
    )
    # Unparseable (e.g. HTTP-date) -> exponential backoff by attempt number.
    assert (
        client._retry_after_seconds(
            _RetryResponse(headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}), 2
        )
        == 2.0
    )
    # No header at all -> backoff.
    assert client._retry_after_seconds(_RetryResponse(), 1) == 1.0


def test_unified_posts_cursor_pagination(tmp_path: Path):
    """Mirrors test_reader_api_cursor_pagination for the unified reader/saved
    endpoint: pagination is driven by re-submitting the server's own nextCursor
    value rather than an after=<ISO saved_at> cursor."""
    client = _reader_client(tmp_path)

    page1 = {
        "items": [
            {
                "post": {
                    "id": 1,
                    "canonical_url": "https://a.substack.com/p/one",
                    "title": "P1",
                    "post_date": "2026-06-10T00:00:00Z",
                    "saved_at": "2026-06-20T00:00:00Z",
                },
                "publication": {"name": "Pub A"},
            },
            {
                "post": {
                    "id": 2,
                    "canonical_url": "https://b.substack.com/p/two",
                    "title": "P2",
                    "post_date": "2026-06-09T00:00:00Z",
                    "saved_at": "2026-06-18T00:00:00Z",
                },
                "publication": {"name": "Pub B"},
            },
        ],
        "nextCursor": "opaque-token-1",
    }
    page2 = {
        "items": [
            {
                "post": {
                    "id": 3,
                    "canonical_url": "https://c.substack.com/p/three",
                    "title": "P3",
                    "post_date": "2026-06-08T00:00:00Z",
                    "saved_at": "2026-06-15T00:00:00Z",
                },
                "publication": {"name": "Pub C"},
            },
        ],
        "nextCursor": None,
    }

    class MockResponse:
        def __init__(self, payload):
            self._payload = payload
            self.status = 200
            self.ok = True
            self.url = "https://substack.com/api/v1/reader/saved"

        def json(self):
            return self._payload

    class MockApiContext:
        def get(self, url):
            if "cursor=" in url:
                return MockResponse(page2)
            return MockResponse(page1)

    posts = client._fetch_all_saved_posts_via_unified_api(MockApiContext())

    assert [item["post"]["id"] for item in posts] == [1, 2, 3]
    # A clean "no nextCursor" completion is never mistaken for a truncation.
    assert client._unified_api_truncated is False


def test_unified_posts_pagination_dedupes_by_canonical_url(tmp_path: Path):
    client = _reader_client(tmp_path)

    dup_item = {
        "post": {
            "id": 1,
            "canonical_url": "https://a.substack.com/p/one?utm_source=substack",
        },
        "publication": {"name": "Pub A"},
    }
    page1 = {"items": [dup_item], "nextCursor": "tok"}
    page2 = {
        "items": [
            {
                "post": {"id": 1, "canonical_url": "https://a.substack.com/p/one"},
                "publication": {"name": "Pub A"},
            }
        ],
        "nextCursor": None,
    }

    class MockResponse:
        def __init__(self, payload):
            self._payload = payload
            self.status = 200
            self.ok = True
            self.url = "https://substack.com/api/v1/reader/saved"

        def json(self):
            return self._payload

    class MockApiContext:
        def get(self, url):
            return MockResponse(page2) if "cursor=" in url else MockResponse(page1)

    posts = client._fetch_all_saved_posts_via_unified_api(MockApiContext())

    assert len(posts) == 1


def test_unified_posts_retries_on_429_then_succeeds(tmp_path: Path):
    ok_payload = {
        "items": [
            {
                "post": {"id": 1, "canonical_url": "https://a.substack.com/p/one"},
                "publication": {"name": "Pub A"},
            }
        ],
        "nextCursor": None,
    }
    responses = [
        _RetryResponse(status=429, headers={"retry-after": "2"}),
        _RetryResponse(status=200, payload=ok_payload),
    ]

    class MockApiContext:
        def __init__(self):
            self.calls = 0

        def get(self, url):
            res = responses[self.calls]
            self.calls += 1
            return res

    slept: list[float] = []
    client = _reader_client(tmp_path)
    posts = client._fetch_all_saved_posts_via_unified_api(
        MockApiContext(), sleep_func=slept.append
    )

    assert [item["post"]["id"] for item in posts] == [1]
    assert slept == [2.0]


def test_unified_posts_gives_up_after_max_retries(tmp_path: Path):
    class MockApiContext:
        def __init__(self):
            self.calls = 0

        def get(self, url):
            self.calls += 1
            return _RetryResponse(status=429)

    ctx = MockApiContext()
    slept: list[float] = []
    client = _reader_client(tmp_path)
    result = client._fetch_all_saved_posts_via_unified_api(
        ctx, max_retries=3, sleep_func=slept.append
    )

    assert result is None
    assert ctx.calls == 4
    assert slept == [0.5, 1.0, 2.0]


def test_parse_remote_post_unified_item_shape():
    """parse_remote_post already resolves {post, publication} items (the shape
    shared by the unified reader/saved endpoint's filter=posts and filter=all);
    this pins that behavior against a realistic unified payload item."""
    item = {
        "post": {
            "id": 42,
            "canonical_url": "https://pub.substack.com/p/some-post",
            "title": "Some Post",
            "post_date": "2026-06-01T00:00:00Z",
            "audience": "everyone",
        },
        "publication": {"name": "Pub Name", "subdomain": "pub"},
    }

    post = parse_remote_post(item)

    assert post.substack_post_id == "42"
    assert post.url == "https://pub.substack.com/p/some-post"
    assert post.title == "Some Post"
    assert post.publication_name == "Pub Name"
    assert post.published_at == "2026-06-01T00:00:00Z"


def test_parse_remote_post_unified_saved_at_from_post_object():
    """The unified endpoint nests saved_at under "post" rather than at the item's
    top level (unlike the legacy reader-posts API); parse_remote_post must check
    both rather than only the item-level key."""
    item = {
        "post": {
            "id": 42,
            "canonical_url": "https://pub.substack.com/p/some-post",
            "saved_at": "2026-06-20T00:00:00Z",
        },
        "publication": {},
    }

    post = parse_remote_post(item)

    assert post.saved_at == "2026-06-20T00:00:00Z"


def test_parse_remote_post_unified_saved_at_never_fabricated():
    item = {
        "post": {"id": 42, "canonical_url": "https://pub.substack.com/p/some-post"},
        "publication": {},
    }

    post = parse_remote_post(item)

    assert post.saved_at is None


def test_reader_api_marks_truncated_on_midstream_429_exhaustion(tmp_path: Path):
    """A 429 that survives every retry on page 1 is 'unavailable' (None,
    nothing collected yet). But if it happens on a LATER page, after some
    posts were already collected, the fetcher must return that partial list
    AND flag it as truncated — not report it as if it were the complete
    remote saved set."""
    page1 = {
        "posts": [
            {
                "id": 1,
                "canonical_url": "https://a.substack.com/p/one",
                "saved_at": "2026-06-20T00:00:00Z",
            }
        ],
        "publications": [],
        "more": True,
    }

    class MockApiContext:
        def __init__(self):
            self.calls = 0

        def get(self, url):
            self.calls += 1
            if self.calls == 1:
                return _RetryResponse(status=200, payload=page1)
            return _RetryResponse(status=429)  # every page-2 attempt 429s

    client = _reader_client(tmp_path)
    slept: list[float] = []
    posts = client._fetch_all_saved_via_reader_api(
        MockApiContext(), page_size=1, max_retries=3, sleep_func=slept.append
    )

    assert [p["id"] for p in posts] == [1]  # partial data kept, not discarded
    assert client._api_truncated is True
    assert client.is_posts_fetch_truncated() is False  # cache not populated yet

    client._api_cache = posts
    assert client.is_posts_fetch_truncated() is True


def test_unified_posts_marks_truncated_on_midstream_429_exhaustion(tmp_path: Path):
    page1 = {
        "items": [
            {
                "post": {"id": 1, "canonical_url": "https://a.substack.com/p/one"},
                "publication": {"name": "Pub A"},
            }
        ],
        "nextCursor": "opaque-token-1",
    }

    class MockApiContext:
        def __init__(self):
            self.calls = 0

        def get(self, url):
            self.calls += 1
            if self.calls == 1:
                return _RetryResponse(status=200, payload=page1)
            return _RetryResponse(status=429)

    client = _reader_client(tmp_path)
    slept: list[float] = []
    posts = client._fetch_all_saved_posts_via_unified_api(
        MockApiContext(), max_retries=3, sleep_func=slept.append
    )

    assert [item["post"]["id"] for item in posts] == [1]
    assert client._unified_api_truncated is True

    client._unified_api_cache = posts
    assert client.is_posts_fetch_truncated() is True


def test_notes_api_marks_truncated_on_midstream_429_exhaustion(tmp_path: Path):
    page1 = {
        "items": [{"comment": {"id": 1, "body": "first"}}],
        "nextCursor": "opaque-token-1",
    }

    class MockApiContext:
        def __init__(self):
            self.calls = 0

        def get(self, url):
            self.calls += 1
            if self.calls == 1:
                return _RetryResponse(status=200, payload=page1)
            return _RetryResponse(status=429)

    client = _reader_client(tmp_path)
    slept: list[float] = []
    notes = client._fetch_all_saved_notes_via_api(
        MockApiContext(), max_retries=3, sleep_func=slept.append
    )

    assert [item["comment"]["id"] for item in notes] == [1]
    assert client._notes_api_truncated is True
    assert client.is_notes_fetch_truncated() is False  # cache not populated yet

    client._notes_api_cache = notes
    assert client.is_notes_fetch_truncated() is True


def test_is_posts_fetch_truncated_false_by_default(tmp_path: Path):
    client = _reader_client(tmp_path)
    assert client.is_posts_fetch_truncated() is False


def test_is_posts_fetch_truncated_safe_on_bare_subclass():
    """MockSubstackClient-style test doubles override __init__ without calling
    super().__init__(), so these instance attributes never get set. The
    getattr() defaults must make that report 'not truncated' rather than
    raising AttributeError."""

    class BareClient(SubstackSavedPostsClient):
        def __init__(self):
            pass

    assert BareClient().is_posts_fetch_truncated() is False
    assert BareClient().is_notes_fetch_truncated() is False


class TruncatedMockSubstackClient(MockSubstackClient):
    """Simulates a posts fetch whose underlying source hit a persistent
    mid-pagination failure, without needing a real 429-retry mock context."""

    def is_posts_fetch_truncated(self) -> bool:
        return True


def test_force_sync_skips_reconcile_when_fetch_truncated(tmp_path: Path):
    """The actual danger the truncation flag exists to prevent: a --force sync
    must not soft-delete a post just because a truncated fetch didn't include
    it — that post may still be saved remotely; the fetch just couldn't reach
    it this run."""
    db_path = tmp_path / "truncated_reconcile_test.sqlite"
    init_db(db_path)

    still_saved_remotely = SavedPost(
        url="https://pub1.substack.com/p/still-saved",
        title="Still Saved",
        publication_name="Pub 1",
        is_saved=1,
    )
    upsert_post(still_saved_remotely, db_path=db_path)

    mock_payloads = [
        [
            {
                "created_at": "2026-06-01T10:00:00Z",
                "post": {
                    "id": 101,
                    "title": "Post 1",
                    "canonical_url": "https://pub1.substack.com/p/post-1",
                    "publication": {"name": "Pub 1"},
                },
            },
        ]
    ]
    client = TruncatedMockSubstackClient(pages=mock_payloads)
    run = sync_saved_posts(force=True, db_path=db_path, client=client)

    assert run.status == "partial"
    assert run.reconciled_count == 0

    # The pre-existing post is NOT soft-deleted, even though it's absent from
    # this run's (truncated) fetch.
    still_there = get_post("https://pub1.substack.com/p/still-saved", db_path=db_path)
    assert still_there.is_saved == 1
    assert still_there.unsaved_at is None


class TruncatedMockNotesClient(MockNotesClient):
    def is_notes_fetch_truncated(self) -> bool:
        return True


def test_sync_notes_force_skips_reconcile_when_fetch_truncated(tmp_path: Path):
    db_path = tmp_path / "truncated_notes_reconcile_test.sqlite"
    init_db(db_path)

    still_saved_remotely = SavedNote(
        substack_note_id="999",
        url="https://substack.com/@stillsaved/note/c-999",
        body_text="still saved",
        is_saved=1,
    )
    upsert_note(still_saved_remotely, db_path=db_path)

    client = TruncatedMockNotesClient(pages=[[_note_item(1)]])
    run = sync_saved_notes(force=True, db_path=db_path, client=client)

    assert run.status == "partial"
    assert run.reconciled_count == 0

    still_there = get_note(
        "https://substack.com/@stillsaved/note/c-999", db_path=db_path
    )
    assert still_there.is_saved == 1
    assert still_there.unsaved_at is None
