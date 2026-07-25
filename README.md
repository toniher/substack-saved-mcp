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

# Authenticate with Substack (opens browser window)
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
