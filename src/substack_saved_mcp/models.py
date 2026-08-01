"""Data models and Pydantic schemas for saved posts and sync metrics."""

from pydantic import BaseModel


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
    saved_at: str | None = None      # ISO-8601 UTC timestamp when bookmarked
    unsaved_at: str | None = None    # ISO-8601 UTC timestamp when unsaved
    is_saved: int = 1                   # 1 = active, 0 = unsaved
    excerpt: str | None = None
    content_text: str | None = None
    image_url: str | None = None
    audience: str | None = None       # raw Substack audience tier, e.g. "everyone", "only_paid"
    is_paywalled: int = 0
    reading_time_minutes: int | None = None
    word_count: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


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


class PublicationSummary(BaseModel):
    """Summary of a publication present in the local cache."""

    publication_name: str
    publication_url: str | None = None
    post_count: int


class AudienceSummary(BaseModel):
    """Summary of an audience tier present in the local cache."""

    audience: str | None = None
    post_count: int


class SyncRun(BaseModel):
    """Tracks execution history of sync operations."""

    id: int | None = None
    started_at: str
    completed_at: str | None = None
    status: str  # 'success', 'partial', 'failed', 'auth_required'
    sync_mode: str = "incremental"  # 'incremental' or 'full'
    fetched_count: int = 0
    upserted_count: int = 0
    reconciled_count: int = 0  # posts soft-deleted because they left the remote saved list
    error_message: str | None = None


class SavedPostsStatus(BaseModel):
    """Overall status and metrics of the local SQLite cache."""

    total_saved_posts: int
    total_unsaved_posts: int
    total_publications: int
    last_successful_sync: str | None = None
    last_sync_status: str | None = None
    database_path: str
