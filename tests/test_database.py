"""Unit and integration tests for SQLite database repository and FTS5 search."""

from pathlib import Path

import pytest

from substack_saved_mcp.database import (
    get_note,
    get_note_by_substack_id,
    get_post,
    get_status,
    init_db,
    list_audiences,
    list_note_authors,
    list_notes,
    list_posts,
    list_publications,
    reconcile_unsaved_notes,
    reconcile_unsaved_posts,
    search_notes,
    search_posts,
    soft_delete_note,
    soft_delete_post,
    start_sync_run,
    upsert_note,
    upsert_post,
)
from substack_saved_mcp.models import SavedNote, SavedPost


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test_saved_posts.sqlite"
    init_db(db_path)
    return db_path


def test_init_db_and_status(temp_db: Path):
    status = get_status(temp_db)
    assert status.total_saved_posts == 0
    assert status.total_unsaved_posts == 0
    assert status.total_publications == 0


def test_upsert_and_get_post(temp_db: Path):
    post = SavedPost(
        url="https://ai.substack.com/p/future-of-llms",
        title="The Future of LLMs",
        publication_name="AI Research",
        author_name="Alice Smith",
        published_at="2026-01-15T10:00:00Z",
        saved_at="2026-02-01T12:00:00Z",
        excerpt="An in-depth analysis of large language model architecture.",
        is_saved=1,
    )

    saved = upsert_post(post, temp_db)
    assert saved.id is not None
    assert saved.title == "The Future of LLMs"
    assert saved.url == "https://ai.substack.com/p/future-of-llms"

    fetched = get_post(saved.id, temp_db)
    assert fetched is not None
    assert fetched.title == "The Future of LLMs"
    assert fetched.saved_at == "2026-02-01T12:00:00Z"


def test_upsert_post_by_id_updates_existing_on_url_change(temp_db: Path):
    """Same substack_post_id, changed URL (e.g. slug rename or custom-domain
    migration) must update the existing row rather than raising. The old
    INSERT ... ON CONFLICT(url) design found this post by id, then tried to
    INSERT on the new URL and hit the substack_post_id UNIQUE constraint."""
    post = SavedPost(
        substack_post_id="999",
        url="https://old-slug.substack.com/p/original-title",
        title="Original Title",
        publication_name="Some Pub",
        is_saved=1,
    )
    first = upsert_post(post, temp_db)

    renamed = SavedPost(
        substack_post_id="999",
        url="https://old-slug.substack.com/p/renamed-title",
        title="Renamed Title",
        publication_name="Some Pub",
        is_saved=1,
    )
    second = upsert_post(renamed, temp_db)

    assert second.id == first.id
    assert second.url == "https://old-slug.substack.com/p/renamed-title"
    assert second.title == "Renamed Title"
    assert len(list_posts(db_path=temp_db)) == 1


def test_fts5_search(temp_db: Path):
    p1 = SavedPost(
        url="https://tech.substack.com/p/quantum-computing",
        title="Quantum Computing Breaksthrough",
        publication_name="Tech Weekly",
        author_name="Bob Jones",
        published_at="2026-03-01T00:00:00Z",
        saved_at="2026-03-02T00:00:00Z",
        excerpt="Exploring qubit entanglement and superconducting circuits.",
        is_saved=1,
    )
    p2 = SavedPost(
        url="https://cooking.substack.com/p/perfect-pasta",
        title="Perfect Handmade Pasta",
        publication_name="Culinary Arts",
        author_name="Chef Maria",
        published_at="2026-03-05T00:00:00Z",
        saved_at="2026-03-06T00:00:00Z",
        excerpt="Secrets to making traditional egg pasta at home.",
        is_saved=1,
    )

    upsert_post(p1, temp_db)
    upsert_post(p2, temp_db)

    # Search for quantum
    results = search_posts("quantum", db_path=temp_db)
    assert len(results) == 1
    assert results[0].title == "Quantum Computing Breaksthrough"

    # Search for pasta
    results_pasta = search_posts("pasta", db_path=temp_db)
    assert len(results_pasta) == 1
    assert results_pasta[0].publication_name == "Culinary Arts"


def test_search_posts_fallback_keeps_filters(temp_db: Path):
    """A malformed FTS query must fall back to LIKE without silently dropping
    the audience filter -- previously the LIKE fallback only ever honored
    `audience` and unconditionally hardcoded is_saved = 1, dropping
    publication/date-range filters and any is_saved_only=False caller.

    Uses an unterminated-quote query (reliably raises sqlite3.OperationalError
    from FTS5) whose literal text also appears in the excerpt, so the LIKE
    fallback can actually match it and the test can tell "audience filter
    kept" apart from "no rows at all"."""
    upsert_post(
        SavedPost(
            url="https://cli.substack.com/p/free-quote",
            title="Free Quote Post",
            publication_name="CLI Times",
            excerpt='she said "hello world today',
            audience="everyone",
            is_saved=1,
        ),
        temp_db,
    )
    upsert_post(
        SavedPost(
            url="https://cli.substack.com/p/paid-quote",
            title="Paid Quote Post",
            publication_name="CLI Times",
            excerpt='she said "hello world today',
            audience="only_paid",
            is_saved=1,
        ),
        temp_db,
    )

    results = search_posts('"hello', audience="only_paid", db_path=temp_db)
    assert len(results) == 1
    assert results[0].title == "Paid Quote Post"


def test_soft_delete_post(temp_db: Path):
    post = SavedPost(
        url="https://news.substack.com/p/daily-briefing",
        title="Daily News Briefing",
        publication_name="Daily News",
        published_at="2026-04-01T00:00:00Z",
        saved_at="2026-04-01T01:00:00Z",
        is_saved=1,
    )
    saved = upsert_post(post, temp_db)
    assert saved.is_saved == 1

    # Soft delete / unsave
    unsaved = soft_delete_post(saved.url, temp_db)
    assert unsaved is not None
    assert unsaved.is_saved == 0
    assert unsaved.unsaved_at is not None

    # Listing active saved posts should now exclude unsaved post
    active_posts = list_posts(db_path=temp_db)
    assert len(active_posts) == 0

    # Status should reflect 1 unsaved post
    status = get_status(temp_db)
    assert status.total_saved_posts == 0
    assert status.total_unsaved_posts == 1


def test_reconcile_unsaved_posts(temp_db: Path):
    kept = upsert_post(
        SavedPost(
            url="https://pub1.com/p/kept", title="Kept", publication_name="Pub One"
        ),
        temp_db,
    )
    removed = upsert_post(
        SavedPost(
            url="https://pub1.com/p/removed",
            title="Removed",
            publication_name="Pub One",
        ),
        temp_db,
    )

    count = reconcile_unsaved_posts(["https://pub1.com/p/kept"], db_path=temp_db)
    assert count == 1

    still_saved = get_post(kept.id, temp_db)
    assert still_saved.is_saved == 1

    unsaved = get_post(removed.id, temp_db)
    assert unsaved.is_saved == 0
    assert unsaved.unsaved_at is not None


def test_reconcile_unsaved_posts_skips_empty_remote_list(temp_db: Path):
    """An empty remote list is more likely a fetch glitch than mass-unsaving,
    so reconciliation must be a no-op rather than wiping every saved post."""
    post = upsert_post(
        SavedPost(
            url="https://pub1.com/p/1", title="Post 1", publication_name="Pub One"
        ),
        temp_db,
    )

    count = reconcile_unsaved_posts([], db_path=temp_db)
    assert count == 0

    unchanged = get_post(post.id, temp_db)
    assert unchanged.is_saved == 1


def test_audience_stored_and_filterable(temp_db: Path):
    upsert_post(
        SavedPost(
            url="https://pub1.com/p/free",
            title="Free Post",
            publication_name="Pub One",
            excerpt="content",
            audience="everyone",
            is_saved=1,
        ),
        temp_db,
    )
    upsert_post(
        SavedPost(
            url="https://pub1.com/p/paid",
            title="Paid Post",
            publication_name="Pub One",
            excerpt="content",
            audience="only_paid",
            is_saved=1,
        ),
        temp_db,
    )

    fetched = get_post("https://pub1.com/p/paid", temp_db)
    assert fetched.audience == "only_paid"

    only_paid = list_posts(audience="only_paid", db_path=temp_db)
    assert len(only_paid) == 1
    assert only_paid[0].title == "Paid Post"
    assert only_paid[0].audience == "only_paid"

    search_results = search_posts("content", audience="everyone", db_path=temp_db)
    assert len(search_results) == 1
    assert search_results[0].title == "Free Post"


def test_list_audiences(temp_db: Path):
    upsert_post(
        SavedPost(
            url="https://pub1.com/p/1",
            title="Post 1",
            publication_name="Pub One",
            audience="everyone",
        ),
        temp_db,
    )
    upsert_post(
        SavedPost(
            url="https://pub1.com/p/2",
            title="Post 2",
            publication_name="Pub One",
            audience="everyone",
        ),
        temp_db,
    )
    upsert_post(
        SavedPost(
            url="https://pub1.com/p/3",
            title="Post 3",
            publication_name="Pub One",
            audience="only_paid",
        ),
        temp_db,
    )

    tiers = list_audiences(temp_db)
    assert len(tiers) == 2
    by_tier = {t.audience: t.post_count for t in tiers}
    assert by_tier["everyone"] == 2
    assert by_tier["only_paid"] == 1


def test_init_db_migrates_pre_audience_schema(tmp_path: Path):
    """A DB created before the audience column existed must not break init_db,
    since CREATE INDEX IF NOT EXISTS on a missing column raises OperationalError.

    This legacy schema also still has the (since-removed) metadata_json column, so
    the get_post below doubles as a regression check that a leftover column absent
    from the SavedPost model is tolerated on read (Pydantic ignores unknown keys)."""
    import sqlite3

    old_db_path = tmp_path / "old_schema.sqlite"
    conn = sqlite3.connect(str(old_db_path))
    conn.execute("""
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            substack_post_id TEXT UNIQUE,
            url TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            publication_name TEXT NOT NULL,
            publication_url TEXT,
            author_name TEXT,
            published_at TEXT,
            saved_at TEXT,
            unsaved_at TEXT,
            is_saved INTEGER NOT NULL DEFAULT 1,
            excerpt TEXT,
            content_text TEXT,
            image_url TEXT,
            is_paywalled INTEGER DEFAULT 0,
            reading_time_minutes INTEGER,
            word_count INTEGER,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO posts (url, title, publication_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            "https://old.substack.com/p/pre-existing",
            "Pre-existing Post",
            "Old Pub",
            "2026-01-01",
            "2026-01-01",
        ),
    )
    conn.commit()
    conn.close()

    init_db(old_db_path)  # must not raise

    post = get_post("https://old.substack.com/p/pre-existing", old_db_path)
    assert post is not None
    assert post.audience is None


def test_image_url_surfaced_in_list_and_search(temp_db: Path):
    upsert_post(
        SavedPost(
            url="https://pub1.com/p/with-image",
            title="Post With Image",
            publication_name="Pub One",
            excerpt="some searchable text",
            image_url="https://substackcdn.com/image/fetch/example.jpeg",
            is_saved=1,
        ),
        temp_db,
    )

    listed = list_posts(db_path=temp_db)
    assert listed[0].image_url == "https://substackcdn.com/image/fetch/example.jpeg"

    searched = search_posts("searchable", db_path=temp_db)
    assert searched[0].image_url == "https://substackcdn.com/image/fetch/example.jpeg"


def test_list_publications(temp_db: Path):
    upsert_post(
        SavedPost(
            url="https://pub1.com/p/1", title="Post 1", publication_name="Pub One"
        ),
        temp_db,
    )
    upsert_post(
        SavedPost(
            url="https://pub1.com/p/2", title="Post 2", publication_name="Pub One"
        ),
        temp_db,
    )
    upsert_post(
        SavedPost(
            url="https://pub2.com/p/1", title="Post 3", publication_name="Pub Two"
        ),
        temp_db,
    )

    pubs = list_publications(temp_db)
    assert len(pubs) == 2
    assert pubs[0].publication_name == "Pub One"
    assert pubs[0].post_count == 2
    assert pubs[1].publication_name == "Pub Two"
    assert pubs[1].post_count == 1


def test_upsert_and_get_note(temp_db: Path):
    note = SavedNote(
        substack_note_id="300984381",
        url="https://substack.com/@nathanbaugh/note/c-300984381",
        body_text="Steinbeck's 6 ideas for better writing",
        author_name="Nathan Baugh",
        author_handle="nathanbaugh",
        posted_at="2026-07-24T16:44:59.938Z",
    )
    saved = upsert_note(note, temp_db)
    assert saved.id is not None
    assert saved.substack_note_id == "300984381"

    fetched = get_note(saved.url, temp_db)
    assert fetched is not None
    assert fetched.author_handle == "nathanbaugh"

    by_id = get_note_by_substack_id("300984381", temp_db)
    assert by_id is not None
    assert by_id.id == saved.id


def test_upsert_note_by_id_updates_existing_on_url_change(temp_db: Path):
    """Same substack_note_id, changed URL (e.g. handle rename) must update the
    existing row rather than creating a second one or raising -- the case that
    breaks upsert_post's ON CONFLICT(url) design."""
    note = SavedNote(
        substack_note_id="123",
        url="https://substack.com/@oldhandle/note/c-123",
        body_text="original body",
    )
    first = upsert_note(note, temp_db)

    renamed = SavedNote(
        substack_note_id="123",
        url="https://substack.com/@newhandle/note/c-123",
        body_text="updated body",
    )
    second = upsert_note(renamed, temp_db)

    assert second.id == first.id
    assert second.url == "https://substack.com/@newhandle/note/c-123"
    assert len(list_notes(db_path=temp_db)) == 1


def test_notes_fts_search(temp_db: Path):
    upsert_note(
        SavedNote(
            substack_note_id="1",
            url="https://substack.com/@a/note/c-1",
            body_text="a note about prosemirror parsing",
            author_name="Alice",
            author_handle="alice",
        ),
        temp_db,
    )
    upsert_note(
        SavedNote(
            substack_note_id="2",
            url="https://substack.com/@b/note/c-2",
            body_text="unrelated content",
            author_name="Bob",
            author_handle="bob",
            is_restack=1,
            restacked_post_title="A great post about kubernetes",
        ),
        temp_db,
    )

    by_body = search_notes("prosemirror", db_path=temp_db)
    assert len(by_body) == 1
    assert by_body[0].author_handle == "alice"

    by_restack_title = search_notes("kubernetes", db_path=temp_db)
    assert len(by_restack_title) == 1
    assert by_restack_title[0].author_handle == "bob"


def test_search_notes_fallback_keeps_filters(temp_db: Path):
    """A malformed FTS query must fall back to LIKE without silently dropping
    the author filter -- the bug present in search_posts's LIKE fallback.

    Uses an unterminated-quote query (reliably raises sqlite3.OperationalError
    from FTS5) whose literal text also appears in the note body, so the LIKE
    fallback can actually match it and the test can tell "author filter kept"
    apart from "no rows at all"."""
    upsert_note(
        SavedNote(
            substack_note_id="1",
            url="https://substack.com/@alice/note/c-1",
            body_text='she said "hello world today',
            author_handle="alice",
        ),
        temp_db,
    )
    upsert_note(
        SavedNote(
            substack_note_id="2",
            url="https://substack.com/@bob/note/c-2",
            body_text='she said "hello world today',
            author_handle="bob",
        ),
        temp_db,
    )

    results = search_notes('"hello', author="alice", db_path=temp_db)
    assert len(results) == 1
    assert results[0].author_handle == "alice"


def test_soft_delete_note(temp_db: Path):
    note = upsert_note(
        SavedNote(
            substack_note_id="1",
            url="https://substack.com/@a/note/c-1",
            body_text="text",
        ),
        temp_db,
    )
    deleted = soft_delete_note(note.id, temp_db)
    assert deleted is not None
    assert deleted.is_saved == 0
    assert deleted.unsaved_at is not None
    assert list_notes(db_path=temp_db) == []


def test_reconcile_unsaved_notes(temp_db: Path):
    for i in range(3):
        upsert_note(
            SavedNote(
                substack_note_id=str(i),
                url=f"https://substack.com/@a/note/c-{i}",
                body_text="text",
            ),
            temp_db,
        )

    assert reconcile_unsaved_notes([], temp_db) == 0
    assert len(list_notes(db_path=temp_db)) == 3

    reconciled = reconcile_unsaved_notes(["0", "1"], temp_db)
    assert reconciled == 1
    remaining = {n.substack_note_id for n in list_notes(db_path=temp_db)}
    assert remaining == {"0", "1"}


def test_get_status_scopes_sync_runs_by_entity(temp_db: Path):
    from substack_saved_mcp.database import finish_sync_run

    post_run_id = start_sync_run(
        sync_mode="incremental", entity="post", db_path=temp_db
    )
    finish_sync_run(post_run_id, "success", 1, 1, db_path=temp_db)

    note_run_id = start_sync_run(
        sync_mode="incremental", entity="note", db_path=temp_db
    )
    finish_sync_run(note_run_id, "failed", 0, 0, error_message="boom", db_path=temp_db)

    status = get_status(temp_db)
    assert status.last_sync_status == "success"
    assert status.last_note_sync_status == "failed"


def test_list_note_authors(temp_db: Path):
    upsert_note(
        SavedNote(
            substack_note_id="1",
            url="https://substack.com/@a/note/c-1",
            body_text="x",
            author_handle="alice",
            author_name="Alice",
        ),
        temp_db,
    )
    upsert_note(
        SavedNote(
            substack_note_id="2",
            url="https://substack.com/@a/note/c-2",
            body_text="y",
            author_handle="alice",
            author_name="Alice",
        ),
        temp_db,
    )

    authors = list_note_authors(temp_db)
    assert len(authors) == 1
    assert authors[0].author_handle == "alice"
    assert authors[0].note_count == 2


def test_init_db_idempotent_on_existing_notes_schema(temp_db: Path):
    """Calling init_db twice must not error and must not duplicate schema objects."""
    init_db(temp_db)
    upsert_note(
        SavedNote(
            substack_note_id="1", url="https://substack.com/@a/note/c-1", body_text="x"
        ),
        temp_db,
    )
    assert len(list_notes(db_path=temp_db)) == 1
