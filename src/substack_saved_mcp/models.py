"""Data models and Pydantic schemas for saved posts and sync metrics."""

import math

from pydantic import BaseModel, computed_field

from .config import get_fully_read_threshold

WORDS_PER_MINUTE = 200  # kept in sync with sync.py's constant of the same name


def _is_fully_read(max_read_progress: float | None) -> bool:
    if max_read_progress is None:
        return False
    return max_read_progress >= get_fully_read_threshold()


def _minutes_remaining(
    word_count: int | None, max_read_progress: float | None
) -> int | None:
    if word_count is None:
        return None
    progress = max_read_progress or 0.0
    return math.ceil(word_count * (1 - progress) / WORDS_PER_MINUTE)


class SavedPost(BaseModel):
    """Represents a Substack post record stored in SQLite."""

    id: int | None = None
    substack_post_id: str | None = None
    url: str
    title: str
    publication_name: str
    publication_url: str | None = None
    author_name: str | None = None
    published_at: str | None = None  # ISO-8601 UTC timestamp of original post
    saved_at: str | None = None  # ISO-8601 UTC timestamp when bookmarked
    unsaved_at: str | None = None  # ISO-8601 UTC timestamp when unsaved
    is_saved: int = 1  # 1 = active, 0 = unsaved
    excerpt: str | None = None
    content_text: str | None = None
    image_url: str | None = None
    audience: str | None = (
        None  # raw Substack audience tier, e.g. "everyone", "only_paid"
    )
    is_paywalled: int = 0
    reading_time_minutes: int | None = None
    word_count: int | None = None
    read_progress: float | None = None  # current scroll position, 0.0-1.0
    max_read_progress: float | None = None  # high-water mark, 0.0-1.0
    is_viewed: int = 0  # 1 = post was opened at least once
    created_at: str | None = None
    updated_at: str | None = None

    @computed_field
    @property
    def is_fully_read(self) -> bool:
        return _is_fully_read(self.max_read_progress)

    @computed_field
    @property
    def minutes_remaining(self) -> int | None:
        return _minutes_remaining(self.word_count, self.max_read_progress)


class PostSummary(BaseModel):
    """Concise representation of a post for listing and search tool responses."""

    id: int | None = None
    substack_post_id: str | None = None
    url: str
    title: str
    publication_name: str
    author_name: str | None = None
    published_at: str | None = None
    saved_at: str | None = None
    is_saved: int = 1
    excerpt: str | None = None
    image_url: str | None = None
    audience: str | None = None
    is_paywalled: int = 0
    reading_time_minutes: int | None = None
    word_count: int | None = None
    read_progress: float | None = None
    max_read_progress: float | None = None
    is_viewed: int = 0

    @computed_field
    @property
    def is_fully_read(self) -> bool:
        return _is_fully_read(self.max_read_progress)

    @computed_field
    @property
    def minutes_remaining(self) -> int | None:
        return _minutes_remaining(self.word_count, self.max_read_progress)


class PublicationSummary(BaseModel):
    """Summary of a publication present in the local cache."""

    publication_name: str
    publication_url: str | None = None
    post_count: int


class AudienceSummary(BaseModel):
    """Summary of an audience tier present in the local cache."""

    audience: str | None = None
    post_count: int


class SavedNote(BaseModel):
    """Represents a Substack note (short-form post) stored in SQLite.

    Notes have no title, publication tier, or word-count/reading-time
    concept the way posts do; they gain an author handle, engagement
    counts, and (for restacks) an attached post instead. Substack's
    reader API never exposes a bookmark timestamp for notes (unlike
    posts), so ``saved_at`` is always None here.
    """

    id: int | None = None
    substack_note_id: str
    url: str | None = None
    body_text: str = ""
    body_raw: str | None = None  # verbatim source body_json, JSON-encoded
    body_format: str | None = None  # 'prosemirror_json' | 'text'
    author_name: str | None = None
    author_handle: str | None = None
    author_id: str | None = None
    publication_name: str | None = None
    publication_url: str | None = None
    posted_at: str | None = None
    saved_at: str | None = None
    unsaved_at: str | None = None
    is_saved: int = 1
    is_restack: int = 0
    parent_note_id: str | None = None  # raw ancestor_path when a note is a reply
    attachment_type: str | None = None  # 'image' | 'link'
    attachment_url: str | None = None
    restacked_post_url: str | None = None
    restacked_post_title: str | None = None
    restacked_publication_name: str | None = None
    like_count: int | None = None
    restack_count: int | None = None
    reply_count: int | None = None
    word_count: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class NoteSummary(BaseModel):
    """Concise representation of a note for listing and search tool responses."""

    id: int | None = None
    substack_note_id: str
    url: str | None = None
    body_preview: str = ""
    author_name: str | None = None
    author_handle: str | None = None
    publication_name: str | None = None
    posted_at: str | None = None
    saved_at: str | None = None
    is_saved: int = 1
    is_restack: int = 0
    restacked_post_title: str | None = None
    restacked_post_url: str | None = None
    like_count: int | None = None
    restack_count: int | None = None
    reply_count: int | None = None


class NoteAuthorSummary(BaseModel):
    """Summary of a note author present in the local cache."""

    author_handle: str | None = None
    author_name: str | None = None
    note_count: int


class SyncRun(BaseModel):
    """Tracks execution history of sync operations."""

    id: int | None = None
    started_at: str
    completed_at: str | None = None
    status: str  # 'success', 'partial', 'failed', 'auth_required'
    sync_mode: str = "incremental"  # 'incremental' or 'full'
    entity: str = "post"  # 'post' or 'note'
    fetched_count: int = 0
    upserted_count: int = 0
    reconciled_count: int = (
        0  # posts/notes soft-deleted because they left the remote saved list
    )
    error_message: str | None = None


class SavedPostsStatus(BaseModel):
    """Overall status and metrics of the local SQLite cache."""

    total_saved_posts: int
    total_unsaved_posts: int
    total_publications: int
    last_successful_sync: str | None = None
    last_sync_status: str | None = None
    total_saved_notes: int = 0
    total_unsaved_notes: int = 0
    last_successful_note_sync: str | None = None
    last_note_sync_status: str | None = None
    posts_unread: int = 0
    posts_in_progress: int = 0
    posts_fully_read: int = 0
    minutes_remaining_total: int = 0
    database_path: str
