"""Command Line Interface (CLI) for Substack Saved Posts MCP & Sync tool."""

import sys

import click

from substack_saved_mcp.content_utils import format_post_for_llm, html_to_llm_text
from substack_saved_mcp.database import (
    get_post,
    get_status,
    init_db,
    soft_delete_post,
    upsert_post,
)
from substack_saved_mcp.database import (
    list_audiences as db_list_audiences,
)
from substack_saved_mcp.database import (
    list_posts as db_list_posts,
)
from substack_saved_mcp.database import (
    list_publications as db_list_publications,
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
        msg = f"Sync complete! Fetched {result.fetched_count} posts, upserted {result.upserted_count} posts."
        if result.reconciled_count:
            msg += f" Unsaved {result.reconciled_count} post(s) no longer on Substack's saved list."
        click.secho(msg, fg="green")
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
        post, confirmation = client.save_post(url)
        saved_db_post = upsert_post(post)
        click.secho(f"Successfully saved '{saved_db_post.title}' to local cache!", fg="green")
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
        click.secho(f"Successfully unsaved '{post.title}' from local cache.", fg="green")
        if confirmation != "confirmed":
            click.secho(
                f"Warning: could not confirm the bookmark was removed on Substack's own page "
                f"(status: {confirmation}).",
                fg="yellow",
            )


@cli.command()
@click.argument("query")
@click.option("--publication", help="Filter by publication name.")
@click.option("--audience", help="Filter by audience tier (e.g. everyone, only_paid). See 'audiences' command for cached values.")
@click.option("--published-after", help="Only posts published on/after this ISO-8601 date (e.g. 2026-01-01).")
@click.option("--published-before", help="Only posts published on/before this ISO-8601 date.")
@click.option("--saved-after", help="Only posts bookmarked on/after this ISO-8601 date.")
@click.option("--saved-before", help="Only posts bookmarked on/before this ISO-8601 date.")
@click.option("--limit", default=10, help="Maximum search results.")
def search(
    query: str,
    publication: str | None,
    audience: str | None,
    published_after: str | None,
    published_before: str | None,
    saved_after: str | None,
    saved_before: str | None,
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
        limit=limit,
    )
    if not results:
        click.echo(f"No saved posts matched query '{query}'.")
        return

    click.echo(f"Found {len(results)} matching post(s):\n")
    for idx, p in enumerate(results, 1):
        click.secho(f"{idx}. {p.title}", fg="cyan", bold=True)
        click.echo(f"   Publication : {p.publication_name}")
        click.echo(f"   Published   : {p.published_at or 'N/A'} | Saved: {p.saved_at or 'N/A'}")
        click.echo(f"   Audience    : {p.audience or 'N/A'}")
        if p.reading_time_minutes or p.word_count:
            click.echo(f"   Reading time: {p.reading_time_minutes or '?'} min ({p.word_count or '?'} words)")
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
@click.option("--audience", help="Filter by audience tier (e.g. everyone, only_paid). See 'audiences' command for cached values.")
@click.option("--sort-by", type=click.Choice(["saved_at", "published_at"]), default="saved_at")
def list_cmd(limit: int, offset: int, publication: str | None, audience: str | None, sort_by: str) -> None:
    """List saved posts ordered by saved date or publication date."""
    init_db()
    posts = db_list_posts(limit=limit, offset=offset, publication=publication, audience=audience, sort_by=sort_by)
    if not posts:
        click.echo("No saved posts found.")
        return

    click.echo(f"Saved Posts ({len(posts)} displayed):\n")
    for idx, p in enumerate(posts, offset + 1):
        reading = f" | {p.reading_time_minutes} min ({p.word_count} words)" if p.reading_time_minutes else ""
        click.secho(f"{idx}. {p.title}", fg="cyan")
        click.echo(f"   Pub: {p.publication_name} | Saved: {p.saved_at or 'N/A'} | Published: {p.published_at or 'N/A'} | Audience: {p.audience or 'N/A'}{reading}")
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
def audiences() -> None:
    """List all audience tiers present in the cache (e.g. everyone, only_paid)."""
    init_db()
    tiers = db_list_audiences()
    if not tiers:
        click.echo("No posts in cache.")
        return

    click.echo(f"Cached Audience Tiers ({len(tiers)} total):\n")
    for t in tiers:
        click.echo(f"- {t.audience or 'unknown'} ({t.post_count} saved post{'s' if t.post_count != 1 else ''})")


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


@cli.command(name="get-content")
@click.argument("url_or_id")
@click.option("--no-cache", is_flag=True, help="Don't store the fetched content in the local cache.")
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
        click.echo(format_post_for_llm(
            title=post.title,
            publication_name=post.publication_name,
            url=post.url,
            body_text=post.content_text,
            author_name=post.author_name,
            published_at=post.published_at,
        ))
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

    click.echo(format_post_for_llm(
        title=post.title,
        publication_name=post.publication_name,
        url=post.url,
        body_text=body_text,
        author_name=post.author_name,
        published_at=post.published_at,
    ))


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
                post_data = response.request.post_data
                if post_data:
                    click.echo(f"    Body: {post_data}")

        page.on("response", handle_response)
        page.goto("https://substack.com/saved")
        click.echo("Navigate around your saved posts page. Press ENTER in terminal when finished.")
        input("--> Press ENTER to finish network inspection: ")
        browser.close()


if __name__ == "__main__":
    cli()
