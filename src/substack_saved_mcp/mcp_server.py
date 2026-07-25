"""FastMCP server exposing tools and resources for searching, retrieving, saving, and unsaving Substack posts."""

import json
from typing import Any, Dict, List, Optional
from fastmcp import FastMCP

from substack_saved_mcp.database import (
    get_post,
    get_status,
    init_db,
    list_posts,
    list_publications as db_list_publications,
    search_posts,
    soft_delete_post,
    upsert_post,
)
from substack_saved_mcp.models import (
    PostSummary,
    PublicationSummary,
    SavedPost,
    SavedPostsStatus,
    SyncRun,
)
from substack_saved_mcp.substack_client import AuthRequiredError, SubstackSavedPostsClient
from substack_saved_mcp.sync import sync_saved_posts as run_sync

# Initialize FastMCP Server
mcp = FastMCP("Substack Saved Posts")


@mcp.tool()
def search_saved_posts(
    query: str,
    publication: Optional[str] = None,
    published_after: Optional[str] = None,
    published_before: Optional[str] = None,
    saved_after: Optional[str] = None,
    saved_before: Optional[str] = None,
    limit: int = 20,
) -> List[PostSummary]:
    """Perform full-text FTS5 search across cached saved posts.

    Searches title, excerpt, publication name, author, and content text.
    Allows filtering by publication name, original post date (published_at), and saved date (saved_at).
    """
    init_db()
    return search_posts(
        query=query,
        publication=publication,
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
    publication: Optional[str] = None,
    sort_by: str = "saved_at",
) -> List[PostSummary]:
    """List cached saved posts with pagination and optional publication filter.

    sort_by can be 'saved_at' (when post was bookmarked) or 'published_at' (when post was published).
    """
    init_db()
    return list_posts(
        limit=limit,
        offset=offset,
        publication=publication,
        sort_by=sort_by,
        is_saved_only=True,
    )


@mcp.tool()
def get_saved_post(url_or_id: str) -> Optional[SavedPost]:
    """Retrieve full cached post details, timestamps (published_at and saved_at), and content by URL or local ID."""
    init_db()
    return get_post(url_or_id)


@mcp.tool()
def save_post(url: str) -> SavedPost:
    """Bookmark a Substack post remotely on Substack and save it to the local cache.

    Requires an active authenticated Substack session (run 'substack-saved-mcp login' if expired).
    """
    init_db()
    client = SubstackSavedPostsClient()
    saved_model = client.save_post(url)
    updated_db_post = upsert_post(saved_model)
    return updated_db_post


@mcp.tool()
def unsave_post(url_or_id: str) -> Dict[str, Any]:
    """Unbookmark a Substack post remotely and soft-delete it in local cache.

    Soft-deletion preserves post history while removing it from active search/list outputs.
    """
    init_db()
    post = get_post(url_or_id)
    if not post:
        return {"success": False, "message": f"Post '{url_or_id}' not found in local cache."}

    client = SubstackSavedPostsClient()
    try:
        client.unsave_post(post.url)
    except AuthRequiredError as e:
        return {"success": False, "message": str(e)}
    except Exception as e:
        # Continue soft deletion locally even if remote unsave fails
        pass

    updated_post = soft_delete_post(post.url)
    return {
        "success": True,
        "message": f"Successfully unsaved post '{post.title}'.",
        "post": updated_post,
    }


@mcp.tool()
def list_publications() -> List[PublicationSummary]:
    """List all publications in local cache with post counts."""
    init_db()
    return db_list_publications()


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
