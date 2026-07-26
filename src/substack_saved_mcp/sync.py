"""Sync engine for retrieving Substack saved posts into the local SQLite cache."""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from substack_saved_mcp.database import (
    finish_sync_run,
    get_post,
    init_db,
    reconcile_unsaved_posts,
    start_sync_run,
    upsert_post,
)
from substack_saved_mcp.models import SavedPost, SyncRun
from substack_saved_mcp.substack_client import AuthRequiredError, SubstackSavedPostsClient
from substack_saved_mcp.url_utils import canonicalize_url

logger = logging.getLogger(__name__)

# Rough average adult reading speed used to derive reading time from word count.
WORDS_PER_MINUTE = 200


def _first_positive_int(source: Dict[str, Any], *keys: str) -> Optional[int]:
    """Return the first of ``keys`` whose value coerces to a positive int, else None."""
    for key in keys:
        val = source.get(key)
        if val is None:
            continue
        try:
            n = int(val)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None


def parse_remote_post(raw_data: Dict[str, Any]) -> SavedPost:
    """Parse raw Substack API JSON payload or DOM dict into a typed SavedPost object."""
    # Check if raw_data is a normalized dictionary from DOM extraction. Use an explicit
    # marker: reader-API post objects also carry a "canonical_url" key and must instead
    # go through the full API mapping below (canonicalization, post_date, id, etc.).
    if raw_data.get("_dom"):
        return SavedPost(
            url=raw_data["canonical_url"],
            title=raw_data.get("title") or "Untitled Substack Post",
            publication_name=raw_data.get("publication_name") or "Substack",
            publication_url=raw_data.get("publication_url"),
            author_name=raw_data.get("author_name"),
            excerpt=raw_data.get("excerpt"),
            published_at=raw_data.get("published_at"),
            saved_at=raw_data.get("saved_at"),
            audience=raw_data.get("audience"),
            is_saved=1,
        )

    # Substack API json mapping
    post_obj = raw_data.get("post") or raw_data.get("item") or raw_data
    pub_obj = raw_data.get("publication") or post_obj.get("publication") or {}

    substack_id = str(post_obj.get("id")) if post_obj.get("id") else None
    raw_url = post_obj.get("canonical_url") or post_obj.get("url") or ""
    clean_url = canonicalize_url(raw_url)

    title = post_obj.get("title") or "Untitled Substack Post"
    pub_name = pub_obj.get("name") or "Substack"
    pub_url = pub_obj.get("custom_domain") or f"https://{pub_obj.get('subdomain')}.substack.com" if pub_obj.get("subdomain") else None
    author = post_obj.get("author") or post_obj.get("author_name") or (pub_obj.get("author_name") if pub_obj else None)

    # Dates. Leave saved_at unknown (None) rather than stamping the sync moment.
    published_at = post_obj.get("post_date") or post_obj.get("published_at")
    saved_at = raw_data.get("created_at") or raw_data.get("saved_at")

    excerpt = post_obj.get("description") or post_obj.get("subtitle") or post_obj.get("excerpt")
    content_text = post_obj.get("body_html") or post_obj.get("content_text")
    image_url = post_obj.get("cover_image") or post_obj.get("image_url")
    audience = post_obj.get("audience")
    is_paywalled = 1 if audience == "only_paid" or post_obj.get("is_paywalled") else 0

    # Word count: Substack's exact reader-API field name isn't confirmed, so map the
    # most likely candidates defensively (verify with `inspect-network` if these stay
    # empty). Reading time is *derived* from word count rather than read from a field,
    # since a wrong guess about that field's unit (seconds vs minutes) would store a
    # badly wrong value — deriving at ~WPM is unambiguous and is how Substack itself
    # presents it.
    word_count = _first_positive_int(post_obj, "wordcount", "word_count", "words")
    reading_time_minutes = (
        -(-word_count // WORDS_PER_MINUTE) if word_count else None  # ceil division, min 1
    )

    return SavedPost(
        substack_post_id=substack_id,
        url=clean_url,
        title=title,
        publication_name=pub_name,
        publication_url=pub_url,
        author_name=author,
        published_at=published_at,
        saved_at=saved_at,
        is_saved=1,
        excerpt=excerpt,
        content_text=content_text,
        image_url=image_url,
        audience=audience,
        is_paywalled=is_paywalled,
        word_count=word_count,
        reading_time_minutes=reading_time_minutes,
    )


def sync_saved_posts(
    force: bool = False,
    db_path: Optional[Path] = None,
    client: Optional[SubstackSavedPostsClient] = None,
) -> SyncRun:
    """Execute an incremental or full sync of Substack saved posts into SQLite cache."""
    init_db(db_path)
    sync_mode = "full" if force else "incremental"
    sync_id = start_sync_run(sync_mode=sync_mode, db_path=db_path)

    active_client = client or SubstackSavedPostsClient()
    active_client.reset_cache()
    total_fetched = 0
    total_upserted = 0
    consecutive_matches = 0
    MAX_CONSECUTIVE_MATCHES = 3
    # Only populated meaningfully for a force/full sync, which enumerates every
    # currently-saved remote post; an incremental sync may stop early and its
    # partial list must never be used to infer removals.
    remote_urls: List[str] = []

    page_size = 50
    offset = 0

    try:
        while True:
            remote_items = active_client.fetch_saved_posts_page(limit=page_size, offset=offset)
            if not remote_items:
                break

            total_fetched += len(remote_items)

            for item in remote_items:
                parsed_post = parse_remote_post(item)
                if not parsed_post.url:
                    continue
                remote_urls.append(parsed_post.url)

                # Incremental sync check
                if not force:
                    existing = get_post(parsed_post.url, db_path=db_path)
                    if existing and existing.is_saved == 1 and existing.saved_at == parsed_post.saved_at:
                        consecutive_matches += 1
                        if consecutive_matches >= MAX_CONSECUTIVE_MATCHES:
                            logger.info(f"Incremental sync: encountered {consecutive_matches} existing matches. Stopping early.")
                            break
                    else:
                        consecutive_matches = 0

                upserted_post = upsert_post(parsed_post, db_path=db_path)
                total_upserted += 1

            if not force and consecutive_matches >= MAX_CONSECUTIVE_MATCHES:
                break

            if len(remote_items) < page_size:
                break

            offset += page_size

        reconciled_count = 0
        if force:
            reconciled_count = reconcile_unsaved_posts(remote_urls, db_path=db_path)
            if reconciled_count:
                logger.info(
                    f"Reconciliation: soft-deleted {reconciled_count} post(s) no longer "
                    "in the remote saved list."
                )

        finish_sync_run(
            sync_id=sync_id,
            status="success",
            fetched_count=total_fetched,
            upserted_count=total_upserted,
            reconciled_count=reconciled_count,
            db_path=db_path,
        )
        return SyncRun(
            id=sync_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            status="success",
            sync_mode=sync_mode,
            fetched_count=total_fetched,
            upserted_count=total_upserted,
            reconciled_count=reconciled_count,
        )

    except AuthRequiredError as e:
        msg = str(e)
        finish_sync_run(
            sync_id=sync_id,
            status="auth_required",
            fetched_count=total_fetched,
            upserted_count=total_upserted,
            error_message=msg,
            db_path=db_path,
        )
        return SyncRun(
            id=sync_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            status="auth_required",
            sync_mode=sync_mode,
            fetched_count=total_fetched,
            upserted_count=total_upserted,
            error_message=msg,
        )
    except Exception as e:
        msg = f"Sync failed: {str(e)}"
        logger.exception(msg)
        finish_sync_run(
            sync_id=sync_id,
            status="failed",
            fetched_count=total_fetched,
            upserted_count=total_upserted,
            error_message=msg,
            db_path=db_path,
        )
        return SyncRun(
            id=sync_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            status="failed",
            sync_mode=sync_mode,
            fetched_count=total_fetched,
            upserted_count=total_upserted,
            error_message=msg,
        )
