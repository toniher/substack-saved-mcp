"""Command Line Interface (CLI) for Substack Saved Posts MCP & Sync tool."""

import json
import sys
from pathlib import Path
from typing import Optional

import click

from substack_saved_mcp.database import (
    get_post,
    get_status,
    init_db,
    list_posts as db_list_posts,
    list_publications as db_list_publications,
    search_posts as db_search_posts,
    soft_delete_post,
    upsert_post,
)
from substack_saved_mcp.mcp_server import run_server
from substack_saved_mcp.models import SavedPost
from substack_saved_mcp.substack_client import (
    AuthRequiredError,
    SubstackSavedPostsClient,
    perform_interactive_login,
)
from substack_saved_mcp.sync import sync_saved_posts as run_sync


@click.group()
@click.version_option(version="0.1.0")
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
@click.option("--force", is_flag=True, help="Force full resync instead of incremental stop.")
def sync(force: bool) -> None:
    """Sync saved posts from Substack account into local SQLite cache."""
    click.echo("Starting Substack saved posts sync...")
    result = run_sync(force=force)

    if result.status == "success":
        click.secho(f"Sync complete! Fetched {result.fetched_count} posts, upserted {result.upserted_count} posts.", fg="green")
    elif result.status == "auth_required":
        click.secho(f"Authentication required: {result.error_message}", fg="yellow")
    else:
        click.secho(f"Sync failed: {result.error_message}", fg="red")


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
        post = client.save_post(url)
        saved_db_post = upsert_post(post)
        click.secho(f"Successfully saved '{saved_db_post.title}' to local cache!", fg="green")
        click.echo(f"Published at: {saved_db_post.published_at or 'N/A'}")
        click.echo(f"Saved at: {saved_db_post.saved_at}")
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
    try:
        client.unsave_post(post.url)
    except Exception as e:
        click.echo(f"Remote unsave notice: {e}")

    updated = soft_delete_post(post.url)
    if updated:
        click.secho(f"Successfully unsaved '{post.title}' from local cache.", fg="green")


@cli.command()
@click.argument("query")
@click.option("--publication", help="Filter by publication name.")
@click.option("--limit", default=10, help="Maximum search results.")
def search(query: str, publication: Optional[str], limit: int) -> None:
    """Perform full-text search across cached saved posts."""
    init_db()
    results = db_search_posts(query=query, publication=publication, limit=limit)
    if not results:
        click.echo(f"No saved posts matched query '{query}'.")
        return

    click.echo(f"Found {len(results)} matching post(s):\n")
    for idx, p in enumerate(results, 1):
        click.secho(f"{idx}. {p.title}", fg="cyan", bold=True)
        click.echo(f"   Publication : {p.publication_name}")
        click.echo(f"   Published   : {p.published_at or 'N/A'} | Saved: {p.saved_at or 'N/A'}")
        click.echo(f"   URL         : {p.url}")
        if p.excerpt:
            click.echo(f"   Excerpt     : {p.excerpt[:120]}...")
        click.echo("")


@cli.command(name="list")
@click.option("--limit", default=10, help="Number of posts to display.")
@click.option("--offset", default=0, help="Pagination offset.")
@click.option("--publication", help="Filter by publication name.")
@click.option("--sort-by", type=click.Choice(["saved_at", "published_at"]), default="saved_at")
def list_cmd(limit: int, offset: int, publication: Optional[str], sort_by: str) -> None:
    """List saved posts ordered by saved date or publication date."""
    init_db()
    posts = db_list_posts(limit=limit, offset=offset, publication=publication, sort_by=sort_by)
    if not posts:
        click.echo("No saved posts found.")
        return

    click.echo(f"Saved Posts ({len(posts)} displayed):\n")
    for idx, p in enumerate(posts, offset + 1):
        click.secho(f"{idx}. {p.title}", fg="cyan")
        click.echo(f"   Pub: {p.publication_name} | Saved: {p.saved_at or 'N/A'} | Published: {p.published_at or 'N/A'}")
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
        click.echo(f"- {p.publication_name} ({p.post_count} saved post{'s' if p.post_count != 1 else ''})")


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


@cli.command()
def inspect_network() -> None:
    """Inspect and capture Substack saved posts network endpoints structure safely."""
    click.echo("Launching Playwright inspector context...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        click.secho("Playwright not installed.", fg="red")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        def handle_response(response):
            if "api/v1" in response.url or "bookmark" in response.url or "saved" in response.url:
                click.echo(f"[Network Intercept] {response.request.method} {response.url} (Status: {response.status})")

        page.on("response", handle_response)
        page.goto("https://substack.com/saved")
        click.echo("Navigate around your saved posts page. Press ENTER in terminal when finished.")
        input("--> Press ENTER to finish network inspection: ")
        browser.close()


if __name__ == "__main__":
    cli()
