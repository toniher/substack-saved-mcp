"""Data models and Pydantic schemas for saved posts and sync metrics."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class SavedPost(BaseModel):
    """Represents a Substack post record stored in SQLite."""

    id: Optional[int] = None
    substack_post_id: Optional[str] = None
    url: str
    title: str
    publication_name: str
    publication_url: Optional[str] = None
    author_name: Optional[str] = None
    published_at: Optional[str] = None  # ISO-8601 UTC timestamp of original post
    saved_at: Optional[str] = None      # ISO-8601 UTC timestamp when bookmarked
    unsaved_at: Optional[str] = None    # ISO-8601 UTC timestamp when unsaved
    is_saved: int = 1                   # 1 = active, 0 = unsaved
    excerpt: Optional[str] = None
    content_text: Optional[str] = None
    image_url: Optional[str] = None
    audience: Optional[str] = None       # raw Substack audience tier, e.g. "everyone", "only_paid"
    is_paywalled: int = 0
    reading_time_minutes: Optional[int] = None
    word_count: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PostSummary(BaseModel):
    """Concise representation of a post for listing and search tool responses."""

    id: Optional[int] = None
    substack_post_id: Optional[str] = None
    url: str
    title: str
    publication_name: str
    author_name: Optional[str] = None
    published_at: Optional[str] = None
    saved_at: Optional[str] = None
    is_saved: int = 1
    excerpt: Optional[str] = None
    image_url: Optional[str] = None
    audience: Optional[str] = None
    is_paywalled: int = 0
    reading_time_minutes: Optional[int] = None
    word_count: Optional[int] = None


class PublicationSummary(BaseModel):
    """Summary of a publication present in the local cache."""

    publication_name: str
    publication_url: Optional[str] = None
    post_count: int


class AudienceSummary(BaseModel):
    """Summary of an audience tier present in the local cache."""

    audience: Optional[str] = None
    post_count: int


class SyncRun(BaseModel):
    """Tracks execution history of sync operations."""

    id: Optional[int] = None
    started_at: str
    completed_at: Optional[str] = None
    status: str  # 'success', 'partial', 'failed', 'auth_required'
    sync_mode: str = "incremental"  # 'incremental' or 'full'
    fetched_count: int = 0
    upserted_count: int = 0
    reconciled_count: int = 0  # posts soft-deleted because they left the remote saved list
    error_message: Optional[str] = None


class SavedPostsStatus(BaseModel):
    """Overall status and metrics of the local SQLite cache."""

    total_saved_posts: int
    total_unsaved_posts: int
    total_publications: int
    last_successful_sync: Optional[str] = None
    last_sync_status: Optional[str] = None
    database_path: str
