"""SQLite database schema, FTS5 virtual table indexing, and query repository."""

import math
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from substack_saved_mcp.config import get_db_path, get_fully_read_threshold
from substack_saved_mcp.models import (
    AudienceSummary,
    NoteAuthorSummary,
    NoteSummary,
    PostSummary,
    PublicationSummary,
    SavedNote,
    SavedPost,
    SavedPostsStatus,
)
from substack_saved_mcp.url_utils import canonicalize_url

WORDS_PER_MINUTE = 200  # kept in sync with sync.py's constant of the same name

_NOTE_SORT_COLUMNS = {"saved_at": "saved_at", "posted_at": "posted_at"}

_POST_SORT_COLUMNS = {
    "saved_at": "saved_at",
    "published_at": "published_at",
    "read_progress": "read_progress",
    "minutes_remaining": (
        "COALESCE(word_count, 0) * (1 - COALESCE(max_read_progress, 0))"
    ),
}

_READ_STATE_PREDICATES = {
    "unread": "COALESCE({p}is_viewed, 0) = 0 AND COALESCE({p}max_read_progress, 0) = 0",
    "in_progress": (
        "COALESCE({p}max_read_progress, 0) > 0 "
        "AND COALESCE({p}max_read_progress, 0) < ?"
    ),
    "finished": "COALESCE({p}max_read_progress, 0) >= ?",
    "started": (
        "COALESCE({p}max_read_progress, 0) > 0 OR COALESCE({p}is_viewed, 0) = 1"
    ),
}


def _read_state_clause(
    read_state: str, column_prefix: str = ""
) -> tuple[str, list[float]]:
    """Build a (clause, params) pair classifying posts into one of four read
    states, resolved against the raw read_progress columns rather than a
    stored derivative. COALESCE is load-bearing: a NULL max_read_progress
    (pre-migration rows, or DOM-sourced posts that never carry progress) must
    read as unread, not silently vanish from every filter."""
    template = _READ_STATE_PREDICATES.get(read_state)
    if template is None:
        allowed = ", ".join(sorted(_READ_STATE_PREDICATES))
        raise ValueError(
            f"Unknown read_state {read_state!r}; expected one of: {allowed}"
        )
    clause = template.format(p=column_prefix)
    threshold = get_fully_read_threshold()
    params = [threshold] if "?" in clause else []
    return clause, params


@contextmanager
def get_db_connection(
    db_path: Path | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for SQLite database connection with WAL mode enabled."""
    target_path = db_path or get_db_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(target_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    """Initialize SQLite database tables, indexes, FTS5 virtual table, and triggers."""
    from substack_saved_mcp.config import ensure_app_dirs

    ensure_app_dirs()
    with get_db_connection(db_path) as conn:
        # Add columns to a pre-existing posts table before the index below is
        # created, since CREATE INDEX IF NOT EXISTS still fails on a missing column.
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='posts'"
        )
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(posts)")
            existing_cols = {row["name"] for row in cursor.fetchall()}
            if "audience" not in existing_cols:
                conn.execute("ALTER TABLE posts ADD COLUMN audience TEXT")
            if "read_progress" not in existing_cols:
                conn.execute("ALTER TABLE posts ADD COLUMN read_progress REAL")
            if "max_read_progress" not in existing_cols:
                conn.execute("ALTER TABLE posts ADD COLUMN max_read_progress REAL")
            if "is_viewed" not in existing_cols:
                conn.execute(
                    "ALTER TABLE posts ADD COLUMN is_viewed INTEGER NOT NULL DEFAULT 0"
                )

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
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
                audience TEXT,
                is_paywalled INTEGER DEFAULT 0,
                reading_time_minutes INTEGER,
                word_count INTEGER,
                read_progress REAL,
                max_read_progress REAL,
                is_viewed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_posts_url ON posts(url);
            CREATE INDEX IF NOT EXISTS idx_posts_published_at ON posts(published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_posts_saved_at ON posts(saved_at DESC);
            CREATE INDEX IF NOT EXISTS idx_posts_is_saved ON posts(is_saved);
            CREATE INDEX IF NOT EXISTS idx_posts_publication ON posts(publication_name);
            CREATE INDEX IF NOT EXISTS idx_posts_audience ON posts(audience);
            CREATE INDEX IF NOT EXISTS idx_posts_max_read_progress ON posts(max_read_progress);

            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                sync_mode TEXT NOT NULL DEFAULT 'incremental',
                entity TEXT NOT NULL DEFAULT 'post',
                fetched_count INTEGER DEFAULT 0,
                upserted_count INTEGER DEFAULT 0,
                reconciled_count INTEGER DEFAULT 0,
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                substack_note_id TEXT NOT NULL UNIQUE,
                url TEXT UNIQUE,
                body_text TEXT NOT NULL DEFAULT '',
                body_raw TEXT,
                body_format TEXT,
                author_name TEXT,
                author_handle TEXT,
                author_id TEXT,
                publication_name TEXT,
                publication_url TEXT,
                posted_at TEXT,
                saved_at TEXT,
                unsaved_at TEXT,
                is_saved INTEGER NOT NULL DEFAULT 1,
                is_restack INTEGER NOT NULL DEFAULT 0,
                parent_note_id TEXT,
                attachment_type TEXT,
                attachment_url TEXT,
                restacked_post_url TEXT,
                restacked_post_title TEXT,
                restacked_publication_name TEXT,
                like_count INTEGER,
                restack_count INTEGER,
                reply_count INTEGER,
                word_count INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_notes_url ON notes(url);
            CREATE INDEX IF NOT EXISTS idx_notes_saved_at ON notes(saved_at DESC);
            CREATE INDEX IF NOT EXISTS idx_notes_posted_at ON notes(posted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_notes_is_saved ON notes(is_saved);
            CREATE INDEX IF NOT EXISTS idx_notes_author ON notes(author_handle);

            -- FTS5 Full-Text Search Virtual Table for notes
            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                body_text,
                author_name,
                author_handle,
                publication_name,
                restacked_post_title,
                content='notes',
                content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
                INSERT INTO notes_fts(rowid, body_text, author_name, author_handle, publication_name, restacked_post_title)
                VALUES (new.id, new.body_text, new.author_name, new.author_handle, new.publication_name, new.restacked_post_title);
            END;

            CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, body_text, author_name, author_handle, publication_name, restacked_post_title)
                VALUES('delete', old.id, old.body_text, old.author_name, old.author_handle, old.publication_name, old.restacked_post_title);
            END;

            CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, body_text, author_name, author_handle, publication_name, restacked_post_title)
                VALUES('delete', old.id, old.body_text, old.author_name, old.author_handle, old.publication_name, old.restacked_post_title);
                INSERT INTO notes_fts(rowid, body_text, author_name, author_handle, publication_name, restacked_post_title)
                VALUES (new.id, new.body_text, new.author_name, new.author_handle, new.publication_name, new.restacked_post_title);
            END;

            -- FTS5 Full-Text Search Virtual Table
            CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
                title,
                publication_name,
                author_name,
                excerpt,
                content_text,
                content='posts',
                content_rowid='id'
            );

            -- Triggers to synchronize FTS5 index on INSERT, UPDATE, and DELETE
            CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
                INSERT INTO posts_fts(rowid, title, publication_name, author_name, excerpt, content_text)
                VALUES (new.id, new.title, new.publication_name, new.author_name, new.excerpt, new.content_text);
            END;

            CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
                INSERT INTO posts_fts(posts_fts, rowid, title, publication_name, author_name, excerpt, content_text)
                VALUES('delete', old.id, old.title, old.publication_name, old.author_name, old.excerpt, old.content_text);
            END;

            CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
                INSERT INTO posts_fts(posts_fts, rowid, title, publication_name, author_name, excerpt, content_text)
                VALUES('delete', old.id, old.title, old.publication_name, old.author_name, old.excerpt, old.content_text);
                INSERT INTO posts_fts(rowid, title, publication_name, author_name, excerpt, content_text)
                VALUES (new.id, new.title, new.publication_name, new.author_name, new.excerpt, new.content_text);
            END;
        """)
        # Additive migration for databases created before reconciliation tracking existed.
        try:
            conn.execute(
                "ALTER TABLE sync_runs ADD COLUMN reconciled_count INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already present

        # Additive migration for databases created before notes support existed.
        # Every historical run was a posts sync, so backfilling 'post' is correct.
        try:
            conn.execute(
                "ALTER TABLE sync_runs ADD COLUMN entity TEXT NOT NULL DEFAULT 'post'"
            )
        except sqlite3.OperationalError:
            pass  # column already present


def upsert_post(post: SavedPost, db_path: Path | None = None) -> SavedPost:
    """Insert or update a post in the database. Returns the updated SavedPost object.

    Looks up by substack_post_id or URL, then branches into an explicit UPDATE
    or INSERT rather than ``INSERT ... ON CONFLICT(url)``. The old ON-CONFLICT
    approach could raise: a post found by id whose canonical URL has since
    changed (slug rename, custom-domain migration) doesn't conflict on the new
    URL, so SQLite would attempt an INSERT and trip the substack_post_id UNIQUE
    constraint instead. This mirrors upsert_note's lookup-then-branch pattern,
    which never had that trap.
    """
    now_iso = datetime.now(UTC).isoformat()
    clean_url = canonicalize_url(post.url)

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        existing = None
        if post.substack_post_id:
            cursor.execute(
                "SELECT * FROM posts WHERE substack_post_id = ?",
                (post.substack_post_id,),
            )
            existing = cursor.fetchone()
        if not existing and clean_url:
            cursor.execute("SELECT * FROM posts WHERE url = ?", (clean_url,))
            existing = cursor.fetchone()

        created_at = existing["created_at"] if existing else now_iso
        # Preserve a known save time; never fabricate one from the DB-insert moment.
        # When the source does not expose the original Substack bookmark time, leave
        # saved_at NULL rather than stamping now().
        saved_at = post.saved_at or (existing["saved_at"] if existing else None)
        substack_post_id = post.substack_post_id or (
            existing["substack_post_id"] if existing else None
        )
        publication_url = post.publication_url or (
            existing["publication_url"] if existing else None
        )
        author_name = post.author_name or (
            existing["author_name"] if existing else None
        )
        published_at = post.published_at or (
            existing["published_at"] if existing else None
        )
        excerpt = post.excerpt or (existing["excerpt"] if existing else None)
        content_text = post.content_text or (
            existing["content_text"] if existing else None
        )
        image_url = post.image_url or (existing["image_url"] if existing else None)
        audience = post.audience or (existing["audience"] if existing else None)
        reading_time_minutes = post.reading_time_minutes or (
            existing["reading_time_minutes"] if existing else None
        )
        word_count = post.word_count or (existing["word_count"] if existing else None)
        # Explicit is-not-None checks (not `or`): a real 0.0 is falsy and would
        # otherwise be silently discarded in favor of a stale stored value.
        read_progress = (
            post.read_progress
            if post.read_progress is not None
            else (existing["read_progress"] if existing else None)
        )
        max_read_progress = (
            post.max_read_progress
            if post.max_read_progress is not None
            else (existing["max_read_progress"] if existing else None)
        )
        is_viewed = (
            post.is_viewed
            if post.is_viewed
            else (existing["is_viewed"] if existing else 0)
        )

        if existing:
            cursor.execute(
                """
                UPDATE posts SET
                    substack_post_id = ?, url = ?, title = ?, publication_name = ?,
                    publication_url = ?, author_name = ?, published_at = ?, saved_at = ?,
                    unsaved_at = ?, is_saved = ?, excerpt = ?, content_text = ?,
                    image_url = ?, audience = ?, is_paywalled = ?, reading_time_minutes = ?,
                    word_count = ?, read_progress = ?, max_read_progress = ?, is_viewed = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    substack_post_id,
                    clean_url,
                    post.title,
                    post.publication_name,
                    publication_url,
                    author_name,
                    published_at,
                    saved_at,
                    post.unsaved_at,
                    post.is_saved,
                    excerpt,
                    content_text,
                    image_url,
                    audience,
                    post.is_paywalled,
                    reading_time_minutes,
                    word_count,
                    read_progress,
                    max_read_progress,
                    is_viewed,
                    now_iso,
                    existing["id"],
                ),
            )
            post_id = existing["id"]
        else:
            cursor.execute(
                """
                INSERT INTO posts (
                    substack_post_id, url, title, publication_name, publication_url,
                    author_name, published_at, saved_at, unsaved_at, is_saved,
                    excerpt, content_text, image_url, audience, is_paywalled, reading_time_minutes,
                    word_count, read_progress, max_read_progress, is_viewed,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    substack_post_id,
                    clean_url,
                    post.title,
                    post.publication_name,
                    publication_url,
                    author_name,
                    published_at,
                    saved_at,
                    post.unsaved_at,
                    post.is_saved,
                    excerpt,
                    content_text,
                    image_url,
                    audience,
                    post.is_paywalled,
                    reading_time_minutes,
                    word_count,
                    read_progress,
                    max_read_progress,
                    is_viewed,
                    created_at,
                    now_iso,
                ),
            )
            post_id = cursor.lastrowid

        cursor.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = cursor.fetchone()
        return SavedPost(**dict(row))


def soft_delete_post(
    url_or_id: str | int, db_path: Path | None = None
) -> SavedPost | None:
    """Mark a post as unsaved (is_saved = 0, unsaved_at = now)."""
    now_iso = datetime.now(UTC).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        if isinstance(url_or_id, int) or (
            isinstance(url_or_id, str) and url_or_id.isdigit()
        ):
            cursor.execute("SELECT * FROM posts WHERE id = ?", (int(url_or_id),))
        else:
            clean_url = canonicalize_url(str(url_or_id))
            cursor.execute(
                "SELECT * FROM posts WHERE url = ? OR substack_post_id = ?",
                (clean_url, str(url_or_id)),
            )

        row = cursor.fetchone()
        if not row:
            return None

        cursor.execute(
            """
            UPDATE posts
            SET is_saved = 0, unsaved_at = ?, updated_at = ?
            WHERE id = ?
        """,
            (now_iso, now_iso, row["id"]),
        )

        cursor.execute("SELECT * FROM posts WHERE id = ?", (row["id"],))
        return SavedPost(**dict(cursor.fetchone()))


def reconcile_unsaved_posts(remote_urls: list[str], db_path: Path | None = None) -> int:
    """Soft-delete locally-active posts absent from a complete remote saved-URL set.

    Intended for use only after a full sync has enumerated every currently-saved
    post on Substack (an incremental sync may stop early and would wrongly treat
    un-refetched posts as removed). Skips entirely when remote_urls is empty, since
    that's more likely a fetch problem than genuine mass-unsaving.
    """
    if not remote_urls:
        return 0
    clean_urls = {canonicalize_url(u) for u in remote_urls if u}

    now_iso = datetime.now(UTC).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, url FROM posts WHERE is_saved = 1")
        stale_ids = [
            row["id"] for row in cursor.fetchall() if row["url"] not in clean_urls
        ]
        if not stale_ids:
            return 0

        cursor.executemany(
            "UPDATE posts SET is_saved = 0, unsaved_at = ?, updated_at = ? WHERE id = ?",
            [(now_iso, now_iso, post_id) for post_id in stale_ids],
        )
        return len(stale_ids)


def get_post(url_or_id: str | int, db_path: Path | None = None) -> SavedPost | None:
    """Retrieve full post record by local ID, Substack post ID, or URL."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        if isinstance(url_or_id, int) or (
            isinstance(url_or_id, str) and url_or_id.isdigit()
        ):
            cursor.execute("SELECT * FROM posts WHERE id = ?", (int(url_or_id),))
        else:
            clean_url = canonicalize_url(str(url_or_id))
            cursor.execute(
                "SELECT * FROM posts WHERE url = ? OR substack_post_id = ?",
                (clean_url, str(url_or_id)),
            )
        row = cursor.fetchone()
        return SavedPost(**dict(row)) if row else None


def list_posts(
    limit: int = 20,
    offset: int = 0,
    publication: str | None = None,
    audience: str | None = None,
    read_state: str | None = None,
    sort_by: str = "saved_at",
    is_saved_only: bool = True,
    db_path: Path | None = None,
) -> list[PostSummary]:
    """List posts with pagination, publication/audience/read_state filters, and sorting."""
    order_col = _POST_SORT_COLUMNS.get(sort_by, "saved_at")
    where_clauses = []
    params: list[str | int | float] = []

    if is_saved_only:
        where_clauses.append("is_saved = 1")
    if publication:
        where_clauses.append("LOWER(publication_name) LIKE LOWER(?)")
        params.append(f"%{publication}%")
    if audience:
        where_clauses.append("LOWER(audience) = LOWER(?)")
        params.append(audience)
    if read_state:
        clause, read_state_params = _read_state_clause(read_state)
        where_clauses.append(clause)
        params.extend(read_state_params)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = f"""
        SELECT id, substack_post_id, url, title, publication_name, author_name,
               published_at, saved_at, is_saved, excerpt, image_url, audience, is_paywalled,
               reading_time_minutes, word_count, read_progress, max_read_progress, is_viewed
        FROM posts
        {where_sql}
        ORDER BY {order_col} DESC NULLS LAST
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [PostSummary(**dict(r)) for r in cursor.fetchall()]


def _post_search_filters(
    publication: str | None,
    audience: str | None,
    published_after: str | None,
    published_before: str | None,
    saved_after: str | None,
    saved_before: str | None,
    is_saved_only: bool,
    read_state: str | None = None,
    column_prefix: str = "",
) -> tuple[list[str], list[str | int | float]]:
    """Build shared WHERE clauses/params for search_posts, used by both the FTS
    branch and the LIKE fallback so a malformed FTS query never silently drops
    filters (the bug the notes search code was built to avoid from the start)."""
    where_clauses = []
    params: list[str | int | float] = []

    if is_saved_only:
        where_clauses.append(f"{column_prefix}is_saved = 1")
    if publication:
        where_clauses.append(f"LOWER({column_prefix}publication_name) LIKE LOWER(?)")
        params.append(f"%{publication}%")
    if audience:
        where_clauses.append(f"LOWER({column_prefix}audience) = LOWER(?)")
        params.append(audience)
    if published_after:
        where_clauses.append(f"{column_prefix}published_at >= ?")
        params.append(published_after)
    if published_before:
        where_clauses.append(f"{column_prefix}published_at <= ?")
        params.append(published_before)
    if saved_after:
        where_clauses.append(f"{column_prefix}saved_at >= ?")
        params.append(saved_after)
    if saved_before:
        where_clauses.append(f"{column_prefix}saved_at <= ?")
        params.append(saved_before)
    if read_state:
        clause, read_state_params = _read_state_clause(read_state, column_prefix)
        where_clauses.append(clause)
        params.extend(read_state_params)

    return where_clauses, params


def search_posts(
    query: str,
    publication: str | None = None,
    audience: str | None = None,
    published_after: str | None = None,
    published_before: str | None = None,
    saved_after: str | None = None,
    saved_before: str | None = None,
    read_state: str | None = None,
    limit: int = 20,
    is_saved_only: bool = True,
    db_path: Path | None = None,
) -> list[PostSummary]:
    """Full-text search over posts using FTS5 BM25 relevance ranking and metadata filters."""
    filter_clauses, filter_params = _post_search_filters(
        publication,
        audience,
        published_after,
        published_before,
        saved_after,
        saved_before,
        is_saved_only,
        read_state,
        column_prefix="p.",
    )
    where_clauses = ["posts_fts MATCH ?", *filter_clauses]
    params: list[str | int | float] = [query, *filter_params]

    sql = f"""
        SELECT p.id, p.substack_post_id, p.url, p.title, p.publication_name, p.author_name,
               p.published_at, p.saved_at, p.is_saved, p.excerpt, p.image_url, p.audience, p.is_paywalled,
               p.reading_time_minutes, p.word_count, p.read_progress, p.max_read_progress, p.is_viewed
        FROM posts_fts fts
        JOIN posts p ON fts.rowid = p.id
        WHERE {" AND ".join(where_clauses)}
        ORDER BY fts.rank
        LIMIT ?
    """
    params.append(limit)

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params)
            return [PostSummary(**dict(r)) for r in cursor.fetchall()]
        except sqlite3.OperationalError:
            # Fallback to standard LIKE if FTS query syntax is special/malformed.
            # Reuses the same filter clauses as the FTS branch above so a
            # malformed query never silently drops publication/audience/date/read_state filters.
            like_query = f"%{query}%"
            fallback_clauses, fallback_params = _post_search_filters(
                publication,
                audience,
                published_after,
                published_before,
                saved_after,
                saved_before,
                is_saved_only,
                read_state,
            )
            fallback_where = [
                "(title LIKE ? OR excerpt LIKE ? OR publication_name LIKE ? OR author_name LIKE ?)",
                *fallback_clauses,
            ]
            params2: list[str | int | float] = [
                like_query,
                like_query,
                like_query,
                like_query,
                *fallback_params,
            ]
            fallback_sql = f"""
                SELECT id, substack_post_id, url, title, publication_name, author_name,
                       published_at, saved_at, is_saved, excerpt, image_url, audience, is_paywalled,
                       reading_time_minutes, word_count, read_progress, max_read_progress, is_viewed
                FROM posts
                WHERE {" AND ".join(fallback_where)}
                ORDER BY saved_at DESC
                LIMIT ?
            """
            params2.append(limit)
            cursor.execute(fallback_sql, params2)
            return [PostSummary(**dict(r)) for r in cursor.fetchall()]


def list_publications(db_path: Path | None = None) -> list[PublicationSummary]:
    """Return all unique publications in cache with active saved post count."""
    sql = """
        SELECT publication_name, MAX(publication_url) as publication_url, COUNT(*) as post_count
        FROM posts
        WHERE is_saved = 1
        GROUP BY publication_name
        ORDER BY post_count DESC, publication_name ASC
    """
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        return [PublicationSummary(**dict(r)) for r in cursor.fetchall()]


def list_audiences(db_path: Path | None = None) -> list[AudienceSummary]:
    """Return distinct audience tiers present in cache with active saved post counts.

    Discovers the actual values in use (e.g. "everyone", "only_paid") rather than
    hardcoding Substack's audience enum, since it's not officially documented and
    may grow (e.g. "only_founding", "preview").
    """
    sql = """
        SELECT audience, COUNT(*) as post_count
        FROM posts
        WHERE is_saved = 1
        GROUP BY audience
        ORDER BY post_count DESC
    """
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        return [AudienceSummary(**dict(r)) for r in cursor.fetchall()]


def get_status(db_path: Path | None = None) -> SavedPostsStatus:
    """Return metrics and statistics for local SQLite database."""
    target_path = db_path or get_db_path()
    with get_db_connection(target_path) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM posts WHERE is_saved = 1")
        total_saved = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM posts WHERE is_saved = 0")
        total_unsaved = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(DISTINCT publication_name) FROM posts WHERE is_saved = 1"
        )
        total_pubs = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM notes WHERE is_saved = 1")
        total_saved_notes = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM notes WHERE is_saved = 0")
        total_unsaved_notes = cursor.fetchone()[0]

        unread_clause, _ = _read_state_clause("unread")
        in_progress_clause, in_progress_params = _read_state_clause("in_progress")
        finished_clause, finished_params = _read_state_clause("finished")

        cursor.execute(
            f"SELECT COUNT(*) FROM posts WHERE is_saved = 1 AND {unread_clause}"
        )
        posts_unread = cursor.fetchone()[0]

        cursor.execute(
            f"SELECT COUNT(*) FROM posts WHERE is_saved = 1 AND {in_progress_clause}",
            in_progress_params,
        )
        posts_in_progress = cursor.fetchone()[0]

        cursor.execute(
            f"SELECT COUNT(*) FROM posts WHERE is_saved = 1 AND {finished_clause}",
            finished_params,
        )
        posts_fully_read = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT SUM(
                COALESCE(word_count, 0) * (1 - COALESCE(max_read_progress, 0))
            ) FROM posts WHERE is_saved = 1
            """
        )
        remaining_words = cursor.fetchone()[0] or 0
        minutes_remaining_total = math.ceil(remaining_words / WORDS_PER_MINUTE)

        cursor.execute("""
            SELECT completed_at, status FROM sync_runs
            WHERE status = 'success' AND entity = 'post'
            ORDER BY id DESC LIMIT 1
        """)
        last_success_row = cursor.fetchone()
        last_success = last_success_row["completed_at"] if last_success_row else None

        cursor.execute(
            "SELECT status FROM sync_runs WHERE entity = 'post' ORDER BY id DESC LIMIT 1"
        )
        last_status_row = cursor.fetchone()
        last_status = last_status_row["status"] if last_status_row else None

        cursor.execute("""
            SELECT completed_at, status FROM sync_runs
            WHERE status = 'success' AND entity = 'note'
            ORDER BY id DESC LIMIT 1
        """)
        last_note_success_row = cursor.fetchone()
        last_note_success = (
            last_note_success_row["completed_at"] if last_note_success_row else None
        )

        cursor.execute(
            "SELECT status FROM sync_runs WHERE entity = 'note' ORDER BY id DESC LIMIT 1"
        )
        last_note_status_row = cursor.fetchone()
        last_note_status = (
            last_note_status_row["status"] if last_note_status_row else None
        )

        return SavedPostsStatus(
            total_saved_posts=total_saved,
            total_unsaved_posts=total_unsaved,
            total_publications=total_pubs,
            last_successful_sync=last_success,
            last_sync_status=last_status,
            total_saved_notes=total_saved_notes,
            total_unsaved_notes=total_unsaved_notes,
            last_successful_note_sync=last_note_success,
            last_note_sync_status=last_note_status,
            posts_unread=posts_unread,
            posts_in_progress=posts_in_progress,
            posts_fully_read=posts_fully_read,
            minutes_remaining_total=minutes_remaining_total,
            database_path=str(target_path),
        )


def start_sync_run(
    sync_mode: str = "incremental", entity: str = "post", db_path: Path | None = None
) -> int:
    """Create a sync_runs entry and return its ID."""
    now_iso = datetime.now(UTC).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO sync_runs (started_at, status, sync_mode, entity)
            VALUES (?, 'running', ?, ?)
        """,
            (now_iso, sync_mode, entity),
        )
        return cursor.lastrowid  # type: ignore


def finish_sync_run(
    sync_id: int,
    status: str,
    fetched_count: int,
    upserted_count: int,
    reconciled_count: int = 0,
    error_message: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Update a sync_runs record upon completion or failure."""
    now_iso = datetime.now(UTC).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE sync_runs
            SET completed_at = ?, status = ?, fetched_count = ?, upserted_count = ?,
                reconciled_count = ?, error_message = ?
            WHERE id = ?
        """,
            (
                now_iso,
                status,
                fetched_count,
                upserted_count,
                reconciled_count,
                error_message,
                sync_id,
            ),
        )


def upsert_note(note: SavedNote, db_path: Path | None = None) -> SavedNote:
    """Insert or update a note in the database. Returns the updated SavedNote object.

    Looks up by substack_note_id (the note's identity) and branches into an
    explicit UPDATE or INSERT, rather than `INSERT ... ON CONFLICT(url)` as
    posts does — posts.url is not a reliable conflict target since notes may
    have no clean permalink, and a note's identity is unambiguous via its id.
    """
    now_iso = datetime.now(UTC).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, created_at, saved_at, url, body_raw, author_name, author_handle, author_id "
            "FROM notes WHERE substack_note_id = ?",
            (note.substack_note_id,),
        )
        existing = cursor.fetchone()

        created_at = existing["created_at"] if existing else now_iso
        # Never fabricate saved_at: Substack's saved-notes endpoint doesn't expose
        # a bookmark timestamp at all, so this will typically stay None.
        saved_at = note.saved_at or (existing["saved_at"] if existing else None)
        url = note.url or (existing["url"] if existing else None)
        body_raw = note.body_raw or (existing["body_raw"] if existing else None)
        author_name = note.author_name or (
            existing["author_name"] if existing else None
        )
        author_handle = note.author_handle or (
            existing["author_handle"] if existing else None
        )
        author_id = note.author_id or (existing["author_id"] if existing else None)

        if existing:
            cursor.execute(
                """
                UPDATE notes SET
                    url = ?, body_text = ?, body_raw = ?, body_format = ?,
                    author_name = ?, author_handle = ?, author_id = ?,
                    publication_name = ?, publication_url = ?, posted_at = ?,
                    saved_at = ?, unsaved_at = ?, is_saved = ?, is_restack = ?,
                    parent_note_id = ?, attachment_type = ?, attachment_url = ?,
                    restacked_post_url = ?, restacked_post_title = ?,
                    restacked_publication_name = ?, like_count = ?, restack_count = ?,
                    reply_count = ?, word_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    url,
                    note.body_text,
                    body_raw,
                    note.body_format,
                    author_name,
                    author_handle,
                    author_id,
                    note.publication_name,
                    note.publication_url,
                    note.posted_at,
                    saved_at,
                    note.unsaved_at,
                    note.is_saved,
                    note.is_restack,
                    note.parent_note_id,
                    note.attachment_type,
                    note.attachment_url,
                    note.restacked_post_url,
                    note.restacked_post_title,
                    note.restacked_publication_name,
                    note.like_count,
                    note.restack_count,
                    note.reply_count,
                    note.word_count,
                    now_iso,
                    existing["id"],
                ),
            )
            note_id = existing["id"]
        else:
            cursor.execute(
                """
                INSERT INTO notes (
                    substack_note_id, url, body_text, body_raw, body_format,
                    author_name, author_handle, author_id, publication_name,
                    publication_url, posted_at, saved_at, unsaved_at, is_saved,
                    is_restack, parent_note_id, attachment_type, attachment_url,
                    restacked_post_url, restacked_post_title, restacked_publication_name,
                    like_count, restack_count, reply_count, word_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note.substack_note_id,
                    url,
                    note.body_text,
                    body_raw,
                    note.body_format,
                    author_name,
                    author_handle,
                    author_id,
                    note.publication_name,
                    note.publication_url,
                    note.posted_at,
                    saved_at,
                    note.unsaved_at,
                    note.is_saved,
                    note.is_restack,
                    note.parent_note_id,
                    note.attachment_type,
                    note.attachment_url,
                    note.restacked_post_url,
                    note.restacked_post_title,
                    note.restacked_publication_name,
                    note.like_count,
                    note.restack_count,
                    note.reply_count,
                    note.word_count,
                    created_at,
                    now_iso,
                ),
            )
            note_id = cursor.lastrowid

        cursor.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        return SavedNote(**dict(cursor.fetchone()))


def get_note(url_or_id: str | int, db_path: Path | None = None) -> SavedNote | None:
    """Retrieve full note record by local ID, Substack note ID, or URL."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        if isinstance(url_or_id, int) or (
            isinstance(url_or_id, str) and url_or_id.isdigit()
        ):
            cursor.execute("SELECT * FROM notes WHERE id = ?", (int(url_or_id),))
        else:
            clean_url = canonicalize_url(str(url_or_id))
            cursor.execute(
                "SELECT * FROM notes WHERE url = ? OR substack_note_id = ?",
                (clean_url, str(url_or_id)),
            )
        row = cursor.fetchone()
        return SavedNote(**dict(row)) if row else None


def get_note_by_substack_id(
    substack_note_id: str, db_path: Path | None = None
) -> SavedNote | None:
    """Retrieve a note by its Substack note id unambiguously.

    ``get_note()``'s ``url_or_id`` dispatch treats any all-digit string as a
    local row id, and ``substack_note_id`` values are always digits — so sync's
    incremental-check loop, which only ever has the Substack id in hand, needs
    this instead of risking a collision with an unrelated local row.
    """
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM notes WHERE substack_note_id = ?", (substack_note_id,)
        )
        row = cursor.fetchone()
        return SavedNote(**dict(row)) if row else None


def soft_delete_note(
    url_or_id: str | int, db_path: Path | None = None
) -> SavedNote | None:
    """Mark a note as unsaved (is_saved = 0, unsaved_at = now)."""
    now_iso = datetime.now(UTC).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        if isinstance(url_or_id, int) or (
            isinstance(url_or_id, str) and url_or_id.isdigit()
        ):
            cursor.execute("SELECT * FROM notes WHERE id = ?", (int(url_or_id),))
        else:
            clean_url = canonicalize_url(str(url_or_id))
            cursor.execute(
                "SELECT * FROM notes WHERE url = ? OR substack_note_id = ?",
                (clean_url, str(url_or_id)),
            )

        row = cursor.fetchone()
        if not row:
            return None

        cursor.execute(
            "UPDATE notes SET is_saved = 0, unsaved_at = ?, updated_at = ? WHERE id = ?",
            (now_iso, now_iso, row["id"]),
        )

        cursor.execute("SELECT * FROM notes WHERE id = ?", (row["id"],))
        return SavedNote(**dict(cursor.fetchone()))


def reconcile_unsaved_notes(
    remote_note_ids: list[str], db_path: Path | None = None
) -> int:
    """Soft-delete locally-active notes absent from a complete remote saved-note-id set.

    Keys on substack_note_id rather than URL, since a note's id is its reliable
    identity. Intended for use only after a full sync; skips entirely when
    remote_note_ids is empty, since that's more likely a fetch problem than
    genuine mass-unsaving.
    """
    if not remote_note_ids:
        return 0
    remote_ids = {str(i) for i in remote_note_ids if i}

    now_iso = datetime.now(UTC).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, substack_note_id FROM notes WHERE is_saved = 1")
        stale_ids = [
            row["id"]
            for row in cursor.fetchall()
            if row["substack_note_id"] not in remote_ids
        ]
        if not stale_ids:
            return 0

        cursor.executemany(
            "UPDATE notes SET is_saved = 0, unsaved_at = ?, updated_at = ? WHERE id = ?",
            [(now_iso, now_iso, note_id) for note_id in stale_ids],
        )
        return len(stale_ids)


def list_notes(
    limit: int = 20,
    offset: int = 0,
    author: str | None = None,
    is_restack: bool | None = None,
    sort_by: str = "saved_at",
    is_saved_only: bool = True,
    db_path: Path | None = None,
) -> list[NoteSummary]:
    """List notes with pagination, author/restack filters, and sorting."""
    order_col = _NOTE_SORT_COLUMNS.get(sort_by, "saved_at")
    where_clauses = []
    params: list[str | int] = []

    if is_saved_only:
        where_clauses.append("is_saved = 1")
    if author:
        where_clauses.append(
            "(LOWER(author_handle) LIKE LOWER(?) OR LOWER(author_name) LIKE LOWER(?))"
        )
        params.extend([f"%{author}%", f"%{author}%"])
    if is_restack is not None:
        where_clauses.append("is_restack = ?")
        params.append(1 if is_restack else 0)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = f"""
        SELECT id, substack_note_id, url, substr(body_text, 1, 240) AS body_preview,
               author_name, author_handle, publication_name, posted_at, saved_at,
               is_saved, is_restack, restacked_post_title, restacked_post_url,
               like_count, restack_count, reply_count
        FROM notes
        {where_sql}
        ORDER BY {order_col} DESC NULLS LAST
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [NoteSummary(**dict(r)) for r in cursor.fetchall()]


def _note_search_filters(
    author: str | None,
    posted_after: str | None,
    posted_before: str | None,
    saved_after: str | None,
    saved_before: str | None,
    is_saved_only: bool,
    column_prefix: str = "",
) -> tuple[list[str], list[str | int]]:
    """Build shared WHERE clauses/params for search_notes, used by both the FTS
    branch and the LIKE fallback so a malformed FTS query never silently drops
    filters (the bug present in search_posts's fallback)."""
    where_clauses = []
    params: list[str | int] = []

    if is_saved_only:
        where_clauses.append(f"{column_prefix}is_saved = 1")
    if author:
        where_clauses.append(
            f"(LOWER({column_prefix}author_handle) LIKE LOWER(?) "
            f"OR LOWER({column_prefix}author_name) LIKE LOWER(?))"
        )
        params.extend([f"%{author}%", f"%{author}%"])
    if posted_after:
        where_clauses.append(f"{column_prefix}posted_at >= ?")
        params.append(posted_after)
    if posted_before:
        where_clauses.append(f"{column_prefix}posted_at <= ?")
        params.append(posted_before)
    if saved_after:
        where_clauses.append(f"{column_prefix}saved_at >= ?")
        params.append(saved_after)
    if saved_before:
        where_clauses.append(f"{column_prefix}saved_at <= ?")
        params.append(saved_before)

    return where_clauses, params


def search_notes(
    query: str,
    author: str | None = None,
    posted_after: str | None = None,
    posted_before: str | None = None,
    saved_after: str | None = None,
    saved_before: str | None = None,
    limit: int = 20,
    is_saved_only: bool = True,
    db_path: Path | None = None,
) -> list[NoteSummary]:
    """Full-text search over notes using FTS5 BM25 relevance ranking and metadata filters."""
    filter_clauses, filter_params = _note_search_filters(
        author,
        posted_after,
        posted_before,
        saved_after,
        saved_before,
        is_saved_only,
        column_prefix="n.",
    )
    where_clauses = ["notes_fts MATCH ?", *filter_clauses]
    params: list[str | int] = [query, *filter_params]

    sql = f"""
        SELECT n.id, n.substack_note_id, n.url, substr(n.body_text, 1, 240) AS body_preview,
               n.author_name, n.author_handle, n.publication_name, n.posted_at, n.saved_at,
               n.is_saved, n.is_restack, n.restacked_post_title, n.restacked_post_url,
               n.like_count, n.restack_count, n.reply_count
        FROM notes_fts fts
        JOIN notes n ON fts.rowid = n.id
        WHERE {" AND ".join(where_clauses)}
        ORDER BY fts.rank
        LIMIT ?
    """
    params.append(limit)

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params)
            return [NoteSummary(**dict(r)) for r in cursor.fetchall()]
        except sqlite3.OperationalError:
            # Fallback to standard LIKE if FTS query syntax is special/malformed.
            # Reuses the same filter clauses as the FTS branch above so a
            # malformed query never silently drops author/date filters.
            like_query = f"%{query}%"
            fallback_clauses, fallback_params = _note_search_filters(
                author,
                posted_after,
                posted_before,
                saved_after,
                saved_before,
                is_saved_only,
            )
            fallback_where = [
                "(body_text LIKE ? OR author_name LIKE ? OR author_handle LIKE ?)",
                *fallback_clauses,
            ]
            params2: list[str | int] = [
                like_query,
                like_query,
                like_query,
                *fallback_params,
            ]
            fallback_sql = f"""
                SELECT id, substack_note_id, url, substr(body_text, 1, 240) AS body_preview,
                       author_name, author_handle, publication_name, posted_at, saved_at,
                       is_saved, is_restack, restacked_post_title, restacked_post_url,
                       like_count, restack_count, reply_count
                FROM notes
                WHERE {" AND ".join(fallback_where)}
                ORDER BY saved_at DESC
                LIMIT ?
            """
            params2.append(limit)
            cursor.execute(fallback_sql, params2)
            return [NoteSummary(**dict(r)) for r in cursor.fetchall()]


def list_note_authors(db_path: Path | None = None) -> list[NoteAuthorSummary]:
    """Return distinct note authors present in cache with active saved note counts."""
    sql = """
        SELECT author_handle, MAX(author_name) as author_name, COUNT(*) as note_count
        FROM notes
        WHERE is_saved = 1
        GROUP BY author_handle
        ORDER BY note_count DESC
    """
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        return [NoteAuthorSummary(**dict(r)) for r in cursor.fetchall()]
