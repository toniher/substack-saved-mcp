# Substack Saved Posts & Notes MCP & CLI

[![PyPI version](https://img.shields.io/pypi/v/substack-saved-mcp.svg)](https://pypi.org/project/substack-saved-mcp/)

A local, stdio-based Model Context Protocol (MCP) server and sync engine for your saved/bookmarked Substack **posts and notes**.

## Features

- **Read & Search**: Full-text search (SQLite FTS5) across saved post titles, excerpts, authors, and publications. Filter by publication, audience tier (e.g. `everyone`, `only_paid`), reading progress (`--read-state unread`/`in_progress`/`finished`/`started`), and date ranges (`published_at` vs `saved_at`). Search also covers a post's **full body text**, but only for posts whose content has already been fetched once via `get-content` / the `get_post_content` tool — a normal `sync` stores metadata and excerpts, not full bodies, so posts you haven't opened yet are matched on their title/excerpt/metadata only, not their full text.
- **Reading Progress**: Substack tracks how far you've read each saved post (visible in its mobile apps, not the web UI) — this tool surfaces it. Each post reports `is_fully_read` and `minutes_remaining`, derived from the stored `max_read_progress` high-water mark at a configurable threshold (default 0.95, `SUBSTACK_SAVED_FULLY_READ_THRESHOLD`). Filter with `--read-state`, or sort a list by `read_progress`/`minutes_remaining` to find something short to finish. Progress refreshes on every sync a post is touched by; run `sync --force` to refresh it for your whole backlog.
- **Saved Notes, too**: Substack's short-form notes are synced, searched, and cached separately from posts (they carry an author and body rather than a title or publication tier). Full-text search covers note bodies, authors, and restacked-post titles. Notes never require a browser at all — every notes operation (sync, save, unsave, full-content fetch) is a plain authenticated API call.
- **Full Content for LLMs**: Fetch a saved post's or note's full content and get it back cleaned and formatted (headings, lists, links) for feeding directly to an LLM, with the result cached locally for next time.
- **Save & Unsave**: Bookmark new Substack posts and notes, or unbookmark existing ones. Posts go through an authenticated browser session; notes are API-only.
- **Offline First**: Fast, offline queries directly from local SQLite cache.
- **Privacy & Security**: Keeps session credentials local, redacting tokens from logs.
- **FastMCP Protocol**: Stdio MCP interface with rich tool suite and resources for both posts and notes.

---

## Installation with `uv`

[`uv`](https://github.com/astral-sh/uv) is the recommended fast Python package manager for installing and running `substack-saved-mcp`.

### Option A: Install from PyPI as a System-wide Tool (`uv tool install`)

Install the published package from [PyPI](https://pypi.org/project/substack-saved-mcp/):

```bash
# Install system-wide into an isolated uv environment
uv tool install substack-saved-mcp
```

To install directly from a local repository folder instead:

```bash
# Navigate to the repository
cd /path/to/substack-saved-mcp

# Install system-wide into an isolated uv environment
uv tool install .

# Or install directly from a remote Git repository:
# uv tool install git+https://github.com/your-username/substack-saved-mcp.git
```

After installation, `substack-saved-mcp` is immediately available in your PATH:

```bash
# Verify installation
substack-saved-mcp --help
```

To update or uninstall:
```bash
# Upgrade installed tool
uv tool upgrade substack-saved-mcp

# Uninstall tool
uv tool uninstall substack-saved-mcp
```

---

### Option B: Local Development / Development Environment (`uv sync`)

If you are developing or modifying the codebase:

```bash
# Clone and enter directory
cd substack-saved-mcp

# Install dependencies and dev tools (pytest)
uv sync --extra dev

# Run CLI commands using uv run
uv run substack-saved-mcp --help

# Run tests
uv run pytest
```

---

## Quick Start

```bash
# 1. Initialize local database
substack-saved-mcp init

# 2. Authenticate with Substack (opens interactive browser window once)
substack-saved-mcp login

# 3. Sync saved posts AND notes into local cache (both entities by default)
substack-saved-mcp sync

# 3b. Or sync just one entity
substack-saved-mcp sync --only posts
substack-saved-mcp sync --only notes

# 4. Search saved posts via CLI
substack-saved-mcp search "artificial intelligence"

# 4b. Filter by publication or audience tier (see which tiers are cached with `audiences`)
substack-saved-mcp audiences
substack-saved-mcp list --audience only_paid
substack-saved-mcp search "artificial intelligence" --audience everyone

# 4c. Filter or sort by reading progress
substack-saved-mcp list --read-state finished
substack-saved-mcp list --read-state in_progress --sort-by minutes_remaining
substack-saved-mcp search "artificial intelligence" --read-state unread

# 5. Save or unsave a post
substack-saved-mcp save "https://example.substack.com/p/post-slug"
substack-saved-mcp unsave "https://example.substack.com/p/post-slug"

# 6. Get a saved post's full content, cleaned up and ready for an LLM
substack-saved-mcp get-content "https://example.substack.com/p/post-slug"

# 7. Work with saved notes the same way
substack-saved-mcp list-notes --limit 10
substack-saved-mcp search-notes "kubernetes" --author alice
substack-saved-mcp note-authors
substack-saved-mcp save-note "https://substack.com/@handle/note/c-123456"
substack-saved-mcp unsave-note "https://substack.com/@handle/note/c-123456"
substack-saved-mcp get-note "https://substack.com/@handle/note/c-123456"

# 8. Check combined status (posts and notes counts, last sync per entity)
substack-saved-mcp status

# 9. Launch stdio MCP server
substack-saved-mcp serve
```

---

## Configuring MCP Clients (Claude Desktop, Goose, Cursor, etc.)

Add `substack-saved-mcp` to your MCP client's configuration file (e.g. `claude_desktop_config.json`).

### Using System-Wide Installed Tool (`uv tool` or global binary)

```json
{
  "mcpServers": {
    "substack-saved": {
      "command": "substack-saved-mcp",
      "args": ["serve"]
    }
  }
}
```

### Using `uv` directly from the Repository Path

If you prefer running directly from your repository path without installing system-wide:

```json
{
  "mcpServers": {
    "substack-saved": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/substack-saved-mcp",
        "run",
        "substack-saved-mcp",
        "serve"
      ]
    }
  }
}
```

---

## Frequently Asked Questions (FAQ)

### Where is the database saved?

By default, the SQLite database is saved in your OS application data directory:

- **Linux / macOS**: `~/.local/share/substack-saved-mcp/saved_posts.sqlite`  
  *(or `$XDG_DATA_HOME/substack-saved-mcp/saved_posts.sqlite` if `XDG_DATA_HOME` is set)*

You can specify a custom database path or directory using environment variables:
```bash
export SUBSTACK_SAVED_DB_PATH="/path/to/my/custom_database.sqlite"
# or
export SUBSTACK_SAVED_DATA_DIR="/path/to/my/data_dir"
```

### Which Substack API does syncing saved posts use?

Saved posts are fetched from Substack's newer unified reader API by default, with
automatic fallback to an older, posts-only API and finally to headless browser
scraping if needed — you shouldn't normally need to think about this. If you ever
want to force a specific source (e.g. while troubleshooting), set:
```bash
export SUBSTACK_SAVED_POSTS_SOURCE="unified"  # or "legacy" or "dom"
```
Leave it unset (or `"auto"`) for the default, self-healing behavior.

### What does a "partial" sync status mean?

If Substack rate-limits (HTTP 429) a sync so heavily that a page of results
can't be fetched even after retrying, the sync keeps whatever it already
fetched rather than failing outright, and reports `status: partial` (instead
of `success`) in `substack-saved-mcp status` or the sync tool's response.
When this happens on a `sync --force`, reconciliation (soft-deleting posts/notes
no longer in the remote list) is automatically skipped for that run, so a post
or note that merely couldn't be fetched is never mistaken for one you actually
unsaved on Substack. Just run `sync` again later — a subsequent successful run
picks up anything that was missed.

### How is "fully read" determined, and how fresh is it?

Substack reports a `max_read_progress` high-water mark (0.0–1.0) per saved post; a
post counts as fully read once that crosses a threshold (default `0.95` — real
posts top out around `0.98`–`0.9999` rather than an exact `1.0`). Override it with:
```bash
export SUBSTACK_SAVED_FULLY_READ_THRESHOLD="0.90"
```
Progress is refreshed whenever a post is re-fetched during sync. An incremental
sync only touches recently-saved posts, so progress on older posts in your
backlog can go stale between reads; run `substack-saved-mcp sync --force` to
refresh it for everything. Notes have no reading-progress concept.

### Will a browser window pop up when running as an MCP server?

**No, a visible browser window will not open during normal MCP operations.**

- **Read & Search Tools** (`search_saved_posts`, `list_saved_posts`, `get_saved_post`, `search_saved_notes`, `list_saved_notes`, `get_saved_note`, `list_publications`, `list_audiences`, `saved_posts_status`):  
  Operate 100% offline using the local SQLite database. Zero browser activity.
- **Post Sync & Write Tools** (`sync_saved_posts`, `save_post`, `unsave_post`, `get_post_content`):  
  Run in **headless background mode** using the pre-authenticated session stored in `storage_state.json`.
- **Note Sync & Write Tools** (`sync_saved_notes`, `save_note`, `unsave_note`, `get_note_content`):  
  Never open a browser page at all, headless or otherwise — Substack's notes endpoints are plain authenticated HTTP calls, so these tools only ever make direct API requests using `storage_state.json`.
- **Interactive Login**:  
  A visible browser window opens **only** when you manually run `substack-saved-mcp login` from your terminal. If your session expires while using an MCP client, the tool will return a clear error message instructing you to re-authenticate via `substack-saved-mcp login` instead of popping open a browser window unexpectedly.

### What if I get a Playwright "Executable doesn't exist" error?

If you encounter an error like `BrowserType.launch: Executable doesn't exist` when running commands (especially `login`), it means Playwright hasn't installed its required browsers in the isolated environment.

To fix this, you need to run the `playwright install` command *inside* the environment where the tool is installed. 

For a system-wide tool installation (via `uv tool install`), run:
```bash
~/.local/share/uv/tools/substack-saved-mcp/bin/playwright install
```

If you are using a local development environment (via `uv sync`), run:
```bash
uv run playwright install
```

### I edited the source code, but the installed `substack-saved-mcp` command still behaves like the old version. Why?

`uv tool install` copies the package into its own isolated environment at install time — it does **not** track your working tree. If you edited files under `src/` (or pulled new commits) after installing the tool system-wide, the globally installed copy is stale and keeps running the old code, even though `uv run substack-saved-mcp ...` from the repo would use the latest source.

Reinstall from your current working tree to pick up the changes:
```bash
uv tool install . --no-cache --force
```
- `--force` replaces the existing installed version instead of skipping the install because a version is already present.
- `--no-cache` ensures a fresh build rather than reusing a cached wheel/build artifact from before your edits.

Do this any time after modifying the codebase and before relying on the globally installed `substack-saved-mcp` binary (as opposed to `uv run substack-saved-mcp`, which always reflects the working tree).
