# Saved Substack Posts & Notes MCP Plan

This is the living plan and decision record for the project. Implementation-level detail lives in
`CLAUDE.md`; this file keeps the *why*: goals, scope boundaries, decisions taken, evidence
that settled them, what remains open, and the verification recipes worth re-running.

## Status

| Milestone | State |
| --- | --- |
| v1.0 — saved posts: sync, FTS5 search, read, save/unsave, MCP + CLI | Shipped |
| 0.2.0 — saved notes as a separate entity, end to end | Shipped |
| 0.3.0 — unified reader API as the primary saved-posts source | Shipped |
| Mid-pagination silent-truncation fix (`partial` sync status) | Shipped |
| 0.4.0 — reading-progress tracking and `read_state` filtering for posts | Shipped |

---

## Goal

A local, stdio-based MCP server that lets an MCP client (such as Claude Desktop or Goose)
search, retrieve, save, and unsave a user's saved Substack **posts** and **notes**. A
CLI-driven sync process retrieves them through the user's authenticated Substack browser
session and maintains a local SQLite cache.

Read queries work fully offline against the cache. Write operations (`save`/`unsave`, for
both entities) use the authenticated session to act on Substack and then update the local
database.

Both entities distinguish **when the item was saved** (`saved_at`) from **when it was
published/posted** (`published_at` / `posted_at`).

---

## Assumptions and Boundaries

- "Saved" means an item bookmarked in the user's Substack account (`https://substack.com/saved`,
  which has an **All / Posts / Notes** toggle).
- Substack has no documented, stable public API for bookmarking. Every endpoint used here was
  discovered against a live session with `inspect-network` and verified with `probe-api`; none
  was guessed into code. When Substack's frontend changes, rediscovery goes through those two
  tools again — never through a plausible-looking guess.
- Read operations (list, search, get) work 100% offline from SQLite.
- Write operations require a valid authenticated session; the local cache always records the
  user's *intent* even when the remote toggle can't be confirmed (see Conventions in
  `CLAUDE.md`).
- Posts and notes are separate entities with separate tables, separate sync runs, and separate
  tools. `filter=all` (fetching both in one paginated pass) is deliberately out of scope.
- Python 3.11+, Playwright, SQLite with FTS5, FastMCP over stdio.

---

## Key Architecture & Data Flow

```text
Substack Authenticated Session (Playwright storage_state.json)
                                |
        +-----------------------+-----------------------+
        |                       |                       |
 (Posts sync)             (Notes sync)          (Write operations)
        |                       |                       |
        v                       v                       v
 sync_saved_posts()      sync_saved_notes()     save_post/unsave_post
 unified API             /api/v1/reader/        save_note/unsave_note
   -> legacy API           saved?filter=notes   - real bookmark endpoints
   -> DOM scrape          (no DOM fallback)     - local write regardless of
 - truncation-aware      - id-keyed reconcile     remote confirmation
 - URL-keyed reconcile
        |                       |                       |
        +-----------------------+-----------------------+
                                |
                                v
                 saved_posts.sqlite (SQLite + FTS5)
                 posts / posts_fts   notes / notes_fts
                 sync_runs (entity = 'post' | 'note')
                 - published_at/posted_at vs saved_at
                 - is_saved (1 = active, 0 = soft-deleted)
                                |
                                v
                       FastMCP stdio Server
              posts + notes read/write/sync tools, resources
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
  CLAUDE.md                  # implementation-level documentation
  PLAN.md                    # this file
  src/substack_saved_mcp/
    __init__.py
    config.py                # paths + SUBSTACK_SAVED_* env overrides
    models.py                # SavedPost/PostSummary, SavedNote/NoteSummary, SyncRun, status
    database.py              # SQLite schema, FTS5 tables/triggers, migrations, repository
    url_utils.py             # canonicalization & tracking param stripping
    content_utils.py         # HTML/ProseMirror -> LLM text, post/note LLM formatting
    substack_client.py       # Playwright reader & writer (posts + notes), probe/inspect
    sync.py                  # parse + incremental/full sync engines for both entities
    mcp_server.py            # FastMCP tools & resources
    cli.py                   # Click CLI
  tests/
    test_database.py  test_url_utils.py  test_normalization.py
    test_sync.py  test_substack_client.py  test_content_utils.py
    test_cli.py  test_mcp_server.py
```

---

## Data Model

SQLite with FTS5. Two entity tables, each with an external-content FTS5 index maintained by
insert/update/delete triggers. Never write to `posts_fts`/`notes_fts` directly.

### Table: `posts`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | PK AUTOINCREMENT |
| `substack_post_id` | TEXT | Remote id; `UNIQUE` when populated |
| `url` | TEXT | Canonical URL (tracking params stripped); `UNIQUE` |
| `title` | TEXT | |
| `publication_name` / `publication_url` | TEXT | |
| `author_name` | TEXT | |
| `published_at` | TEXT | ISO-8601 UTC — original publication time |
| `saved_at` | TEXT | ISO-8601 UTC — bookmark time; `NULL` when the source doesn't expose it |
| `unsaved_at` | TEXT | Set on soft delete |
| `is_saved` | INTEGER | 1 = active, 0 = unsaved (default 1) |
| `excerpt` / `content_text` | TEXT | `content_text` populated only by `get-content`/`get_post_content` |
| `image_url` | TEXT | Cover image (~98% populated live) |
| `audience` | TEXT | Raw Substack value (`everyone`, `only_paid`, …) |
| `is_paywalled` | INTEGER | Derived from `audience == "only_paid"` |
| `reading_time_minutes` | INTEGER | **Derived** from `word_count` (~200 wpm), never mapped |
| `word_count` | INTEGER | From `wordcount` (confirmed live at 100% on the unified payload) |
| `read_progress` | REAL | Current scroll position, 0.0–1.0; mapped from `post.read_progress` |
| `max_read_progress` | REAL | High-water reading mark, 0.0–1.0; drives `is_fully_read`/`read_state` |
| `is_viewed` | INTEGER | 1 = post was opened at least once (default 0) |
| `created_at` / `updated_at` | TEXT | Local record timestamps |

`is_fully_read` and `minutes_remaining` are **not columns** — they're Pydantic
`@computed_field` properties on `SavedPost`/`PostSummary`, derived from `max_read_progress`
(and `word_count`) at read time, same rationale as `reading_time_minutes`: a stored
derivative would go stale the moment the threshold or WPM assumption changed.

Indexes: `idx_posts_url`, `idx_posts_published_at DESC`, `idx_posts_saved_at DESC`,
`idx_posts_is_saved`, `idx_posts_audience`, `idx_posts_max_read_progress`.
`posts_fts` covers `title`, `publication_name`, `author_name`, `excerpt`, `content_text`.

`metadata_json` ("raw sanitized source JSON for future migrations") was specified in the
original plan, never populated, and **removed** during implementation. Databases created
before its removal may still carry the inert column; reads tolerate it because `SavedPost`
ignores unknown columns and `upsert_post` no longer references it.

### Table: `notes`

Identity is `substack_note_id TEXT NOT NULL UNIQUE`, **not** `url` (`url TEXT UNIQUE`,
optional) — a note may lack a stable permalink, and id-primary identity is what makes
reconciliation possible. Columns: `body_text` (NOT NULL DEFAULT ''), `body_raw`,
`body_format`, `author_name`/`author_handle`/`author_id`, `publication_name`/`publication_url`,
`posted_at`, `saved_at`, `unsaved_at`, `is_saved`, `is_restack`, `parent_note_id`,
`attachment_type`/`attachment_url`, `restacked_post_url`/`restacked_post_title`/
`restacked_publication_name`, `like_count`/`restack_count`/`reply_count`, `word_count`,
`created_at`/`updated_at`.

Indexes: `idx_notes_url`, `idx_notes_saved_at DESC`, `idx_notes_posted_at DESC`,
`idx_notes_is_saved`, `idx_notes_author`.
`notes_fts` covers `body_text`, `author_name`, `author_handle`, `publication_name`,
`restacked_post_title` — that column order appears seven times across the declaration and the
`notes_ai`/`notes_ad`/`notes_au` trigger `VALUES` clauses, and FTS5 external-content triggers
are positional: one wrong order corrupts the index silently. Review them as one unit.

Three deliberate deviations from `SavedPost`:

1. **`substack_note_id` required, `url` optional** — the inverse of posts.
2. **No `title`** — `body_preview` is computed in SQL (`substr(body_text, 1, 240)`) so the
   `NoteSummary(**dict(row))` pattern still holds without a derived column.
3. **`body_raw` + `body_format`** — the source body is stored verbatim beside the derived text,
   so improving the ProseMirror walker later needs no resync. Highest-value hedge in the schema.

Restacks use flat columns rather than a FK into `posts`: a restacked post is usually *not* in
the user's saved posts, so a FK would dangle or force phantom rows.

### Table: `sync_runs`

Records each run, including `reconciled_count` and `entity` (`'post'` | `'note'`, additive
column defaulting pre-existing rows to `'post'`). `entity` scopes `get_status()`'s
"last sync"/"last successful sync" queries so a notes sync is never reported as the posts'
last sync. `status` is one of `success`, `partial`, `auth_required`, `failed`.

---

## MCP Tool Suite

**Posts:** `search_saved_posts`, `list_saved_posts`, `get_saved_post`, `get_post_content`,
`save_post`, `unsave_post`, `list_publications`, `list_audiences`, `saved_posts_status`,
`sync_saved_posts(force)`.

**Notes:** `search_saved_notes`, `list_saved_notes`, `get_saved_note`, `get_note_content`,
`save_note`, `unsave_note`, `sync_saved_notes(force)`.

**Resources:** `substack://posts/{id}`, `substack://publications`, `substack://notes/{note_id}`.

`sync_saved_posts` stays posts-only so existing MCP clients are unaffected; the
both-entities default lives only on the CLI. `saved_posts_status` keeps its name and picks up
the note counts through the model. Write tools return `remote_confirmed: bool` plus a
`warning` key (save) or warning text appended to `message` (unsave).

## CLI Surface

```
init | login | serve | status
sync [--force] [--only posts|notes]      # default: both entities, one result block each
save URL | unsave URL_OR_ID | get-content URL_OR_ID [--no-cache]
list | search QUERY | publications | audiences
save-note URL | unsave-note URL_OR_ID | get-note URL_OR_ID [--no-cache]
list-notes | search-notes QUERY | note-authors
inspect-network [--authenticated/--anonymous] [--url] [--filter] [--max-body] [--out]
probe-api URL [--out]
compare-saved-apis [--out]
```

`sync` runs posts then notes; a failure in one entity must not suppress the other — run both,
report both, exit non-zero only if *both* failed. `--only posts` reproduces pre-notes
behaviour exactly.

---

## Decisions Ledger

### Posts (v1.0)

| Decision | Choice |
| --- | --- |
| `published_at` vs `saved_at` | Always distinct; `saved_at` comes from the source payload only, never fabricated from the sync moment |
| Unsaving | Soft delete (`is_saved = 0`, `unsaved_at` set); history retained |
| Reconciliation | Only on `--force`/full sync, from a complete remote set; never on an incremental sync |
| URL identity | Everything canonicalized through `canonicalize_url()` before storage or lookup |
| `audience` | Raw string stored verbatim; `is_paywalled` derived from it |
| `reading_time_minutes` | Derived from `word_count`, not mapped from a field — a wrong unit guess would persist a badly wrong value |
| Local vs remote | Local write always reflects user intent; unconfirmed remote actions surface as warnings, never as silent success |

### Saved notes (0.2.0)

| Decision | Choice |
| --- | --- |
| Storage | Separate `notes` table + `notes_fts`; `posts` untouched |
| Scope | sync + list + search, save by URL, unsave, fetch full body |
| Endpoint discovery | Capture first via `inspect-network`, then build — no hypothesis reached code before it was confirmed |
| CLI `sync` | Both entities by default; `--only` narrows |
| Naming | Flat `-note`-suffixed CLI commands, `_note` MCP tools |
| Identity | `substack_note_id`, not URL |
| DOM fallback | **None.** An unavailable notes API raises an error naming `inspect-network` |
| Sync code | `sync_saved_notes` duplicates rather than generalizes `sync_saved_posts` |
| Formatting | New `format_note_for_llm`; `format_post_for_llm` not generalized |

Why notes are their own entity: no title, no publication tier, no word count or reading time;
instead an author handle, a short body, engagement counts, and — for restacks — an attached
post. Forcing them into `posts` would leave most columns meaningless per row and require a
`kind` filter on every existing query.

Why `sync_saved_notes` duplicates instead of generalizing: a shared version needs
`fetch_page`, `parse`, `key_of`, `get_existing`, `upsert`, and `reconcile` injected — six
callables to abstract ~55 lines of straight-line loop, and a bug in it would break both
entities including the one already working in production. The loops also drift legitimately
(notes reconcile by id, posts by URL; notes have no DOM fallback). What *was* worth extracting
is `_build_sync_run()`.

Why no DOM fallback for notes: the notes card markup is entirely uncaptured, and since a
note's id *is* its identity, a DOM-scraped row without one could never reconcile. The posts
DOM path already degrades badly (no `saved_at`, no id, a localized relative date). The `/p/`
filter in the posts DOM scraper stays exactly as is — it is what keeps note cards out of the
posts result set.

### Unified reader API (0.3.0)

| Decision | Choice |
| --- | --- |
| Evidence | Capture the payload first via a browserless probe; no code decision before seeing it |
| Migration rule | Replace only on parity-or-better; on any must-have miss, keep legacy primary |
| Paginator | Standalone unified fetcher; **not** a generalization of the legacy cursor loop |
| Rollback | `SUBSTACK_SAVED_POSTS_SOURCE` env var, not a code revert |
| `filter=all` | Out of scope — posts and notes stay separate entities and separate sync runs |

The legacy cursor loop (`after=<ISO saved_at>` + `more` flag + controllable `page_size`) and
the unified one (opaque `nextCursor`) are genuinely different pagination models; the parts
worth sharing (`_reader_api_get()`, `_retry_after_seconds()`) were already factored out. A
parameterized loop bent around two incompatible models would be worse than two honest loops.

### Reading progress (0.4.0)

| Decision | Choice | Why |
| --- | --- | --- |
| "Fully read" test | `max_read_progress >= 0.95`, overridable via `SUBSTACK_SAVED_FULLY_READ_THRESHOLD` | Real values like `0.9999`/`0.9867`/`0.9822` exist; an exact `1.0` test would call finished posts unfinished forever |
| Driving field | `max_read_progress`, not `read_progress` | The high-water mark is "have I read this"; current position drops back on scroll-up. Both stored since they genuinely differ (5/72 sampled posts) |
| Filter shape | `read_state` enum: `unread` / `in_progress` / `finished` / `started` | Self-documenting to an LLM caller; a numeric `--min-progress` param would leak the threshold convention |
| `is_fully_read` / `minutes_remaining` | Derived (Pydantic `@computed_field`), never stored | Same rationale as `reading_time_minutes`; progress and the threshold both change, a stored derivative would go stale |
| `is_viewed` | Stored as its own column | 14/72 sampled posts were opened but never scrolled — indistinguishable from "never opened" without it |
| Staleness | Documented; `sync --force` refreshes everything | Progress changes without `saved_at` changing, and incremental sync's early-stop (`MAX_CONSECUTIVE_MATCHES = 3`) means older posts go stale between full syncs |
| Notes | Out of scope | Confirmed absent from the notes payload; `SavedNote`'s docstring already records notes have no reading-time concept |

Evidence: a live probe of a real account (72 saved posts, 6 pages of
`GET /api/v1/reader/saved?filter=posts`) found `post.read_progress`/`post.max_read_progress`
(floats 0.0–1.0) and `post.is_viewed` (bool) on 100% of items, identically mirrored under
`post.inboxItem` (so no need to read that nested object), and present on the legacy
`/api/v1/reader/posts?inboxType=saved` payload too — the same three-source `auto` chain in
0.3.0 supplies them on both API sources; the DOM fallback does not. Distribution: 30 unstarted,
30 in progress, 3 at 0.90–0.99, 9 at ≥1.0.

`COALESCE(max_read_progress, 0)` in every `read_state` predicate is load-bearing, not
defensive styling: after the additive migration every pre-existing row is `NULL`, and a bare
`max_read_progress = 0` comparison evaluates to `NULL` (not true) in SQL — which would
silently exclude the entire legacy cache from every read_state filter until a resync.
`COALESCE` puts those rows (and DOM-sourced posts, which never carry progress) in `unread`.

---

## Evidence That Settled the Open Questions

### Notes endpoints — captured live, not guessed

| Question | Confirmed answer |
| --- | --- |
| List endpoint | `GET /api/v1/reader/saved?filter=notes` (also `all`/`posts`) |
| Pagination | Opaque `nextCursor` resubmitted as `cursor=`; `limit=` has **no effect** |
| Response shape | `{"items": [...], "nextCursor": <token>|null}`; item = `{entity_key: "c-<id>", publication, post, comment}` |
| Note id | `comment.id`, also embedded in the permalink |
| Body | `comment.body` (flattened plain text) + `comment.body_json` (ProseMirror doc) |
| Saved timestamp | **Absent** — only `comment.is_saved: bool`. `SavedNote.saved_at` is always `None` |
| Permalink | `https://substack.com/@<handle>/note/c-<id>` (confirmed via click tracking) |
| Single note | `GET /api/v1/reader/comment/{id}` → `{"item": {"comment": {...}}}` |
| Save / unsave | `POST` / `DELETE https://substack.com/api/v1/note/c-{id}/save` — id in the path, **no request body** |
| Browser needed? | No. Notes are plain authenticated `p.request` calls; no page, no `window._preloads` |
| Restack shape | Top-level `publication`/`post` populated instead of `null` — structurally solid (shared with `filter=posts`) but **not confirmed by a live example** |

The missing bookmark timestamp is load-bearing: `sync_saved_notes`'s incremental early-stop
compares "already saved locally" (`existing.is_saved == 1`) rather than a `saved_at` match the
way posts does.

### Unified vs legacy posts API — the decision gate

The gate was written down *before* the numbers came in, so the decision couldn't be
reverse-engineered from them. Must-haves (any failure disqualifies unified as primary): same
post set as legacy, a real bookmark timestamp on ~100% of items, newest-saved-first ordering,
`post.id` + `canonical_url` on ~100%, `title` + `post_date` + publication name on ~100%.
Nice-to-haves (weighed, not fatal): `audience`, `cover_image`, `description`/`subtitle`, word
count.

Result against a real ~1000-post account: **unified is a strict superset.** 1079 posts vs.
legacy's 985 — only 1 legacy-only, 95 unified-only; `saved_at`/`id`/`canonical_url`/`title`/
`post_date`/`audience`/`wordcount` all at 100%, correctly ordered newest-first. `wordcount`
also resolved the previously-unconfirmed word-count field guess. **Outcome A:** unified
primary, legacy automatic fallback, DOM last.

A follow-up shadow sync (real `sync --force`, throwaway DBs, same live session) explained the
count gap: legacy's own count is unstable run-to-run (985, then 1080 across consecutive
syncs), while unified held steady at 1079 both times. That instability was a real bug —
`_fetch_all_saved_via_reader_api()` returned a partial list with no "incomplete" signal when a
mid-pagination 429 survived every retry.

Two process notes worth keeping:

- The first parity run was **invalidated by rate limiting** immediately after a large legacy
  fetch. `compare-saved-apis` now pauses and retries harder around the second fetch for
  exactly this reason. Expect this trap on any back-to-back full-library fetch.
- `inspect-network` had been silently capturing **zero** response bodies — 0 of 108 records in
  a full session — because `response.text()` inside a sync-API `page.on("response")` handler
  raises every time and a guarded `except` wrote `null` without a word. The fix routes bodies
  through `context.route()` + `route.fetch()` + `route.fulfill()`, and an unreadable body now
  logs a visible warning. Silence in a discovery tool costs entire sessions.

### The silent-truncation bug (fixed)

The mid-pagination silent truncation turned out to be shared by **all three** cursor-paginated
fetchers (legacy posts, unified posts, and — pre-existing — notes). Each still returns its
partial list rather than throwing away progress, but now also sets a per-source truncation
flag; `is_posts_fetch_truncated()`/`is_notes_fetch_truncated()` report it. On a `--force` sync
a truncated fetch **skips reconciliation entirely** rather than soft-deleting items that are
merely absent from an incomplete list, and the `SyncRun` gets `status = "partial"` with an
explanatory `error_message`. The CLI treats `partial` as a qualified success (yellow, not red)
and prints the warning.

A 429 that survives all retries is deliberately treated as "unavailable" (partial list, or
`None` → next source) rather than as an empty success, so rate-limiting can never masquerade
as "you have no saved posts."

---

## Latent Issues Register

1. **FIXED.** `upsert_post` crash path — it found `existing` by `substack_post_id` *or* `url`
   but wrote `INSERT … ON CONFLICT(url)`, so a post found by id whose canonical URL had changed
   (slug rename, custom-domain migration) tripped the `substack_post_id UNIQUE` constraint on
   the fresh INSERT. Now uses the explicit lookup-then-branch UPDATE/INSERT pattern that
   `upsert_note()` uses. Regression test: `test_upsert_post_by_id_updates_existing_on_url_change`.
2. **FIXED.** `search_posts`'s LIKE fallback silently dropped filters — hardcoded `is_saved = 1`,
   honored only `audience`, discarded `publication`, both date ranges, and `is_saved_only=False`.
   Now shares WHERE-clause construction with the FTS branch via `_post_search_filters()`,
   mirroring `_note_search_filters()`. Regression test: `test_search_posts_fallback_keeps_filters`.
3. **FIXED.** `get_status` read `sync_runs` unscoped — harmless with one entity, actively wrong
   with two. Now scoped by `entity`.
4. **OPEN, not urgent.** `reconcile_unsaved_posts` pulls every active row into Python. Fine at
   `max_posts=2000`; if volumes ever exceed ~10k, switch both entities to a temp-table
   `LEFT JOIN`.
5. **FIXED.** `_fetch_saved_posts_page_impl` couldn't take a Playwright double, leaving its
   caching/slicing/fallback branch untested. Now accepts an optional `playwright_instance`.
   Regression test: `test_fetch_saved_posts_page_slices_cache`.
6. **FIXED.** `inspect-network` was unauthenticated (no `storage_state`), so `/saved` rendered
   the logged-out page and the user's real traffic never fired.
7. **FIXED.** `sync_saved_posts` hand-built `SyncRun` three times and stamped `started_at` at
   *finish* time, making `started_at == completed_at` on every returned run. `_build_sync_run()`
   fixed both.
8. **FIXED.** All three cursor paginators silently truncated on a surviving 429 — see above.

---

## Testing Conventions

- Database/sync tests pass an explicit `db_path=` (`tmp_path`); CLI and MCP tests set
  `SUBSTACK_SAVED_DB_PATH` via `monkeypatch` so a developer's real cache is never touched.
- Sync tests inject a `SubstackSavedPostsClient` substitute (overriding only `__init__` and the
  fetch method) rather than opening Playwright or contacting Substack.
- Client tests use `MockPlaywrightForImpl` / `MockApiRequestContext` with the three-layer
  `_impl` + `playwright_instance` injection pattern.
- **When refactoring a fetch path, the existing reader-API tests re-run unedited.** They are
  the proof the legacy path wasn't disturbed; editing them forfeits the safety net.

Cases carrying weight beyond the obvious round-trips: `test_upsert_note_by_id_updates_existing`
(same id, changed URL → one row), `test_search_notes_fallback_keeps_filters`,
`test_get_status_scopes_sync_runs_by_entity`, `test_init_db_idempotent_on_existing_posts_db`,
`test_sync_notes_force_reconciles` / `..._incremental_never_reconciles`,
`test_parse_remote_note_missing_id_is_skipped`, `test_parse_remote_note_saved_at_never_fabricated`,
`test_unsave_note_without_id_returns_not_found` (proves the no-DOM-fallback decision),
`test_notes_api_unavailable_raises_clear_error` (message must name `inspect-network`),
`test_fetch_saved_posts_page_prefers_configured_source` / `..._falls_back_when_primary_unavailable`
/ `..._falls_back_to_dom_when_both_unavailable`.

---

## Verification Recipes

Local, after any change:

```bash
uv sync --extra dev
uv run python -m pytest
uv run ruff check .
uv lock && prek run --all-files
```

Schema/migration check on a throwaway DB — never the real cache:

```bash
SUBSTACK_SAVED_DB_PATH=/tmp/nt.sqlite uv run substack-saved-mcp init
sqlite3 /tmp/nt.sqlite ".schema notes" ".schema notes_fts" "PRAGMA table_info(sync_runs);"
```

Run the migration against a **copy** of a real pre-notes DB: posts counts must be identical
before and after, and `notes`/`notes_fts`/`sync_runs.entity` must now exist.

Endpoint rediscovery, when Substack's frontend changes:

```bash
# discover unknown endpoints by watching a live session
uv run substack-saved-mcp inspect-network --authenticated --out /tmp/capture.jsonl
python3 -c "import json;print(sum(1 for l in open('/tmp/capture.jsonl') if json.loads(l).get('response_body')))"
#   ^ must be > 0 — the response-body regression check

# verify a known endpoint, browserless
uv run substack-saved-mcp probe-api \
  'https://substack.com/api/v1/reader/saved?filter=posts' --out /tmp/unified-posts.json

# re-score the posts source parity gate
uv run substack-saved-mcp compare-saved-apis --out /tmp/parity.json
```

Shadow sync — never against the real cache:

```bash
cp ~/.local/share/substack-saved-mcp/saved_posts.sqlite /tmp/baseline.sqlite
for src in legacy unified; do
  SUBSTACK_SAVED_POSTS_SOURCE=$src SUBSTACK_SAVED_DB_PATH=/tmp/$src.sqlite \
    uv run substack-saved-mcp sync --only posts --force
done
sqlite3 /tmp/legacy.sqlite 'select count(*),count(saved_at),count(audience),count(image_url) from posts'
sqlite3 /tmp/unified.sqlite 'select count(*),count(saved_at),count(audience),count(image_url) from posts'
```

Row counts must match, and unified's `saved_at`/`audience`/`image_url` coverage must be no
worse than legacy's. Live end-to-end after `login`:

```bash
uv run substack-saved-mcp sync --only posts --force   # reconciled_count must be 0
uv run substack-saved-mcp list --limit 5              # saved_at ordering intact
uv run substack-saved-mcp search "<known term>" --saved-after 2026-01-01

uv run substack-saved-mcp sync --only notes --force   # fetched == upserted, reconciled 0
uv run substack-saved-mcp list-notes --limit 5
uv run substack-saved-mcp search-notes "<known word>"
uv run substack-saved-mcp get-note <note-url>         # body renders as readable text
uv run substack-saved-mcp save-note <an unsaved note URL>
uv run substack-saved-mcp sync --only notes --force   # the new note appears
uv run substack-saved-mcp unsave-note <that URL>
uv run substack-saved-mcp sync --only notes --force   # reconciled_count reflects it

uv run substack-saved-mcp status                      # both entities' counts and sync rows
uv run substack-saved-mcp sync                        # both entities in one run
```

A non-zero `reconciled_count` on a first force sync means the source returned a short list and
posts were wrongly soft-deleted — stop and restore from `/tmp/baseline.sqlite`.

Data-quality gates after a live notes sync: `body_text` non-empty on ~100% of notes (empty ⇒
the body-format assumption is wrong, and `body_raw` reveals the real shape without a resync);
`substack_note_id` unique and non-null on 100%. `saved_at` is expected `NULL` on every note —
the endpoint doesn't expose it.

MCP smoke: `uv run substack-saved-mcp serve`, confirm the posts and notes tools plus all three
resources are listed.

---

## Definition of Done (current scope — met)

- User authenticates via `login`; sync maintains the local cache for both entities.
- FTS5 search with date-range filters over posts (`published_at`, `saved_at`) and notes
  (`posted_at`), plus publication/audience/author filters.
- `save`/`unsave` work remotely and locally for posts and notes, with confirmation status
  surfaced rather than assumed.
- `published_at`/`posted_at` stay distinct from `saved_at`, and `saved_at` is never fabricated.
- A truncated fetch never soft-deletes real items; it reports `partial` instead.
- All read/search/write/sync paths covered by fixture-backed tests, green with `ruff` clean.

## Out of Scope / Possible Next Steps

- `filter=all` — fetching posts and notes in one paginated pass. A real efficiency win now
  that both live on the unified endpoint, but it would merge two entities that are
  deliberately separate (separate tables, separate `sync_runs` rows). Revisit only as an
  explicit decision.
- Temp-table `LEFT JOIN` reconciliation for both entities, if a library ever exceeds ~10k items
  (Latent issue #4).
- Collapsing the unified posts and notes cursor loops, *if* their loop bodies prove identical
  enough — a call to make with the real code in front of you, not in advance.
- Restack-shape confirmation for notes: the shape is inferred from `filter=posts`, never seen
  in a live example. First real restack encountered is the chance to verify it.
