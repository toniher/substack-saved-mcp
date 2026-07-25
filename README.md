# Substack Saved Posts MCP & CLI

A local, stdio-based Model Context Protocol (MCP) server and sync engine for your saved/bookmarked Substack posts.

## Features

- **Read & Search**: Full-text search (SQLite FTS5) across saved post titles, excerpts, authors, publications, and content. Filter by publication and date ranges (`published_at` vs `saved_at`).
- **Save & Unsave**: Bookmark new Substack posts or unbookmark existing ones via authenticated browser sessions.
- **Offline First**: Fast, offline queries directly from local SQLite cache.
- **Privacy & Security**: Keeps session credentials local, redacting tokens from logs.
- **FastMCP Protocol**: Stdio MCP interface with rich tool suite and resources.

## Quick Start

```bash
# Initialize local database
substack-saved-mcp init

# Authenticate with Substack (opens interactive browser window once)
substack-saved-mcp login

# Sync saved posts into local cache
substack-saved-mcp sync

# Search saved posts via CLI
substack-saved-mcp search "artificial intelligence"

# Save or unsave a post
substack-saved-mcp save "https://example.substack.com/p/post-slug"
substack-saved-mcp unsave "https://example.substack.com/p/post-slug"

# Launch stdio MCP server for Claude Desktop / Goose
substack-saved-mcp serve
```

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

### Will a browser window pop up when running as an MCP server?

**No, a visible browser window will not open during normal MCP operations.**

- **Read & Search Tools** (`search_saved_posts`, `list_saved_posts`, `get_saved_post`, `list_publications`, `saved_posts_status`):  
  Operate 100% offline using the local SQLite database. Zero browser activity.
- **Sync & Write Tools** (`sync_saved_posts`, `save_post`, `unsave_post`):  
  Run in **headless background mode** using the pre-authenticated session stored in `storage_state.json`.
- **Interactive Login**:  
  A visible browser window opens **only** when you manually run `substack-saved-mcp login` from your terminal. If your session expires while using an MCP client, the tool will return a clear error message instructing you to re-authenticate via `substack-saved-mcp login` instead of popping open a browser window unexpectedly.
