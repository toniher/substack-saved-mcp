"""SQLite database schema, FTS5 virtual table indexing, and query repository."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, List, Optional, Tuple, Union

from substack_saved_mcp.config import get_db_path
from substack_saved_mcp.models import (
    PostSummary,
    PublicationSummary,
    SavedPost,
    SavedPostsStatus,
    SyncRun,
)
from substack_saved_mcp.url_utils import canonicalize_url


@contextmanager
def get_db_connection(db_path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
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


def init_db(db_path: Optional[Path] = None) -> None:
    """Initialize SQLite database tables, indexes, FTS5 virtual table, and triggers."""
    from substack_saved_mcp.config import ensure_app_dirs
    ensure_app_dirs()
    with get_db_connection(db_path) as conn:
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
                is_paywalled INTEGER DEFAULT 0,
                reading_time_minutes INTEGER,
                word_count INTEGER,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_posts_url ON posts(url);
            CREATE INDEX IF NOT EXISTS idx_posts_published_at ON posts(published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_posts_saved_at ON posts(saved_at DESC);
            CREATE INDEX IF NOT EXISTS idx_posts_is_saved ON posts(is_saved);
            CREATE INDEX IF NOT EXISTS idx_posts_publication ON posts(publication_name);

            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                sync_mode TEXT NOT NULL DEFAULT 'incremental',
                fetched_count INTEGER DEFAULT 0,
                upserted_count INTEGER DEFAULT 0,
                error_message TEXT
            );

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


def upsert_post(post: SavedPost, db_path: Optional[Path] = None) -> SavedPost:
    """Insert or update a post in the database. Returns the updated SavedPost object."""
    now_iso = datetime.now(timezone.utc).isoformat()
    clean_url = canonicalize_url(post.url)

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        # Check if record already exists by substack_post_id or URL
        existing = None
        if post.substack_post_id:
            cursor.execute("SELECT id, created_at, saved_at FROM posts WHERE substack_post_id = ?", (post.substack_post_id,))
            existing = cursor.fetchone()
        if not existing and clean_url:
            cursor.execute("SELECT id, created_at, saved_at FROM posts WHERE url = ?", (clean_url,))
            existing = cursor.fetchone()

        created_at = existing["created_at"] if existing else now_iso
        saved_at = post.saved_at or (existing["saved_at"] if existing else now_iso)

        cursor.execute("""
            INSERT INTO posts (
                substack_post_id, url, title, publication_name, publication_url,
                author_name, published_at, saved_at, unsaved_at, is_saved,
                excerpt, content_text, image_url, is_paywalled, reading_time_minutes,
                word_count, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                substack_post_id = COALESCE(excluded.substack_post_id, posts.substack_post_id),
                title = excluded.title,
                publication_name = excluded.publication_name,
                publication_url = COALESCE(excluded.publication_url, posts.publication_url),
                author_name = COALESCE(excluded.author_name, posts.author_name),
                published_at = COALESCE(excluded.published_at, posts.published_at),
                saved_at = COALESCE(excluded.saved_at, posts.saved_at),
                unsaved_at = excluded.unsaved_at,
                is_saved = excluded.is_saved,
                excerpt = COALESCE(excluded.excerpt, posts.excerpt),
                content_text = COALESCE(excluded.content_text, posts.content_text),
                image_url = COALESCE(excluded.image_url, posts.image_url),
                is_paywalled = excluded.is_paywalled,
                reading_time_minutes = COALESCE(excluded.reading_time_minutes, posts.reading_time_minutes),
                word_count = COALESCE(excluded.word_count, posts.word_count),
                metadata_json = COALESCE(excluded.metadata_json, posts.metadata_json),
                updated_at = excluded.updated_at
        """, (
            post.substack_post_id,
            clean_url,
            post.title,
            post.publication_name,
            post.publication_url,
            post.author_name,
            post.published_at,
            saved_at,
            post.unsaved_at,
            post.is_saved,
            post.excerpt,
            post.content_text,
            post.image_url,
            post.is_paywalled,
            post.reading_time_minutes,
            post.word_count,
            post.metadata_json,
            created_at,
            now_iso,
        ))

        post_id = cursor.lastrowid if not existing else existing["id"]
        cursor.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = cursor.fetchone()
        return SavedPost(**dict(row))


def soft_delete_post(url_or_id: Union[str, int], db_path: Optional[Path] = None) -> Optional[SavedPost]:
    """Mark a post as unsaved (is_saved = 0, unsaved_at = now)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        if isinstance(url_or_id, int) or (isinstance(url_or_id, str) and url_or_id.isdigit()):
            cursor.execute("SELECT * FROM posts WHERE id = ?", (int(url_or_id),))
        else:
            clean_url = canonicalize_url(str(url_or_id))
            cursor.execute("SELECT * FROM posts WHERE url = ? OR substack_post_id = ?", (clean_url, str(url_or_id)))

        row = cursor.fetchone()
        if not row:
            return None

        cursor.execute("""
            UPDATE posts
            SET is_saved = 0, unsaved_at = ?, updated_at = ?
            WHERE id = ?
        """, (now_iso, now_iso, row["id"]))

        cursor.execute("SELECT * FROM posts WHERE id = ?", (row["id"],))
        return SavedPost(**dict(cursor.fetchone()))


def get_post(url_or_id: Union[str, int], db_path: Optional[Path] = None) -> Optional[SavedPost]:
    """Retrieve full post record by local ID, Substack post ID, or URL."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        if isinstance(url_or_id, int) or (isinstance(url_or_id, str) and url_or_id.isdigit()):
            cursor.execute("SELECT * FROM posts WHERE id = ?", (int(url_or_id),))
        else:
            clean_url = canonicalize_url(str(url_or_id))
            cursor.execute("SELECT * FROM posts WHERE url = ? OR substack_post_id = ?", (clean_url, str(url_or_id)))
        row = cursor.fetchone()
        return SavedPost(**dict(row)) if row else None


def list_posts(
    limit: int = 20,
    offset: int = 0,
    publication: Optional[str] = None,
    sort_by: str = "saved_at",
    is_saved_only: bool = True,
    db_path: Optional[Path] = None,
) -> List[PostSummary]:
    """List posts with pagination, publication filter, and sorting."""
    order_col = "published_at" if sort_by == "published_at" else "saved_at"
    where_clauses = []
    params: List[Union[str, int]] = []

    if is_saved_only:
        where_clauses.append("is_saved = 1")
    if publication:
        where_clauses.append("LOWER(publication_name) LIKE LOWER(?)")
        params.append(f"%{publication}%")

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = f"""
        SELECT id, substack_post_id, url, title, publication_name, author_name,
               published_at, saved_at, is_saved, excerpt, is_paywalled
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


def search_posts(
    query: str,
    publication: Optional[str] = None,
    published_after: Optional[str] = None,
    published_before: Optional[str] = None,
    saved_after: Optional[str] = None,
    saved_before: Optional[str] = None,
    limit: int = 20,
    is_saved_only: bool = True,
    db_path: Optional[Path] = None,
) -> List[PostSummary]:
    """Full-text search over posts using FTS5 BM25 relevance ranking and metadata filters."""
    where_clauses = ["posts_fts MATCH ?"]
    params: List[Union[str, int]] = [query]

    if is_saved_only:
        where_clauses.append("p.is_saved = 1")
    if publication:
        where_clauses.append("LOWER(p.publication_name) LIKE LOWER(?)")
        params.append(f"%{publication}%")
    if published_after:
        where_clauses.append("p.published_at >= ?")
        params.append(published_after)
    if published_before:
        where_clauses.append("p.published_at <= ?")
        params.append(published_before)
    if saved_after:
        where_clauses.append("p.saved_at >= ?")
        params.append(saved_after)
    if saved_before:
        where_clauses.append("p.saved_at <= ?")
        params.append(saved_before)

    sql = f"""
        SELECT p.id, p.substack_post_id, p.url, p.title, p.publication_name, p.author_name,
               p.published_at, p.saved_at, p.is_saved, p.excerpt, p.is_paywalled
        FROM posts_fts fts
        JOIN posts p ON fts.rowid = p.id
        WHERE {' AND '.join(where_clauses)}
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
            # Fallback to standard LIKE if FTS query syntax is special/malformed
            like_query = f"%{query}%"
            fallback_sql = """
                SELECT id, substack_post_id, url, title, publication_name, author_name,
                       published_at, saved_at, is_saved, excerpt, is_paywalled
                FROM posts
                WHERE (title LIKE ? OR excerpt LIKE ? OR publication_name LIKE ? OR author_name LIKE ?)
                AND is_saved = 1
                ORDER BY saved_at DESC
                LIMIT ?
            """
            cursor.execute(fallback_sql, (like_query, like_query, like_query, like_query, limit))
            return [PostSummary(**dict(r)) for r in cursor.fetchall()]


def list_publications(db_path: Optional[Path] = None) -> List[PublicationSummary]:
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


def get_status(db_path: Optional[Path] = None) -> SavedPostsStatus:
    """Return metrics and statistics for local SQLite database."""
    target_path = db_path or get_db_path()
    with get_db_connection(target_path) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM posts WHERE is_saved = 1")
        total_saved = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM posts WHERE is_saved = 0")
        total_unsaved = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(DISTINCT publication_name) FROM posts WHERE is_saved = 1")
        total_pubs = cursor.fetchone()[0]

        cursor.execute("""
            SELECT completed_at, status FROM sync_runs
            WHERE status = 'success'
            ORDER BY id DESC LIMIT 1
        """)
        last_success_row = cursor.fetchone()
        last_success = last_success_row["completed_at"] if last_success_row else None

        cursor.execute("SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1")
        last_status_row = cursor.fetchone()
        last_status = last_status_row["status"] if last_status_row else None

        return SavedPostsStatus(
            total_saved_posts=total_saved,
            total_unsaved_posts=total_unsaved,
            total_publications=total_pubs,
            last_successful_sync=last_success,
            last_sync_status=last_status,
            database_path=str(target_path),
        )


def start_sync_run(sync_mode: str = "incremental", db_path: Optional[Path] = None) -> int:
    """Create a sync_runs entry and return its ID."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sync_runs (started_at, status, sync_mode)
            VALUES (?, 'running', ?)
        """, (now_iso, sync_mode))
        return cursor.lastrowid  # type: ignore


def finish_sync_run(
    sync_id: int,
    status: str,
    fetched_count: int,
    upserted_count: int,
    error_message: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Update a sync_runs record upon completion or failure."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE sync_runs
            SET completed_at = ?, status = ?, fetched_count = ?, upserted_count = ?, error_message = ?
            WHERE id = ?
        """, (now_iso, status, fetched_count, upserted_count, error_message, sync_id))
