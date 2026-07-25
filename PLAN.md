# Saved Substack Posts MCP Plan

## Goal

Create a local, stdio-based MCP server that lets an MCP client (such as Claude Desktop or Goose) search, retrieve, save, and unsave a user's Substack posts. A background/CLI sync process retrieves saved posts through the user's authenticated Substack browser session and maintains a local SQLite cache.

The server must work offline against cached data for search and read queries, while write operations (`save_post` and `unsave_post`) use the authenticated Playwright browser session to sync actions directly with Substack and update the local database.

The server tracks both **when the post was saved** (`saved_at`) and **the original publication date of the post** (`published_at`).

---

## Assumptions and Boundaries

- "Favourite" / "Saved" means a post saved or bookmarked in the user's Substack account (`https://substack.com/saved`).
- Substack does not provide a documented, stable public API for bookmarking. Sync and write actions use Playwright authenticated contexts or reverse-engineered private API endpoints.
- Read operations (list, search, get) work 100% offline using the local SQLite cache.
- Write operations (`save_post`, `unsave_post`) require a valid authenticated session to update Substack remotely, followed immediately by updating the local SQLite cache.
- The project will use Python 3.11+, Playwright, SQLite (with FTS5), and FastMCP over stdio transport.

---

## Key Architecture & Data Flow

```text
Substack Authenticated Session (Playwright persistent profile / storage_state.json)
                                |
                   +------------+------------+
                   |                         |
          (Read / Sync Engine)      (Write Operations)
                   |                         |
                   v                         v
           sync_saved_posts.py       save_post() / unsave_post()
           - Paginated sync          - Calls Substack bookmark API / DOM
           - Auth expiration check   - Updates remote bookmark state
           - Normalizes posts        - Updates local DB immediately
                   |                         |
                   +------------+------------+
                                |
                                v
                saved_posts.sqlite (SQLite + FTS5)
                - Stores published_at & saved_at
                - Tracks is_saved (1 = active, 0 = unsaved)
                                |
                                v
                       FastMCP stdio Server
           - Tools: list, search, get_post, save_post, unsave_post, list_publications, status, sync
           - Resources: substack://posts/{id}, substack://publications
                                |
                                v
                            MCP Client
```

---

## Repository Layout

```text
substack-saved-mcp/
  pyproject.toml
  README.md
  PLAN.md
  src/substack_saved_mcp/
    __init__.py
    config.py                  # Environment & default OS app-data paths
    models.py                  # Dataclasses/Pydantic schemas for posts & sync runs
    database.py                # SQLite schema, FTS5 virtual table, migrations, queries
    url_utils.py               # Canonicalization & tracking query param stripping
    substack_client.py         # Playwright reader & writer for Substack posts
    sync.py                    # Incremental & full sync engine with rate limiting
    mcp_server.py              # FastMCP server, read/write tools, and resources
    cli.py                     # Click/Typer CLI (login, sync, serve, save, unsave, status, inspect)
  tests/
    test_database.py
    test_url_utils.py
    test_normalization.py
    test_search.py
    test_write_actions.py      # Tests for save and unsave database & API handlers
    test_mcp_server.py
    fixtures/                  # Sanitized sample API response JSON payloads
  data/                        # Gitignored default SQLite location
  browser-state/               # Gitignored Playwright storage_state.json & user profile
```

---

## Data Model & Schema Design

Use SQLite with FTS5 enabled from initial setup to provide fast full-text search across titles, publication names, authors, and excerpts/content.

### Table: `posts`

| Column | Type | Constraints / Description |
|---|---|---|
| `id` | INTEGER | Primary Key AUTOINCREMENT |
| `substack_post_id` | TEXT | Remote identifier when available; `UNIQUE` when populated |
| `url` | TEXT | Canonical post URL (tracking params stripped); `UNIQUE` |
| `title` | TEXT | Post title |
| `publication_name` | TEXT | Publication display name |
| `publication_url` | TEXT | Publication URL |
| `author_name` | TEXT | Author display name |
| `published_at` | TEXT | **ISO-8601 UTC timestamp of original post publication** |
| `saved_at` | TEXT | **ISO-8601 UTC timestamp when post was saved/bookmarked** |
| `unsaved_at` | TEXT | Optional ISO-8601 UTC timestamp when unsaved |
| `is_saved` | INTEGER | **1 = active saved post, 0 = unsaved post** (default `1`) |
| `excerpt` | TEXT | Plain-text summary or lead snippet |
| `content_text` | TEXT | Optional full body text (plain text or Markdown) |
| `image_url` | TEXT | Optional lead thumbnail/cover image URL |
| `is_paywalled` | INTEGER | Boolean flag (0 = free, 1 = paywalled) |
| `reading_time_minutes` | INTEGER | Optional estimated reading time |
| `word_count` | INTEGER | Optional word count |
| `metadata_json` | TEXT | Raw sanitized source JSON retained for future migrations |
| `created_at` | TEXT | Local record insertion timestamp (ISO-8601) |
| `updated_at` | TEXT | Local record update timestamp (ISO-8601) |

### Indexes
- `idx_posts_url` ON `posts(url)`
- `idx_posts_published_at` ON `posts(published_at DESC)`
- `idx_posts_saved_at` ON `posts(saved_at DESC)`
- `idx_posts_is_saved` ON `posts(is_saved)`

### Virtual Table: `posts_fts` (FTS5)

Created over `title`, `publication_name`, `author_name`, `excerpt`, and `content_text` with triggers to keep `posts_fts` synchronized on `INSERT`, `UPDATE`, and `DELETE`. Search queries filter by default on `is_saved = 1`.

---

## MCP Tool Suite Specification

| Tool Name | Action | Parameters | Description & Output |
|---|---|---|---|
| `search_saved_posts` | **Read** | `query` (str, req), `publication` (opt), `published_after` (opt), `published_before` (opt), `saved_after` (opt), `saved_before` (opt), `limit` (int, default 20) | Full-text FTS5 search across saved posts. Returns list of concise posts with `title`, `url`, `publication_name`, `published_at`, `saved_at`, `excerpt`. |
| `list_saved_posts` | **Read** | `limit` (int, default 20), `offset` (int, default 0), `publication` (opt), `sort_by` (`saved_at` \| `published_at`, default `saved_at`) | Paginated list of active saved posts ordered by `saved_at` or `published_at`. |
| `get_saved_post` | **Read** | `url_or_id` (str, req) | Detailed view of a single post, including full metadata, timestamps (`published_at`, `saved_at`), and `content_text` if cached. |
| `save_post` | **Write** | `url` (str, req) | Saves/bookmarks a Substack post remotely on Substack and updates local DB. Returns updated post object with `saved_at` set to current UTC time. |
| `unsave_post` | **Write** | `url_or_id` (str, req) | Unsaves/unbookmarks a Substack post remotely on Substack and updates local DB (`is_saved = 0`, `unsaved_at = current UTC time`). |
| `list_publications` | **Read** | None | Returns list of all publications present in cache with count of saved posts for each. |
| `saved_posts_status` | **Read** | None | Cache statistics: active saved posts count, total unsaved count, last sync run details. |
| `sync_saved_posts` | **Sync** | `force` (bool, default False) | Triggers background/incremental sync from Substack account into local DB. |

---

## Detailed Write Action & Date Tracking Design

### 1. Date Tracking Strategy (`published_at` vs `saved_at`)
- **`published_at`**: Extracted from Substack post payload (`post.post_date` or HTML `<time datetime="...">` metadata). Preserves when the article was original authored/published.
- **`saved_at`**:
  - When synced from Substack: Extracted from Substack's bookmark object (e.g. `bookmark.created_at`).
  - When saved via `save_post` tool: Recorded immediately as local ISO-8601 UTC timestamp (`datetime.now(timezone.utc)`).

### 2. Implementation of `save_post(url)`
1. **Canonicalization**: Strip tracking parameters (`utm_*`, `r`, `s`, etc.) from `url`.
2. **Remote Execution**:
   - Using `SubstackSavedPostsClient` with `storage_state.json`:
   - Issue authenticated POST request to Substack bookmark endpoint (e.g. `/api/v1/bookmark` or post page action).
   - If session is expired (401/403), fail early with `auth_required` error message.
3. **Database Update**:
   - Parse returned post metadata (title, publication, `published_at`).
   - Set `saved_at = datetime.now(timezone.utc).isoformat()`, `is_saved = 1`, `unsaved_at = NULL`.
   - Upsert record into `posts` table and update `posts_fts`.
4. **Return**: Structured output with confirmation and full post summary.

### 3. Implementation of `unsave_post(url_or_id)`
1. **Resolution**: Lookup canonical post in SQLite by ID or canonical URL.
2. **Remote Execution**:
   - Issue authenticated DELETE request to Substack bookmark endpoint for `substack_post_id`.
   - If session is expired (401/403), fail early with `auth_required` error message.
3. **Database Update**:
   - Set `is_saved = 0` and `unsaved_at = datetime.now(timezone.utc).isoformat()`.
   - Keep post record in database (soft delete) so history is preserved and sync does not re-add it unexpectedly.
4. **Return**: Structured confirmation containing unsaved post title and canonical URL.

---

## Execution Phases

### Phase 1: Package Setup, Schema & Date/URL Utilities
1. Initialize Python package (`pyproject.toml` with `fastmcp`, `playwright`, `pytest`).
2. Implement `url_utils.py` for query param stripping and canonical URL normalization.
3. Implement `database.py` with SQLite schema including `published_at`, `saved_at`, `unsaved_at`, `is_saved`, FTS5 triggers, and CRUD queries.
4. Build `cli.py` skeleton with subcommands: `init`, `login`, `sync`, `serve`, `save`, `unsave`, `status`, `inspect-network`.

### Phase 2: Session Capture & Network Inspection (Reads & Writes)
1. Implement `substack-saved-mcp login` using Playwright in headful mode (`headless=False`).
2. Store authenticated browser storage state in `browser-state/storage_state.json`.
3. Implement `inspect-network` command to record network payloads for:
   - Reading saved posts page (`GET /api/v1/saved_posts`)
   - Bookmarking a post (`POST /api/v1/bookmark`)
   - Unbookmarking a post (`DELETE /api/v1/bookmark`)
4. Implement `SubstackSavedPostsClient` with `fetch_saved_posts()`, `save_post()`, and `unsave_post()` methods.

### Phase 3: Incremental Sync Engine & Error Guardrails
1. Implement paginated sync with rate-limit backoff (500ms delay between pages).
2. Store `published_at` (original post date) and `saved_at` (bookmark date) for each normalized post.
3. Support incremental sync cutoff when encountering existing saved posts.
4. Handle session expiration cleanly with actionable error messages.

### Phase 4: Local Query Engine with FTS5 & Date Filtering
1. Implement repository methods in `database.py`:
   - `search_posts(query, publication, published_after, published_before, saved_after, saved_before, limit)`
   - `list_posts(limit, offset, publication, sort_by)`
   - `get_post(url_or_id)`
   - `list_publications()`
   - `save_post_db(post_data)`
   - `unsave_post_db(url_or_id)`
2. Ensure search and list queries filter by default on `is_saved = 1`.

### Phase 5: FastMCP Tool & Resource Suite
1. Implement `mcp_server.py` with all 8 tools (`search_saved_posts`, `list_saved_posts`, `get_saved_post`, `save_post`, `unsave_post`, `list_publications`, `saved_posts_status`, `sync_saved_posts`).
2. Expose resources `substack://posts/{id}` and `substack://publications`.
3. Bind `substack-saved-mcp serve` CLI command.

### Phase 6: Privacy, Hardening & Security Audit
1. Verify storage paths use standard OS app-data directories.
2. Audit log sanitization to ensure tokens/cookies are never logged.
3. Validate write actions prevent unexpected side-effects.

### Phase 7: Comprehensive Testing Suite
1. **Unit Tests**: Test URL canonicalization, date parsing, FTS queries, and soft-deletion logic.
2. **Write Action Tests**: Test `save_post` and `unsave_post` database state transitions and mock API client calls.
3. **MCP Integration Tests**: Verify all 8 tools over stdio with fixture database states.

---

## Definition of Done (v1.0)

- User can authenticate via `login`.
- MCP client can search saved posts with full-text FTS5 query matching and date range filters (`published_at`, `saved_at`).
- MCP client can issue `save_post` to bookmark a new Substack URL remotely and cache it locally.
- MCP client can issue `unsave_post` to unbookmark a Substack post remotely and soft-delete it locally.
- `published_at` (original post date) and `saved_at` (bookmark date) are tracked and returned for every post.
- Fixture tests pass for all read, search, write, and sync tool workflows.
