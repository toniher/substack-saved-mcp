"""Command Line Interface (CLI) for Substack Saved Posts MCP & Sync tool."""

import itertools
import json
import logging
import re
import sys
import time
from datetime import UTC, datetime

import click

from substack_saved_mcp import __version__
from substack_saved_mcp.config import get_storage_state_path
from substack_saved_mcp.content_utils import (
    format_note_for_llm,
    format_post_for_llm,
    html_to_llm_text,
)
from substack_saved_mcp.database import (
    get_note,
    get_post,
    get_status,
    init_db,
    soft_delete_note,
    soft_delete_post,
    upsert_note,
    upsert_post,
)
from substack_saved_mcp.database import (
    list_audiences as db_list_audiences,
)
from substack_saved_mcp.database import (
    list_note_authors as db_list_note_authors,
)
from substack_saved_mcp.database import (
    list_notes as db_list_notes,
)
from substack_saved_mcp.database import (
    list_posts as db_list_posts,
)
from substack_saved_mcp.database import (
    list_publications as db_list_publications,
)
from substack_saved_mcp.database import (
    search_notes as db_search_notes,
)
from substack_saved_mcp.database import (
    search_posts as db_search_posts,
)
from substack_saved_mcp.mcp_server import run_server
from substack_saved_mcp.substack_client import (
    AuthRequiredError,
    SubstackSavedPostsClient,
    perform_interactive_login,
)
from substack_saved_mcp.sync import sync_saved_notes as run_sync_notes
from substack_saved_mcp.sync import sync_saved_posts as run_sync
from substack_saved_mcp.url_utils import canonicalize_url

logger = logging.getLogger(__name__)


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Substack Saved Posts MCP & Sync Engine."""
    pass


@cli.command()
def init() -> None:
    """Initialize local SQLite database schema and FTS5 search index."""
    init_db()
    status = get_status()
    click.echo(f"Initialized database at: {status.database_path}")
    click.echo(f"Active saved posts: {status.total_saved_posts}")


@cli.command()
def login() -> None:
    """Launch interactive browser window to log in to your Substack account."""
    try:
        state_file = perform_interactive_login()
        click.echo(f"Session saved to: {state_file}")
    except Exception as e:
        click.secho(f"Error during login: {e}", fg="red")
        sys.exit(1)


@cli.command()
@click.option(
    "--force", is_flag=True, help="Force full resync instead of incremental stop."
)
@click.option(
    "--only",
    type=click.Choice(["posts", "notes"]),
    help="Sync only one entity instead of both.",
)
def sync(force: bool, only: str | None) -> None:
    """Sync saved posts and notes from Substack account into local SQLite cache."""
    posts_ok = only == "notes"
    notes_ok = only == "posts"

    if only in (None, "posts"):
        click.echo("Starting Substack saved posts sync...")
        result = run_sync(force=force)
        if result.status in ("success", "partial"):
            posts_ok = True
            msg = f"Sync complete! Fetched {result.fetched_count} posts, upserted {result.upserted_count} posts."
            if result.reconciled_count:
                msg += f" Unsaved {result.reconciled_count} post(s) no longer on Substack's saved list."
            click.secho(msg, fg="green" if result.status == "success" else "yellow")
            if result.status == "partial":
                click.secho(f"Warning: {result.error_message}", fg="yellow")
        elif result.status == "auth_required":
            click.secho(f"Authentication required: {result.error_message}", fg="yellow")
        else:
            click.secho(f"Sync failed: {result.error_message}", fg="red")

    if only is None:
        # Fetching the full saved-posts list can itself trigger rate limiting
        # that would otherwise make the very next request (the notes fetch)
        # 429 through all its retries too - see the identical rationale on
        # compare_saved_apis(). A brief pause here lets that limit cool down
        # before the notes sync starts hammering the same reader API.
        time.sleep(5)

    if only in (None, "notes"):
        click.echo("Starting Substack saved notes sync...")
        note_result = run_sync_notes(force=force)
        if note_result.status in ("success", "partial"):
            notes_ok = True
            msg = f"Sync complete! Fetched {note_result.fetched_count} notes, upserted {note_result.upserted_count} notes."
            if note_result.reconciled_count:
                msg += f" Unsaved {note_result.reconciled_count} note(s) no longer on Substack's saved list."
            click.secho(
                msg, fg="green" if note_result.status == "success" else "yellow"
            )
            if note_result.status == "partial":
                click.secho(f"Warning: {note_result.error_message}", fg="yellow")
        elif note_result.status == "auth_required":
            click.secho(
                f"Authentication required: {note_result.error_message}", fg="yellow"
            )
        else:
            click.secho(f"Sync failed: {note_result.error_message}", fg="red")

    if not (posts_ok or notes_ok):
        sys.exit(1)


@cli.command()
def serve() -> None:
    """Run FastMCP stdio server for desktop clients (Claude Desktop, Goose, etc.)."""
    init_db()
    run_server()


@cli.command()
@click.argument("url")
def save(url: str) -> None:
    """Save/bookmark a Substack post by URL."""
    init_db()
    click.echo(f"Saving post: {url}...")
    client = SubstackSavedPostsClient()
    try:
        post, confirmation = client.save_post(url)
        saved_db_post = upsert_post(post)
        click.secho(
            f"Successfully saved '{saved_db_post.title}' to local cache!", fg="green"
        )
        click.echo(f"Published at: {saved_db_post.published_at or 'N/A'}")
        click.echo(f"Saved at: {saved_db_post.saved_at}")
        if confirmation != "confirmed":
            click.secho(
                f"Warning: could not confirm the bookmark was saved on Substack's own page "
                f"(status: {confirmation}). Cached locally regardless; a future 'sync --force' "
                "will correct it if the remote save didn't actually happen.",
                fg="yellow",
            )
    except AuthRequiredError as e:
        click.secho(f"Authentication required: {e}", fg="yellow")
    except Exception as e:
        click.secho(f"Error saving post: {e}", fg="red")


@cli.command()
@click.argument("url_or_id")
def unsave(url_or_id: str) -> None:
    """Unsave/unbookmark a Substack post by URL or local ID."""
    init_db()
    post = get_post(url_or_id)
    if not post:
        click.secho(f"Post '{url_or_id}' not found in local cache.", fg="yellow")
        return

    click.echo(f"Unsaving post '{post.title}'...")
    client = SubstackSavedPostsClient()
    confirmation = "click_failed"
    try:
        post_id = int(post.substack_post_id) if post.substack_post_id else None
        confirmation = client.unsave_post(post.url, post_id=post_id)
    except Exception as e:
        click.echo(f"Remote unsave notice: {e}")

    updated = soft_delete_post(post.url)
    if updated:
        click.secho(
            f"Successfully unsaved '{post.title}' from local cache.", fg="green"
        )
        if confirmation != "confirmed":
            click.secho(
                f"Warning: could not confirm the bookmark was removed on Substack's own page (status: {confirmation}).",
                fg="yellow",
            )


@cli.command(name="save-note")
@click.argument("url")
def save_note(url: str) -> None:
    """Save/bookmark a Substack note by URL."""
    init_db()
    click.echo(f"Saving note: {url}...")
    client = SubstackSavedPostsClient()
    try:
        note, confirmation = client.save_note(url)
        saved_db_note = upsert_note(note)
        click.secho(
            f"Successfully saved note by @{saved_db_note.author_handle} to local cache!",
            fg="green",
        )
        click.echo(f"Posted at: {saved_db_note.posted_at or 'N/A'}")
        if confirmation != "confirmed":
            click.secho(
                f"Warning: could not confirm the bookmark was saved on Substack's own page "
                f"(status: {confirmation}). Cached locally regardless; a future 'sync --force' "
                "will correct it if the remote save didn't actually happen.",
                fg="yellow",
            )
    except AuthRequiredError as e:
        click.secho(f"Authentication required: {e}", fg="yellow")
    except Exception as e:
        click.secho(f"Error saving note: {e}", fg="red")


@cli.command(name="unsave-note")
@click.argument("url_or_id")
def unsave_note(url_or_id: str) -> None:
    """Unsave/unbookmark a Substack note by URL or local ID."""
    init_db()
    note = get_note(url_or_id)
    if not note:
        click.secho(f"Note '{url_or_id}' not found in local cache.", fg="yellow")
        return

    click.echo(f"Unsaving note by @{note.author_handle}...")
    client = SubstackSavedPostsClient()
    confirmation = "unconfirmed"
    try:
        confirmation = client.unsave_note(note.url or "", note_id=note.substack_note_id)
    except Exception as e:
        click.echo(f"Remote unsave notice: {e}")

    updated = soft_delete_note(note.id) if note.id is not None else None
    if updated:
        click.secho(
            f"Successfully unsaved note by @{note.author_handle} from local cache.",
            fg="green",
        )
        if confirmation != "confirmed":
            click.secho(
                f"Warning: could not confirm the bookmark was removed on Substack's own page (status: {confirmation}).",
                fg="yellow",
            )


@cli.command()
@click.argument("query")
@click.option("--publication", help="Filter by publication name.")
@click.option(
    "--audience",
    help="Filter by audience tier (e.g. everyone, only_paid). See 'audiences' command for cached values.",
)
@click.option(
    "--published-after",
    help="Only posts published on/after this ISO-8601 date (e.g. 2026-01-01).",
)
@click.option(
    "--published-before", help="Only posts published on/before this ISO-8601 date."
)
@click.option(
    "--saved-after", help="Only posts bookmarked on/after this ISO-8601 date."
)
@click.option(
    "--saved-before", help="Only posts bookmarked on/before this ISO-8601 date."
)
@click.option(
    "--read-state",
    type=click.Choice(["unread", "in_progress", "finished", "started"]),
    help="Filter by reading progress.",
)
@click.option("--limit", default=10, help="Maximum search results.")
def search(
    query: str,
    publication: str | None,
    audience: str | None,
    published_after: str | None,
    published_before: str | None,
    saved_after: str | None,
    saved_before: str | None,
    read_state: str | None,
    limit: int,
) -> None:
    """Perform full-text search across cached saved posts."""
    init_db()
    results = db_search_posts(
        query=query,
        publication=publication,
        audience=audience,
        published_after=published_after,
        published_before=published_before,
        saved_after=saved_after,
        saved_before=saved_before,
        read_state=read_state,
        limit=limit,
    )
    if not results:
        click.echo(f"No saved posts matched query '{query}'.")
        return

    click.echo(f"Found {len(results)} matching post(s):\n")
    for idx, p in enumerate(results, 1):
        click.secho(f"{idx}. {p.title}", fg="cyan", bold=True)
        click.echo(f"   Publication : {p.publication_name}")
        click.echo(
            f"   Published   : {p.published_at or 'N/A'} | Saved: {p.saved_at or 'N/A'}"
        )
        click.echo(f"   Audience    : {p.audience or 'N/A'}")
        if p.reading_time_minutes or p.word_count:
            click.echo(
                f"   Reading time: {p.reading_time_minutes or '?'} min ({p.word_count or '?'} words)"
            )
        if p.max_read_progress is not None or p.is_viewed:
            progress_pct = round((p.max_read_progress or 0.0) * 100)
            status_word = "finished" if p.is_fully_read else "in progress"
            remaining = (
                f", {p.minutes_remaining} min left"
                if p.minutes_remaining is not None and not p.is_fully_read
                else ""
            )
            click.echo(f"   Progress    : {progress_pct}% ({status_word}{remaining})")
        click.echo(f"   URL         : {p.url}")
        if p.excerpt:
            click.echo(f"   Excerpt     : {p.excerpt[:120]}...")
        if p.image_url:
            click.echo(f"   Image       : {p.image_url}")
        click.echo("")


@cli.command(name="list")
@click.option("--limit", default=10, help="Number of posts to display.")
@click.option("--offset", default=0, help="Pagination offset.")
@click.option("--publication", help="Filter by publication name.")
@click.option(
    "--audience",
    help="Filter by audience tier (e.g. everyone, only_paid). See 'audiences' command for cached values.",
)
@click.option(
    "--read-state",
    type=click.Choice(["unread", "in_progress", "finished", "started"]),
    help="Filter by reading progress.",
)
@click.option(
    "--sort-by",
    type=click.Choice(
        ["saved_at", "published_at", "read_progress", "minutes_remaining"]
    ),
    default="saved_at",
)
def list_cmd(
    limit: int,
    offset: int,
    publication: str | None,
    audience: str | None,
    read_state: str | None,
    sort_by: str,
) -> None:
    """List saved posts ordered by saved date or publication date."""
    init_db()
    posts = db_list_posts(
        limit=limit,
        offset=offset,
        publication=publication,
        audience=audience,
        read_state=read_state,
        sort_by=sort_by,
    )
    if not posts:
        click.echo("No saved posts found.")
        return

    click.echo(f"Saved Posts ({len(posts)} displayed):\n")
    for idx, p in enumerate(posts, offset + 1):
        reading = (
            f" | {p.reading_time_minutes} min ({p.word_count} words)"
            if p.reading_time_minutes
            else ""
        )
        if p.max_read_progress is not None or p.is_viewed:
            progress_pct = round((p.max_read_progress or 0.0) * 100)
            status_word = "finished" if p.is_fully_read else "in progress"
            reading += f" | {progress_pct}% ({status_word})"
        click.secho(f"{idx}. {p.title}", fg="cyan")
        click.echo(
            f"   Pub: {p.publication_name} | Saved: {p.saved_at or 'N/A'} | Published: {p.published_at or 'N/A'} | Audience: {p.audience or 'N/A'}{reading}"
        )
        click.echo(f"   URL: {p.url}\n")


@cli.command()
def publications() -> None:
    """List all publications present in the cache."""
    init_db()
    pubs = db_list_publications()
    if not pubs:
        click.echo("No publications in cache.")
        return

    click.echo(f"Cached Publications ({len(pubs)} total):\n")
    for p in pubs:
        click.echo(
            f"- {p.publication_name} ({p.post_count} saved post{'s' if p.post_count != 1 else ''})"
        )


@cli.command()
def audiences() -> None:
    """List all audience tiers present in the cache (e.g. everyone, only_paid)."""
    init_db()
    tiers = db_list_audiences()
    if not tiers:
        click.echo("No posts in cache.")
        return

    click.echo(f"Cached Audience Tiers ({len(tiers)} total):\n")
    for t in tiers:
        click.echo(
            f"- {t.audience or 'unknown'} ({t.post_count} saved post{'s' if t.post_count != 1 else ''})"
        )


@cli.command(name="search-notes")
@click.argument("query")
@click.option("--author", help="Filter by author name or handle.")
@click.option("--posted-after", help="Only notes posted on/after this ISO-8601 date.")
@click.option("--posted-before", help="Only notes posted on/before this ISO-8601 date.")
@click.option(
    "--saved-after", help="Only notes bookmarked on/after this ISO-8601 date."
)
@click.option(
    "--saved-before", help="Only notes bookmarked on/before this ISO-8601 date."
)
@click.option("--limit", default=10, help="Maximum search results.")
def search_notes(
    query: str,
    author: str | None,
    posted_after: str | None,
    posted_before: str | None,
    saved_after: str | None,
    saved_before: str | None,
    limit: int,
) -> None:
    """Perform full-text search across cached saved notes."""
    init_db()
    results = db_search_notes(
        query=query,
        author=author,
        posted_after=posted_after,
        posted_before=posted_before,
        saved_after=saved_after,
        saved_before=saved_before,
        limit=limit,
    )
    if not results:
        click.echo(f"No saved notes matched query '{query}'.")
        return

    click.echo(f"Found {len(results)} matching note(s):\n")
    for idx, n in enumerate(results, 1):
        click.secho(f"{idx}. @{n.author_handle or 'unknown'}", fg="cyan", bold=True)
        click.echo(f"   Author  : {n.author_name or 'N/A'}")
        click.echo(f"   Posted  : {n.posted_at or 'N/A'}")
        if n.is_restack and n.restacked_post_title:
            click.echo(f"   Restack : {n.restacked_post_title}")
        click.echo(f"   Body    : {n.body_preview[:120]}...")
        if n.url:
            click.echo(f"   URL     : {n.url}")
        click.echo("")


@cli.command(name="list-notes")
@click.option("--limit", default=10, help="Number of notes to display.")
@click.option("--offset", default=0, help="Pagination offset.")
@click.option("--author", help="Filter by author name or handle.")
@click.option("--restacks-only", is_flag=True, help="Show only restacked notes.")
@click.option(
    "--sort-by", type=click.Choice(["saved_at", "posted_at"]), default="saved_at"
)
def list_notes(
    limit: int, offset: int, author: str | None, restacks_only: bool, sort_by: str
) -> None:
    """List saved notes ordered by saved date or posted date."""
    init_db()
    notes = db_list_notes(
        limit=limit,
        offset=offset,
        author=author,
        is_restack=True if restacks_only else None,
        sort_by=sort_by,
    )
    if not notes:
        click.echo("No saved notes found.")
        return

    click.echo(f"Saved Notes ({len(notes)} displayed):\n")
    for idx, n in enumerate(notes, offset + 1):
        restack = f" | Restack: {n.restacked_post_title}" if n.is_restack else ""
        click.secho(f"{idx}. @{n.author_handle or 'unknown'}", fg="cyan")
        click.echo(
            f"   Posted: {n.posted_at or 'N/A'} | Saved: {n.saved_at or 'N/A'}{restack}"
        )
        click.echo(f"   {n.body_preview[:120]}")
        if n.url:
            click.echo(f"   URL: {n.url}")
        click.echo("")


@cli.command(name="note-authors")
def note_authors() -> None:
    """List all note authors present in the cache."""
    init_db()
    authors = db_list_note_authors()
    if not authors:
        click.echo("No notes in cache.")
        return

    click.echo(f"Cached Note Authors ({len(authors)} total):\n")
    for a in authors:
        click.echo(
            f"- @{a.author_handle or 'unknown'} ({a.author_name or 'N/A'}): "
            f"{a.note_count} saved note{'s' if a.note_count != 1 else ''}"
        )


@cli.command()
def status() -> None:
    """Show cache statistics and last sync run info."""
    init_db()
    st = get_status()
    click.echo(f"Database Path       : {st.database_path}")
    click.echo(f"Active Saved Posts  : {st.total_saved_posts}")
    click.echo(f"Unsaved Posts       : {st.total_unsaved_posts}")
    click.echo(f"Total Publications  : {st.total_publications}")
    click.echo(f"Last Successful Sync: {st.last_successful_sync or 'Never'}")
    click.echo(f"Last Sync Status    : {st.last_sync_status or 'N/A'}")
    click.echo(f"Active Saved Notes  : {st.total_saved_notes}")
    click.echo(f"Unsaved Notes       : {st.total_unsaved_notes}")
    click.echo(f"Last Successful Note Sync: {st.last_successful_note_sync or 'Never'}")
    click.echo(f"Last Note Sync Status    : {st.last_note_sync_status or 'N/A'}")
    click.echo(f"Posts Unread        : {st.posts_unread}")
    click.echo(f"Posts In Progress   : {st.posts_in_progress}")
    click.echo(f"Posts Fully Read    : {st.posts_fully_read}")
    click.echo(f"Minutes Remaining   : {st.minutes_remaining_total}")


@cli.command(name="get-content")
@click.argument("url_or_id")
@click.option(
    "--no-cache",
    is_flag=True,
    help="Don't store the fetched content in the local cache.",
)
def get_content(url_or_id: str, no_cache: bool) -> None:
    """Fetch a saved post's full content and print it formatted for an LLM.

    Uses the cached content_text if a previous fetch already stored it;
    otherwise fetches the post's page and caches the result unless --no-cache
    is given.
    """
    init_db()
    post = get_post(url_or_id)
    if not post:
        click.secho(f"Post '{url_or_id}' not found in local cache.", fg="yellow")
        return

    if post.content_text:
        click.echo(
            format_post_for_llm(
                title=post.title,
                publication_name=post.publication_name,
                url=post.url,
                body_text=post.content_text,
                author_name=post.author_name,
                published_at=post.published_at,
            )
        )
        return

    click.echo(f"Fetching full content for '{post.title}'...", err=True)
    client = SubstackSavedPostsClient()
    try:
        result = client.fetch_post_content(post.url)
    except AuthRequiredError as e:
        click.secho(f"Authentication required: {e}", fg="yellow")
        return
    except Exception as e:
        click.secho(f"Error fetching content: {e}", fg="red")
        return

    body_html = result.get("body_html")
    if not body_html:
        click.secho(
            "Could not find this post's full content on its page (Substack may have "
            "changed how it embeds it, or this post is paywalled beyond your account's "
            "access). Run 'substack-saved-mcp inspect-network' while opening this "
            f"post ({post.url}) in the browser so we can capture the real content "
            "source, then this command can be updated.",
            fg="yellow",
        )
        return

    body_text = html_to_llm_text(body_html)
    if not no_cache:
        post.content_text = body_text
        post = upsert_post(post)

    click.echo(
        format_post_for_llm(
            title=post.title,
            publication_name=post.publication_name,
            url=post.url,
            body_text=body_text,
            author_name=post.author_name,
            published_at=post.published_at,
        )
    )


@cli.command(name="get-note")
@click.argument("url_or_id")
@click.option(
    "--no-cache",
    is_flag=True,
    help="Don't store the fetched content in the local cache.",
)
def get_note_content(url_or_id: str, no_cache: bool) -> None:
    """Fetch a saved note's full content and print it formatted for an LLM.

    Uses the cached body_text if a previous fetch already populated it;
    otherwise fetches the note directly via Substack's reader API and caches
    the result unless --no-cache is given.
    """
    init_db()
    note = get_note(url_or_id)
    if not note:
        click.secho(f"Note '{url_or_id}' not found in local cache.", fg="yellow")
        return

    if note.body_text:
        click.echo(
            format_note_for_llm(
                author_name=note.author_name,
                author_handle=note.author_handle,
                url=note.url,
                body_text=note.body_text,
                posted_at=note.posted_at,
                restacked_post_title=note.restacked_post_title,
                restacked_post_url=note.restacked_post_url,
            )
        )
        return

    if not note.url:
        click.secho(
            f"Note '{url_or_id}' has no known permalink; cannot fetch its content.",
            fg="yellow",
        )
        return

    click.echo(f"Fetching full content for note by @{note.author_handle}...", err=True)
    client = SubstackSavedPostsClient()
    try:
        result = client.fetch_note_content(note.url)
    except AuthRequiredError as e:
        click.secho(f"Authentication required: {e}", fg="yellow")
        return
    except Exception as e:
        click.secho(f"Error fetching content: {e}", fg="red")
        return

    body_text = result.get("body_text")
    if not body_text:
        click.secho(
            "Could not find this note's content via Substack's reader API. Run "
            "'substack-saved-mcp inspect-network' while opening this note "
            f"({note.url}) in the browser so we can capture the real content "
            "source, then this command can be updated.",
            fg="yellow",
        )
        return

    if not no_cache:
        note.body_text = body_text
        note.body_raw = result.get("body_raw")
        note.body_format = result.get("body_format")
        note = upsert_note(note)

    click.echo(
        format_note_for_llm(
            author_name=note.author_name,
            author_handle=note.author_handle,
            url=note.url,
            body_text=body_text,
            posted_at=note.posted_at,
            restacked_post_title=note.restacked_post_title,
            restacked_post_url=note.restacked_post_url,
        )
    )


_INSPECT_URL_PATTERN = (
    r"api/v1|api/v2|bookmark|saved|notes?|comment|reader|feed|restack"
)
_INSPECT_ASSET_SUFFIXES = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".woff",
    ".woff2",
    ".ico",
)


@cli.command(name="inspect-network")
@click.option(
    "--authenticated/--anonymous",
    default=True,
    help=(
        "Reuse the saved login session (storage_state.json) so the target page "
        "renders as your logged-in account. Use --anonymous to browse without it."
    ),
)
@click.option(
    "--url",
    default="https://substack.com/saved",
    help="Page to open in the inspector (e.g. an individual note/post URL).",
)
@click.option(
    "--filter",
    "url_filter",
    default=_INSPECT_URL_PATTERN,
    help="Regex matched (case-insensitively) against response URLs to decide what to log.",
)
@click.option(
    "--max-body",
    default=4000,
    help="Max characters of each JSON response body to print (0 disables body logging).",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False),
    help="Append each intercepted exchange as a JSON line to this file.",
)
def inspect_network(
    authenticated: bool, url: str, url_filter: str, max_body: int, out_path: str | None
) -> None:
    """Inspect and capture Substack network traffic (posts, notes, bookmarks) safely."""
    click.echo("Launching Playwright inspector context...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        click.secho("Playwright not installed.", fg="red")
        return

    storage_state = None
    if authenticated:
        state_path = get_storage_state_path()
        if state_path.exists():
            storage_state = str(state_path)
        else:
            click.secho(
                f"No saved session found at {state_path}; continuing anonymously. "
                "Run 'substack-saved-mcp login' first to capture authenticated traffic.",
                fg="yellow",
            )

    pattern = re.compile(url_filter, re.IGNORECASE)
    out_file = open(out_path, "a") if out_path else None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()

        def handle_route(route):
            request = route.request
            req_url = request.url
            if req_url.lower().split("?")[0].endswith(
                _INSPECT_ASSET_SUFFIXES
            ) or not pattern.search(req_url):
                route.continue_()
                return

            # A sync-API page.on("response") handler can deadlock calling
            # response.text() on the same driver thread that produced it. Routing
            # the request through route.fetch() reads the body safely outside that
            # handler, and route.fulfill() re-serves the exact response to the page
            # so navigation/rendering behaves identically to an unrouted request.
            try:
                response = route.fetch()
            except Exception as e:
                logger.warning(f"Could not fetch intercepted request {req_url}: {e}")
                route.continue_()
                return

            request_body = request.post_data
            content_type = response.headers.get("content-type") or ""
            response_body = None
            body_read_failed = False
            if max_body and "json" in content_type:
                try:
                    response_body = response.text()[:max_body]
                except Exception as e:
                    body_read_failed = True
                    logger.warning(f"Could not read response body for {req_url}: {e}")

            click.echo(
                f"[Network Intercept] {request.method} {req_url} "
                f"(Status: {response.status})"
            )
            if request_body:
                click.echo(f"    Request Body : {request_body}")
            if response_body:
                click.echo(f"    Response Body: {response_body}")
            elif body_read_failed:
                click.secho("    Response Body: <could not be read>", fg="yellow")

            if out_file:
                out_file.write(
                    json.dumps(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "method": request.method,
                            "url": req_url,
                            "status": response.status,
                            "request_body": request_body,
                            "response_body": response_body,
                        }
                    )
                    + "\n"
                )
                out_file.flush()

            route.fulfill(response=response)

        context.route("**/*", handle_route)
        page.goto(url)
        click.echo(
            "Navigate around the page (try the Notes toggle, save/unsave, open an "
            "individual post/note). Press ENTER in terminal when finished."
        )
        input("--> Press ENTER to finish network inspection: ")
        browser.close()

    if out_file:
        out_file.close()
        click.echo(f"Capture written to {out_path}")


@cli.command(name="probe-api")
@click.argument("url")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False),
    help="Write the raw JSON response to this file instead of stdout.",
)
def probe_api(url: str, out_path: str | None) -> None:
    """Headlessly GET a Substack API URL with the saved session (no browser page).

    Complements 'inspect-network' (which discovers unknown endpoints by watching
    a live browsing session): use this to re-verify a URL already known, or to
    inspect a specific reader-API response while assessing a migration.
    """
    client = SubstackSavedPostsClient()
    try:
        data = client.probe_api_get(url)
    except AuthRequiredError as e:
        click.secho(f"Authentication required: {e}", fg="yellow")
        return
    except Exception as e:
        click.secho(f"Error probing {url}: {e}", fg="red")
        return

    text = json.dumps(data, indent=2)
    if out_path:
        with open(out_path, "w") as f:
            f.write(text)
        click.echo(f"Response written to {out_path}")
    else:
        click.echo(text)


# Fields parse_remote_post() reads off a unified-endpoint post object, used by
# compare-saved-apis to report per-field presence without printing post content.
_UNIFIED_POST_FIELDS = (
    "id",
    "canonical_url",
    "title",
    "post_date",
    "saved_at",
    "audience",
    "description",
    "subtitle",
    "cover_image",
    "wordcount",
    "word_count",
    "words",
    "read_progress",
    "max_read_progress",
    "is_viewed",
)


@cli.command(name="compare-saved-apis")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False),
    help=(
        "Write the full report (including mismatched canonical URLs) as JSON to "
        "this file. Without it, only aggregate counts/percentages are printed — "
        "no post URLs or content."
    ),
)
def compare_saved_apis(out_path: str | None) -> None:
    """Fetch the full saved-posts list from both the legacy and unified reader
    APIs and report parity: counts, set differences, per-field presence on the
    unified payload, and bookmark-timestamp/ordering — the evidence needed to
    decide whether posts can migrate off the legacy reader-posts API.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        click.secho("Playwright not installed.", fg="red")
        return

    client = SubstackSavedPostsClient()
    try:
        client._ensure_authenticated()
    except AuthRequiredError as e:
        click.secho(f"Authentication required: {e}", fg="yellow")
        return

    with sync_playwright() as p:
        api_context = p.request.new_context(storage_state=str(client.state_path))
        try:
            click.echo("Fetching via legacy reader-posts API...")
            legacy_posts = client._fetch_all_saved_via_reader_api(api_context) or []

            # This tool runs once, interactively, rather than in a background
            # sync loop, so it can afford to be far more patient than the
            # default sync retry policy: pause briefly before the second full
            # paginated fetch (fetching legacy's full list can itself trigger
            # rate limiting that would otherwise make the very next request
            # 429 through all retries and be misread as "unified has no
            # posts"), and retry the unified fetch harder (max_retries=6,
            # exponential backoff up to ~30s per attempt) before giving up.
            click.echo("Pausing briefly before the unified reader/saved API...")
            time.sleep(5)
            click.echo("Fetching via unified reader/saved API...")
            unified_items = (
                client._fetch_all_saved_posts_via_unified_api(
                    api_context, max_retries=6
                )
                or []
            )
        except AuthRequiredError as e:
            click.secho(f"Authentication required: {e}", fg="yellow")
            return

    legacy_urls = {
        canonicalize_url(item.get("canonical_url") or "") for item in legacy_posts
    }
    legacy_urls.discard("")
    unified_posts = [item.get("post") or {} for item in unified_items]
    unified_urls = {
        canonicalize_url(post.get("canonical_url") or post.get("url") or "")
        for post in unified_posts
    }
    unified_urls.discard("")

    missing_from_unified = legacy_urls - unified_urls
    missing_from_legacy = unified_urls - legacy_urls

    n_unified = len(unified_posts) or 1
    field_presence = {
        field: sum(1 for post in unified_posts if post.get(field))
        for field in _UNIFIED_POST_FIELDS
    }

    saved_ats = [post.get("saved_at") for post in unified_posts if post.get("saved_at")]
    ordered = all(a >= b for a, b in itertools.pairwise(saved_ats))

    if legacy_posts and not unified_items:
        click.echo("")
        click.secho(
            "Unified API returned 0 posts while legacy returned "
            f"{len(legacy_posts)} — before reading this as 'the endpoint lacks "
            "posts data', re-run compare-saved-apis on its own (not right "
            "after another full sync/fetch): a 429 that survives every retry "
            "reads identically to a genuinely empty/unavailable endpoint, and "
            "the large legacy fetch just before it can trigger exactly that.",
            fg="yellow",
        )

    click.echo("")
    click.echo(f"Legacy reader-posts API : {len(legacy_posts)} posts")
    click.echo(f"Unified reader/saved API: {len(unified_items)} posts")
    click.echo(f"In both                : {len(legacy_urls & unified_urls)}")
    click.echo(f"Only in legacy          : {len(missing_from_unified)}")
    click.echo(f"Only in unified         : {len(missing_from_legacy)}")
    click.echo("")
    click.echo("Unified payload field presence:")
    for field in _UNIFIED_POST_FIELDS:
        pct = 100 * field_presence[field] / n_unified
        click.echo(
            f"  {field:<16} {field_presence[field]:>5}/{len(unified_posts):<5} ({pct:5.1f}%)"
        )
    click.echo("")
    if saved_ats:
        click.echo(
            f"post.saved_at present on {len(saved_ats)}/{len(unified_posts)} items; "
            f"{'monotonically newest-first' if ordered else 'NOT monotonically ordered'}."
        )
    else:
        click.secho(
            "No post.saved_at found anywhere in the unified payload — a real "
            "bookmark timestamp for posts may be absent from this endpoint.",
            fg="yellow",
        )

    if out_path:
        report = {
            "legacy_count": len(legacy_posts),
            "unified_count": len(unified_items),
            "overlap_count": len(legacy_urls & unified_urls),
            "only_in_legacy": sorted(missing_from_unified),
            "only_in_unified": sorted(missing_from_legacy),
            "unified_field_presence": field_presence,
            "unified_field_presence_denominator": len(unified_posts),
            "saved_at_present_count": len(saved_ats),
            "saved_at_ordered_newest_first": ordered if saved_ats else None,
        }
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)
        click.echo(f"\nFull report (including mismatched URLs) written to {out_path}")


if __name__ == "__main__":
    cli()
