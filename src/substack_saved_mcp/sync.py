"""Sync engine for retrieving Substack saved posts and notes into the local SQLite cache."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from substack_saved_mcp.content_utils import note_body_to_text
from substack_saved_mcp.database import (
    finish_sync_run,
    get_note_by_substack_id,
    get_post,
    init_db,
    reconcile_unsaved_notes,
    reconcile_unsaved_posts,
    start_sync_run,
    upsert_note,
    upsert_post,
)
from substack_saved_mcp.models import SavedNote, SavedPost, SyncRun
from substack_saved_mcp.substack_client import (
    AuthRequiredError,
    SubstackSavedPostsClient,
)
from substack_saved_mcp.url_utils import canonicalize_url

logger = logging.getLogger(__name__)

# Rough average adult reading speed used to derive reading time from word count.
WORDS_PER_MINUTE = 200

# Both sync loops stop early on this many consecutive already-synced items,
# rather than re-walking the caller's entire saved history every incremental run.
MAX_CONSECUTIVE_MATCHES = 3


def _first_positive_int(source: dict[str, Any], *keys: str) -> int | None:
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


def _int_or_none(value: Any) -> int | None:
    """Coerce a value to int, tolerating 0 (unlike _first_positive_int, which drops it)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    """Coerce a value to float, tolerating 0.0, and clamp into [0.0, 1.0] since this
    is only ever used for read-progress fractions."""
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, n))


def _build_sync_run(
    sync_id: int,
    status: str,
    sync_mode: str,
    entity: str,
    started_at: str,
    fetched_count: int,
    upserted_count: int,
    reconciled_count: int = 0,
    error_message: str | None = None,
) -> SyncRun:
    """Construct the SyncRun returned to callers, sharing one timestamp/field
    assembly point instead of the three near-duplicate constructions each
    sync function used to have. Takes the real ``started_at`` captured before
    the fetch loop began, rather than stamping it at finish time (which
    previously made started_at == completed_at regardless of how long the
    sync actually took)."""
    return SyncRun(
        id=sync_id,
        started_at=started_at,
        completed_at=datetime.now(UTC).isoformat(),
        status=status,
        sync_mode=sync_mode,
        entity=entity,
        fetched_count=fetched_count,
        upserted_count=upserted_count,
        reconciled_count=reconciled_count,
        error_message=error_message,
    )


def parse_remote_post(raw_data: dict[str, Any]) -> SavedPost:
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
    pub_url = (
        pub_obj.get("custom_domain")
        or f"https://{pub_obj.get('subdomain')}.substack.com"
        if pub_obj.get("subdomain")
        else None
    )
    author = (
        post_obj.get("author")
        or post_obj.get("author_name")
        or (pub_obj.get("author_name") if pub_obj else None)
    )

    # Dates. Leave saved_at unknown (None) rather than stamping the sync moment.
    # Checked at both levels: the legacy reader-posts API puts it on the item
    # itself, the unified reader/saved API nests it under "post" instead.
    published_at = post_obj.get("post_date") or post_obj.get("published_at")
    saved_at = (
        raw_data.get("created_at")
        or raw_data.get("saved_at")
        or post_obj.get("saved_at")
    )

    excerpt = (
        post_obj.get("description")
        or post_obj.get("subtitle")
        or post_obj.get("excerpt")
    )
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
        -(-word_count // WORDS_PER_MINUTE)
        if word_count
        else None  # ceil division, min 1
    )

    # Reading progress. Confirmed live to sit on the post object in both the
    # unified and legacy payloads, so unlike saved_at no dual-level lookup is
    # needed. DOM cards never carry this, so the early-return branch above
    # leaves all three as their model defaults (None/None/0), which reads as
    # "unread" rather than fabricating a false zero.
    read_progress = _float_or_none(post_obj.get("read_progress"))
    max_read_progress = _float_or_none(post_obj.get("max_read_progress"))
    is_viewed = 1 if post_obj.get("is_viewed") else 0

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
        read_progress=read_progress,
        max_read_progress=max_read_progress,
        is_viewed=is_viewed,
    )


def sync_saved_posts(
    force: bool = False,
    db_path: Path | None = None,
    client: SubstackSavedPostsClient | None = None,
) -> SyncRun:
    """Execute an incremental or full sync of Substack saved posts into SQLite cache."""
    init_db(db_path)
    sync_mode = "full" if force else "incremental"
    started_at = datetime.now(UTC).isoformat()
    sync_id = start_sync_run(sync_mode=sync_mode, db_path=db_path)

    active_client = client or SubstackSavedPostsClient()
    active_client.reset_cache()
    total_fetched = 0
    total_upserted = 0
    consecutive_matches = 0
    # Only populated meaningfully for a force/full sync, which enumerates every
    # currently-saved remote post; an incremental sync may stop early and its
    # partial list must never be used to infer removals.
    remote_urls: list[str] = []

    page_size = 50
    offset = 0

    try:
        while True:
            remote_items = active_client.fetch_saved_posts_page(
                limit=page_size, offset=offset
            )
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
                    if (
                        existing
                        and existing.is_saved == 1
                        and existing.saved_at == parsed_post.saved_at
                    ):
                        consecutive_matches += 1
                        if consecutive_matches >= MAX_CONSECUTIVE_MATCHES:
                            logger.info(
                                f"Incremental sync: encountered {consecutive_matches} existing matches. Stopping early."
                            )
                            break
                    else:
                        consecutive_matches = 0

                upsert_post(parsed_post, db_path=db_path)
                total_upserted += 1

            if not force and consecutive_matches >= MAX_CONSECUTIVE_MATCHES:
                break

            if len(remote_items) < page_size:
                break

            offset += page_size

        # A persistent failure mid-pagination (e.g. a 429 surviving every retry)
        # makes a fetcher return whatever it had collected so far rather than
        # signaling failure outright, so that partial progress isn't thrown
        # away entirely. But that same partial list must never drive
        # reconciliation: a post absent from it only because the fetch was cut
        # short would look identical to one genuinely unsaved on Substack, and
        # reconcile_unsaved_posts() would wrongly soft-delete it.
        fetch_truncated = active_client.is_posts_fetch_truncated()
        reconciled_count = 0
        if force:
            if fetch_truncated:
                logger.warning(
                    "Force sync's fetch was truncated by a persistent failure "
                    "mid-pagination; skipping reconciliation this run so posts "
                    "that merely couldn't be fetched aren't soft-deleted as if "
                    "they were unsaved remotely."
                )
            else:
                reconciled_count = reconcile_unsaved_posts(remote_urls, db_path=db_path)
                if reconciled_count:
                    logger.info(
                        f"Reconciliation: soft-deleted {reconciled_count} post(s) no longer in the remote saved list."
                    )

        status = "partial" if fetch_truncated else "success"
        error_message = (
            "Fetch was truncated by a persistent failure mid-pagination "
            "(e.g. a 429 that survived every retry); some saved posts may be "
            "missing from this sync, and reconciliation was skipped if this "
            "was a --force sync."
            if fetch_truncated
            else None
        )
        finish_sync_run(
            sync_id=sync_id,
            status=status,
            fetched_count=total_fetched,
            upserted_count=total_upserted,
            reconciled_count=reconciled_count,
            error_message=error_message,
            db_path=db_path,
        )
        return _build_sync_run(
            sync_id,
            status,
            sync_mode,
            "post",
            started_at,
            total_fetched,
            total_upserted,
            reconciled_count=reconciled_count,
            error_message=error_message,
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
        return _build_sync_run(
            sync_id,
            "auth_required",
            sync_mode,
            "post",
            started_at,
            total_fetched,
            total_upserted,
            error_message=msg,
        )
    except Exception as e:
        msg = str(e)
        logger.exception(f"Sync failed: {msg}")
        finish_sync_run(
            sync_id=sync_id,
            status="failed",
            fetched_count=total_fetched,
            upserted_count=total_upserted,
            error_message=msg,
            db_path=db_path,
        )
        return _build_sync_run(
            sync_id,
            "failed",
            sync_mode,
            "post",
            started_at,
            total_fetched,
            total_upserted,
            error_message=msg,
        )


def parse_remote_note(raw_data: dict[str, Any]) -> SavedNote | None:
    """Parse a raw saved-notes API item (or single-note-fetch wrapper) into a
    typed SavedNote, or None if it carries no usable note id.

    Confirmed against a live capture of Substack's ``/api/v1/reader/saved`` and
    ``/api/v1/reader/comment/{id}`` endpoints (see CLAUDE.md). Notes carry no
    bookmark timestamp anywhere in the payload — only a boolean ``is_saved`` —
    so ``saved_at`` is always left None here, same as the CLAUDE.md rule of
    never fabricating one from the sync moment.
    """
    wrapper: dict[str, Any] = raw_data
    if "comment" not in raw_data:
        wrapper = raw_data.get("item") or {}
    comment = wrapper.get("comment") or {}

    raw_id = comment.get("id")
    if raw_id is None:
        return None
    substack_note_id = str(raw_id)

    handle = comment.get("handle")
    url = (
        canonicalize_url(f"https://substack.com/@{handle}/note/c-{substack_note_id}")
        if handle
        else None
    )

    body_json = comment.get("body_json")
    body_text, body_format = note_body_to_text(body_json or comment.get("body") or "")
    body_raw = json.dumps(body_json) if body_json else comment.get("body")

    restacked_post = wrapper.get("post")
    restacked_pub = wrapper.get("publication")
    is_restack = 1 if (restacked_post or restacked_pub) else 0

    restacked_post_url = None
    restacked_post_title = None
    if restacked_post:
        raw_restack_url = restacked_post.get("canonical_url") or restacked_post.get(
            "url"
        )
        restacked_post_url = (
            canonicalize_url(raw_restack_url) if raw_restack_url else None
        )
        restacked_post_title = restacked_post.get("title")
    restacked_publication_name = (restacked_pub or {}).get("name")

    attachment_type = None
    attachment_url = None
    attachments = comment.get("attachments") or []
    if attachments:
        first = attachments[0]
        attachment_type = first.get("type")
        if attachment_type == "image":
            attachment_url = first.get("imageUrl")
        elif attachment_type == "link":
            attachment_url = (first.get("linkMetadata") or {}).get("url")

    ancestor_path = comment.get("ancestor_path") or ""
    word_count = len(body_text.split()) or None

    return SavedNote(
        substack_note_id=substack_note_id,
        url=url,
        body_text=body_text,
        body_raw=body_raw,
        body_format=body_format,
        author_name=comment.get("name"),
        author_handle=handle,
        author_id=str(comment.get("user_id")) if comment.get("user_id") else None,
        publication_name=restacked_publication_name,
        posted_at=comment.get("date"),
        saved_at=None,
        is_saved=1,
        is_restack=is_restack,
        parent_note_id=ancestor_path or None,
        attachment_type=attachment_type,
        attachment_url=attachment_url,
        restacked_post_url=restacked_post_url,
        restacked_post_title=restacked_post_title,
        restacked_publication_name=restacked_publication_name,
        like_count=_int_or_none(comment.get("reaction_count")),
        restack_count=_int_or_none(comment.get("restacks")),
        reply_count=_int_or_none(comment.get("children_count")),
        word_count=word_count,
    )


def sync_saved_notes(
    force: bool = False,
    db_path: Path | None = None,
    client: SubstackSavedPostsClient | None = None,
) -> SyncRun:
    """Execute an incremental or full sync of Substack saved notes into SQLite cache.

    A sibling of sync_saved_posts, not a generalization of it: notes reconcile
    by Substack note id (not URL), have no DOM fallback, and — since the notes
    endpoint never exposes a bookmark timestamp — the incremental early-stop
    check compares "already saved locally" rather than a saved_at match.
    """
    init_db(db_path)
    sync_mode = "full" if force else "incremental"
    started_at = datetime.now(UTC).isoformat()
    sync_id = start_sync_run(sync_mode=sync_mode, entity="note", db_path=db_path)

    active_client = client or SubstackSavedPostsClient()
    active_client.reset_cache()
    total_fetched = 0
    total_upserted = 0
    consecutive_matches = 0
    # Only populated meaningfully for a force/full sync, which enumerates every
    # currently-saved remote note; an incremental sync may stop early and its
    # partial list must never be used to infer removals.
    remote_note_ids: list[str] = []

    page_size = 50
    offset = 0

    try:
        while True:
            remote_items = active_client.fetch_saved_notes_page(
                limit=page_size, offset=offset
            )
            if not remote_items:
                break

            total_fetched += len(remote_items)

            for item in remote_items:
                parsed_note = parse_remote_note(item)
                if not parsed_note:
                    continue
                remote_note_ids.append(parsed_note.substack_note_id)

                if not force:
                    existing = get_note_by_substack_id(
                        parsed_note.substack_note_id, db_path=db_path
                    )
                    if existing and existing.is_saved == 1:
                        consecutive_matches += 1
                        if consecutive_matches >= MAX_CONSECUTIVE_MATCHES:
                            logger.info(
                                f"Incremental note sync: encountered {consecutive_matches} existing matches. Stopping early."
                            )
                            break
                    else:
                        consecutive_matches = 0

                upsert_note(parsed_note, db_path=db_path)
                total_upserted += 1

            if not force and consecutive_matches >= MAX_CONSECUTIVE_MATCHES:
                break

            if len(remote_items) < page_size:
                break

            offset += page_size

        # See the mirror comment in sync_saved_posts(): a persistent failure
        # mid-pagination can leave the fetcher's cached list truncated, and
        # that partial list must never drive reconciliation.
        fetch_truncated = active_client.is_notes_fetch_truncated()
        reconciled_count = 0
        if force:
            if fetch_truncated:
                logger.warning(
                    "Force sync's fetch was truncated by a persistent failure "
                    "mid-pagination; skipping reconciliation this run so notes "
                    "that merely couldn't be fetched aren't soft-deleted as if "
                    "they were unsaved remotely."
                )
            else:
                reconciled_count = reconcile_unsaved_notes(
                    remote_note_ids, db_path=db_path
                )
                if reconciled_count:
                    logger.info(
                        f"Reconciliation: soft-deleted {reconciled_count} note(s) no longer in the remote saved list."
                    )

        status = "partial" if fetch_truncated else "success"
        error_message = (
            "Fetch was truncated by a persistent failure mid-pagination "
            "(e.g. a 429 that survived every retry); some saved notes may be "
            "missing from this sync, and reconciliation was skipped if this "
            "was a --force sync."
            if fetch_truncated
            else None
        )
        finish_sync_run(
            sync_id=sync_id,
            status=status,
            fetched_count=total_fetched,
            upserted_count=total_upserted,
            reconciled_count=reconciled_count,
            error_message=error_message,
            db_path=db_path,
        )
        return _build_sync_run(
            sync_id,
            status,
            sync_mode,
            "note",
            started_at,
            total_fetched,
            total_upserted,
            reconciled_count=reconciled_count,
            error_message=error_message,
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
        return _build_sync_run(
            sync_id,
            "auth_required",
            sync_mode,
            "note",
            started_at,
            total_fetched,
            total_upserted,
            error_message=msg,
        )
    except Exception as e:
        msg = str(e)
        logger.exception(f"Sync failed: {msg}")
        finish_sync_run(
            sync_id=sync_id,
            status="failed",
            fetched_count=total_fetched,
            upserted_count=total_upserted,
            error_message=msg,
            db_path=db_path,
        )
        return _build_sync_run(
            sync_id,
            "failed",
            sync_mode,
            "note",
            started_at,
            total_fetched,
            total_upserted,
            error_message=msg,
        )
