"""FastMCP server exposing tools and resources for searching, retrieving, saving, and unsaving Substack posts."""

import json
from typing import Any

from fastmcp import FastMCP

from substack_saved_mcp.content_utils import format_post_for_llm, html_to_llm_text
from substack_saved_mcp.database import (
    get_post,
    get_status,
    init_db,
    list_posts,
    search_posts,
    soft_delete_post,
    upsert_post,
)
from substack_saved_mcp.database import (
    list_audiences as db_list_audiences,
)
from substack_saved_mcp.database import (
    list_publications as db_list_publications,
)
from substack_saved_mcp.models import (
    AudienceSummary,
    PostSummary,
    PublicationSummary,
    SavedPost,
    SavedPostsStatus,
    SyncRun,
)
from substack_saved_mcp.substack_client import (
    AuthRequiredError,
    SubstackSavedPostsClient,
)
from substack_saved_mcp.sync import sync_saved_posts as run_sync

# Initialize FastMCP Server
mcp = FastMCP("Substack Saved Posts")


@mcp.tool()
def search_saved_posts(
    query: str,
    publication: str | None = None,
    audience: str | None = None,
    published_after: str | None = None,
    published_before: str | None = None,
    saved_after: str | None = None,
    saved_before: str | None = None,
    limit: int = 20,
) -> list[PostSummary]:
    """Perform full-text FTS5 search across cached saved posts.

    Searches title, excerpt, publication name, author, and content text.
    Allows filtering by publication name, audience tier (see list_audiences for
    cached values, e.g. "everyone", "only_paid"), original post date
    (published_at), and saved date (saved_at).
    """
    init_db()
    return search_posts(
        query=query,
        publication=publication,
        audience=audience,
        published_after=published_after,
        published_before=published_before,
        saved_after=saved_after,
        saved_before=saved_before,
        limit=limit,
    )


@mcp.tool()
def list_saved_posts(
    limit: int = 20,
    offset: int = 0,
    publication: str | None = None,
    audience: str | None = None,
    sort_by: str = "saved_at",
) -> list[PostSummary]:
    """List cached saved posts with pagination and optional publication/audience filters.

    sort_by can be 'saved_at' (when post was bookmarked) or 'published_at' (when post was published).
    audience filters by tier (see list_audiences for cached values, e.g. "everyone", "only_paid").
    """
    init_db()
    return list_posts(
        limit=limit,
        offset=offset,
        publication=publication,
        audience=audience,
        sort_by=sort_by,
        is_saved_only=True,
    )


@mcp.tool()
def get_saved_post(url_or_id: str) -> SavedPost | None:
    """Retrieve full cached post details, timestamps (published_at and saved_at), and content by URL or local ID."""
    init_db()
    return get_post(url_or_id)


@mcp.tool()
def save_post(url: str) -> dict[str, Any]:
    """Bookmark a Substack post remotely on Substack and save it to the local cache.

    Requires an active authenticated Substack session (run 'substack-saved-mcp login' if expired).
    Remote confirmation is best-effort: Substack's bookmark button markup isn't
    officially documented, so this detects whether the button's rendered state
    provably changed after clicking. remote_confirmed=False means the post is
    still cached locally, but the tool could not verify the bookmark was
    actually created on Substack's side — a subsequent 'sync --force' will
    correct the local cache if the remote save didn't actually happen.
    """
    init_db()
    client = SubstackSavedPostsClient()
    saved_model, confirmation = client.save_post(url)
    updated_db_post = upsert_post(saved_model)
    result: dict[str, Any] = {
        "success": True,
        "post": updated_db_post,
        "remote_confirmed": confirmation == "confirmed",
    }
    if confirmation != "confirmed":
        result["warning"] = (
            f"Could not confirm the bookmark toggle on Substack's page (status: {confirmation})."
        )
    return result


@mcp.tool()
def unsave_post(url_or_id: str) -> dict[str, Any]:
    """Unbookmark a Substack post remotely and soft-delete it in local cache.

    Soft-deletion preserves post history while removing it from active search/list outputs.
    When the post's Substack ID is known (normally true after a sync), this calls
    Substack's real unsave endpoint directly and is reliably confirmed; otherwise
    it falls back to a best-effort DOM click (see save_post). remote_confirmed=False
    means the post was still soft-deleted locally, but the tool could not
    verify the unbookmark on Substack's side.
    """
    init_db()
    post = get_post(url_or_id)
    if not post:
        return {
            "success": False,
            "message": f"Post '{url_or_id}' not found in local cache.",
        }

    client = SubstackSavedPostsClient()
    confirmation = "click_failed"
    try:
        post_id = int(post.substack_post_id) if post.substack_post_id else None
        confirmation = client.unsave_post(post.url, post_id=post_id)
    except AuthRequiredError as e:
        return {"success": False, "message": str(e)}
    except Exception:
        confirmation = (
            "click_failed"  # Continue soft deletion locally even if remote unsave fails
        )

    updated_post = soft_delete_post(post.url)
    message = f"Successfully unsaved post '{post.title}' locally."
    if confirmation != "confirmed":
        message += f" Warning: could not confirm the removal on Substack's page (status: {confirmation})."
    return {
        "success": True,
        "message": message,
        "post": updated_post,
        "remote_confirmed": confirmation == "confirmed",
    }


@mcp.tool()
def get_post_content(url_or_id: str, force_refetch: bool = False) -> dict[str, Any]:
    """Fetch a saved post's full content, cleaned and formatted for LLM consumption.

    Returns the cached content_text if a previous fetch already stored it, unless
    force_refetch is set. Otherwise fetches the post's page directly, extracts its
    body_html from Substack's server-rendered window._preloads blob, converts it
    to plain text (headings, list items, and links kept readable), and caches the
    result. Requires an active authenticated Substack session. If the content
    can't be located on the page (e.g. Substack changed how it embeds it, or the
    post is paywalled beyond this account's access), returns success=False with a
    message suggesting the caller run 'substack-saved-mcp inspect-network' while
    opening the post so the real content source can be captured.
    """
    init_db()
    post = get_post(url_or_id)
    if not post:
        return {
            "success": False,
            "message": f"Post '{url_or_id}' not found in local cache.",
        }

    if post.content_text and not force_refetch:
        return {
            "success": True,
            "post": post,
            "content": format_post_for_llm(
                title=post.title,
                publication_name=post.publication_name,
                url=post.url,
                body_text=post.content_text,
                author_name=post.author_name,
                published_at=post.published_at,
            ),
            "cached": True,
        }

    client = SubstackSavedPostsClient()
    try:
        result = client.fetch_post_content(post.url)
    except AuthRequiredError as e:
        return {"success": False, "message": str(e)}

    body_html = result.get("body_html")
    if not body_html:
        return {
            "success": False,
            "post": post,
            "message": (
                "Could not find this post's full content on its page. Substack may "
                "have changed how it embeds it, or this post is paywalled beyond "
                f"this account's access. Run 'substack-saved-mcp inspect-network' "
                f"while opening {post.url} in the browser so the real content "
                "source can be captured, then this tool can be updated."
            ),
        }

    body_text = html_to_llm_text(body_html)
    post.content_text = body_text
    updated_post = upsert_post(post)

    return {
        "success": True,
        "post": updated_post,
        "content": format_post_for_llm(
            title=updated_post.title,
            publication_name=updated_post.publication_name,
            url=updated_post.url,
            body_text=body_text,
            author_name=updated_post.author_name,
            published_at=updated_post.published_at,
        ),
        "cached": False,
    }


@mcp.tool()
def list_publications() -> list[PublicationSummary]:
    """List all publications in local cache with post counts."""
    init_db()
    return db_list_publications()


@mcp.tool()
def list_audiences() -> list[AudienceSummary]:
    """List distinct audience tiers present in local cache with post counts.

    Discovers actual values in use (e.g. "everyone", "only_paid") rather than a
    hardcoded enum, since Substack's audience values aren't officially documented
    and may vary or grow over time.
    """
    init_db()
    return db_list_audiences()


@mcp.tool()
def saved_posts_status() -> SavedPostsStatus:
    """Return cache statistics, database path, and last sync run status."""
    init_db()
    return get_status()


@mcp.tool()
def sync_saved_posts(force: bool = False) -> SyncRun:
    """Trigger incremental or full resync of saved posts from Substack account into local SQLite cache.

    Requires an active authenticated Substack session.
    """
    return run_sync(force=force)


# FastMCP Resources
@mcp.resource("substack://posts/{post_id}")
def get_post_resource(post_id: str) -> str:
    """Resource returning JSON representation of a specific saved post by ID or URL."""
    init_db()
    post = get_post(post_id)
    if not post:
        return json.dumps({"error": f"Post '{post_id}' not found."})
    return post.model_dump_json()


@mcp.resource("substack://publications")
def get_publications_resource() -> str:
    """Resource returning JSON list of cached Substack publications."""
    init_db()
    pubs = db_list_publications()
    return json.dumps([p.model_dump() for p in pubs])


def run_server() -> None:
    """Run FastMCP server over stdio transport."""
    mcp.run(transport="stdio")
