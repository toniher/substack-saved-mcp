"""Playwright client for Substack authentication, saved post extractions, and write operations."""

import concurrent.futures
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from substack_saved_mcp.config import get_browser_dir, get_storage_state_path
from substack_saved_mcp.models import SavedPost
from substack_saved_mcp.url_utils import canonicalize_url

logger = logging.getLogger(__name__)


def _run_playwright_sync(func, *args, **kwargs):
    """Execute a function using Playwright Sync API safely, dispatching to a worker thread if an asyncio loop is active."""
    try:
        import asyncio
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(func, *args, **kwargs)
            return future.result()
    return func(*args, **kwargs)


class SubstackClientError(Exception):
    """Base exception for Substack client operations."""
    pass


class AuthRequiredError(SubstackClientError):
    """Raised when Substack session is expired, invalid, or unauthenticated."""
    pass


def perform_interactive_login(browser_dir: Path | None = None) -> Path:
    """Launch a visible browser window for the user to log in to Substack.

    Saves storage state (cookies, local storage) to storage_state.json once complete.
    """
    return _run_playwright_sync(_perform_interactive_login_impl, browser_dir=browser_dir)


def _perform_interactive_login_impl(browser_dir: Path | None = None) -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SubstackClientError("Playwright is not installed. Please run 'pip install playwright && playwright install'") from None

    from substack_saved_mcp.config import ensure_app_dirs
    ensure_app_dirs()
    target_dir = browser_dir or get_browser_dir()
    state_file = target_dir / "storage_state.json"

    print("Opening Substack sign-in window...")
    print("Please log in to your Substack account in the opened browser window.")
    print("Once logged in and viewing your feed or saved posts, press ENTER in this terminal to save session.\n")

    with sync_playwright() as p:
        # Launch visible browser
        browser = p.chromium.launch(headless=False)
        context_kwargs = {}
        if state_file.exists():
            context_kwargs["storage_state"] = str(state_file)

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.goto("https://substack.com/sign-in")

        input("--> Press ENTER here AFTER you have successfully completed sign-in in the browser window: ")

        # Verify navigation to saved page or authenticated state
        try:
            if page.is_closed():
                pages = [p for p in context.pages if not p.is_closed()]
                if pages:
                    page = pages[0]
                else:
                    page = context.new_page()
            page.goto("https://substack.com/saved", wait_until="domcontentloaded", timeout=10000)
        except Exception as err:
            logger.warning(f"Notice during login verification: {err}")

        try:
            context.storage_state(path=str(state_file))
        except Exception as err:
            logger.warning(f"Notice saving storage state: {err}")
            
        try:
            browser.close()
        except Exception:
            pass

    # Restrict permissions on session storage state file
    import os
    if os.name == "posix" and state_file.exists():
        try:
            state_file.chmod(0o600)
        except Exception:
            pass

    print(f"--> Authentication state saved successfully to {state_file}")
    return state_file


class SubstackSavedPostsClient:
    """Client for fetching and managing saved Substack posts via storage_state.json."""

    def __init__(self, storage_state_path: Path | None = None):
        self.state_path = storage_state_path or get_storage_state_path()
        self._dom_cache: list[dict[str, Any]] | None = None
        self._api_cache: list[dict[str, Any]] | None = None
        self._api_failed: bool = False

    def reset_cache(self) -> None:
        """Reset cached posts extraction (reader API and DOM fallback)."""
        self._dom_cache = None
        self._api_cache = None
        self._api_failed = False

    def _ensure_authenticated(self) -> None:
        """Check if storage state exists."""
        if not self.state_path.exists():
            raise AuthRequiredError(
                f"No saved Substack session found at {self.state_path}. "
                "Please run 'substack-saved-mcp login' first to authenticate."
            )

    def fetch_saved_posts_page(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Fetch a page of saved posts from Substack using Playwright request context."""
        return _run_playwright_sync(self._fetch_saved_posts_page_impl, limit=limit, offset=offset)

    def _fetch_saved_posts_page_impl(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        self._ensure_authenticated()

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SubstackClientError("Playwright is not installed.") from None

        with sync_playwright() as p:
            # Prefer the reader inbox API: it returns the real bookmark timestamp
            # (saved_at) and an ISO publication date (post_date) per post. The full
            # saved list is fetched once via cursor pagination and cached, then sliced
            # by offset so the caller keeps a simple offset/limit interface.
            if self._api_cache is None and not self._api_failed:
                api_context = p.request.new_context(storage_state=str(self.state_path))
                self._api_cache = self._fetch_all_saved_via_reader_api(api_context)
                if self._api_cache is None:
                    self._api_failed = True
                    logger.warning("Reader inbox API unavailable; falling back to DOM extraction.")

            if self._api_cache is not None:
                return self._api_cache[offset : offset + limit]

            # Fallback: headless DOM extraction on https://substack.com/saved
            return self._fetch_via_dom(offset=offset, limit=limit, playwright_instance=p)

    @staticmethod
    def _retry_after_seconds(res: Any, attempt: int, cap: float = 30.0) -> float:
        """Compute how long to wait before retrying a throttled/failed reader-API request.

        Honors the ``Retry-After`` response header when it's an integer number of
        seconds (the form Substack/most APIs send), clamped to ``cap`` so a hostile
        or absurd value can't hang the sync. Falls back to capped exponential
        backoff (0.5s, 1s, 2s, ...) when the header is absent or unparseable.
        """
        headers = getattr(res, "headers", None) or {}
        raw = headers.get("retry-after")
        if raw is not None:
            try:
                return min(float(int(str(raw).strip())), cap)
            except (ValueError, TypeError):
                pass  # Not an integer (possibly an HTTP-date); use backoff instead.
        return min(0.5 * (2 ** attempt), cap)

    def _reader_api_get(
        self, api_context: Any, url: str, max_retries: int = 3, sleep_func=time.sleep
    ) -> Any:
        """GET a reader-API URL, retrying on 429/5xx with Retry-After-aware backoff.

        Only transient statuses (429 Too Many Requests and 5xx server errors) are
        retried. Auth failures (401/403) and other 4xx are returned unretried so the
        caller's existing handling (raise ``AuthRequiredError`` / fall back) applies.
        """
        res = api_context.get(url)
        attempts = 0
        while (res.status == 429 or 500 <= res.status < 600) and attempts < max_retries:
            delay = self._retry_after_seconds(res, attempts)
            logger.warning(
                f"Reader inbox API returned {res.status}; backing off {delay}s "
                f"before retry {attempts + 1}/{max_retries}."
            )
            sleep_func(delay)
            attempts += 1
            res = api_context.get(url)
        return res

    def _fetch_all_saved_via_reader_api(
        self, api_context: Any, page_size: int = 20, max_posts: int = 2000,
        max_retries: int = 3, sleep_func=time.sleep,
    ) -> list[dict[str, Any]] | None:
        """Fetch the full saved list from the reader inbox API via cursor pagination.

        Returns a list of enriched post dicts (each carrying its real ``saved_at``,
        ISO ``post_date``, and an attached ``publication`` object), or ``None`` if the
        endpoint is unavailable so the caller can fall back to DOM extraction. Raises
        ``AuthRequiredError`` when the session is expired. Transient 429/5xx responses
        are retried with ``Retry-After``-aware backoff (see ``_reader_api_get``); a
        429 that survives all retries is treated as "unavailable" (partial list or
        DOM fallback) rather than as silent success.
        """
        from urllib.parse import quote

        all_posts: list[dict[str, Any]] = []
        seen_urls = set()
        # "after=X" returns posts saved before X (newest first); a far-future sentinel
        # yields the first (most recently saved) page. Substack always sends this param.
        cursor: str = "2999-01-01T00:00:00.000Z"

        while len(all_posts) < max_posts:
            url = (
                f"https://substack.com/api/v1/reader/posts?inboxType=saved"
                f"&limit={page_size}&after={quote(cursor)}"
            )

            res = self._reader_api_get(
                api_context, url, max_retries=max_retries, sleep_func=sleep_func
            )
            if res.status in (401, 403) or "sign-in" in res.url:
                raise AuthRequiredError(
                    "Substack session has expired or is invalid. Please run 'substack-saved-mcp login'."
                )
            if not res.ok:
                # Unavailable on the first page → signal fallback; mid-stream → keep what we have.
                return all_posts if all_posts else None

            try:
                data = res.json()
            except Exception as e:
                logger.warning(f"JSON parsing error from reader inbox API: {e}.")
                return all_posts if all_posts else None

            posts = data.get("posts") or []
            if not posts:
                break

            pubs_by_id = {pub.get("id"): pub for pub in (data.get("publications") or [])}
            before_len = len(all_posts)
            page_min_saved: str | None = None

            for post in posts:
                saved_at = post.get("saved_at")
                if saved_at and (page_min_saved is None or saved_at < page_min_saved):
                    page_min_saved = saved_at

                # Attach publication object and author so parse_remote_post can read them.
                pub = pubs_by_id.get(post.get("publication_id"))
                if pub:
                    post["publication"] = pub
                bylines = post.get("publishedBylines") or []
                if bylines and isinstance(bylines[0], dict) and bylines[0].get("name"):
                    post.setdefault("author_name", bylines[0]["name"])

                clean = canonicalize_url(post.get("canonical_url") or "")
                if clean and clean not in seen_urls:
                    seen_urls.add(clean)
                    all_posts.append(post)

            # Stop when the server says there is no more, we made no progress, or the
            # cursor did not advance (guards against an inclusive-boundary infinite loop).
            if not data.get("more") or not page_min_saved or len(all_posts) == before_len:
                break
            if page_min_saved == cursor:
                break
            cursor = page_min_saved

        return all_posts

    def _fetch_via_dom(self, offset: int = 0, limit: int = 20, playwright_instance: Any = None) -> list[dict[str, Any]]:
        """Fallback method: Render https://substack.com/saved in headless browser and extract post cards."""
        if playwright_instance is not None:
            return self._fetch_via_dom_impl(offset=offset, limit=limit, playwright_instance=playwright_instance)
        return _run_playwright_sync(
            self._fetch_via_dom_impl,
            offset=offset,
            limit=limit,
            playwright_instance=playwright_instance,
        )

    def _fetch_via_dom_impl(self, offset: int = 0, limit: int = 20, playwright_instance: Any = None) -> list[dict[str, Any]]:
        self._ensure_authenticated()

        def _do_fetch(p):
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(self.state_path))
            page = context.new_page()

            page.goto("https://substack.com/saved", wait_until="domcontentloaded", timeout=15000)

            if "sign-in" in page.url or page.locator("text=Sign in").count() > 0:
                browser.close()
                raise AuthRequiredError("Substack session has expired or is invalid. Please run 'substack-saved-mcp login'.")

            # Extract post elements from DOM with infinite scrolling
            seen_urls = set()
            results: list[dict[str, Any]] = []
            target_count = max(offset + limit, 1000)
            max_stale_scrolls = 6
            stale_scrolls = 0

            while len(results) < target_count and stale_scrolls < max_stale_scrolls:
                prev_count = len(results)
                cards = page.locator("div.reader2-post-container").all()

                for card in cards:
                    try:
                        link = card.locator("a[href*='/p/']").first
                        if link.count() == 0:
                            continue
                        href = link.get_attribute("href")
                        if not href or "/p/" not in href:
                            continue

                        clean = canonicalize_url(href)
                        if clean in seen_urls:
                            continue
                        seen_urls.add(clean)

                        parsed = urlparse(clean)

                        def _card_text(card: Any, selector: str) -> str | None:
                            loc = card.locator(selector).first
                            if loc.count() > 0:
                                text = loc.inner_text().strip()
                                return text or None
                            return None

                        title = _card_text(card, ".reader2-post-title")
                        pub_name = _card_text(card, ".pub-name")
                        excerpt = _card_text(card, ".reader2-paragraph")
                        # Localized relative display string (e.g. "1 de jul.", "3h");
                        # Substack does not expose a machine-readable ISO date here.
                        published_display = _card_text(card, ".inbox-item-timestamp")

                        # ".reader2-item-meta" reads like "Author∙8 min read"; take the author part.
                        meta_text = _card_text(card, ".reader2-item-meta")
                        author = meta_text.split("∙")[0].strip() if meta_text else None

                        fallback_pub = parsed.netloc.split(".")[0].capitalize()

                        results.append({
                            "_dom": True,
                            "canonical_url": clean,
                            "title": title or pub_name or fallback_pub,
                            "publication_name": pub_name or fallback_pub,
                            "publication_url": f"{parsed.scheme}://{parsed.netloc}",
                            "author_name": author,
                            "excerpt": excerpt,
                            "saved_at": None,
                            "published_at": published_display,
                        })
                    except Exception:
                        continue

                if len(results) >= target_count:
                    break

                if len(results) == prev_count:
                    stale_scrolls += 1
                else:
                    stale_scrolls = 0

                # Scroll down and attempt clicking any load/more buttons
                try:
                    more_btn = page.locator("button:has-text('Load more'), button:has-text('Show more'), button:has-text('More')").first
                    if more_btn.count() > 0 and more_btn.is_visible():
                        more_btn.click(timeout=1000)
                except Exception:
                    pass

                page.evaluate("window.scrollBy(0, -100)")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)

            browser.close()
            return results

        if self._dom_cache is None:
            if playwright_instance is not None:
                self._dom_cache = _do_fetch(playwright_instance)
            else:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    self._dom_cache = _do_fetch(p)

        return self._dom_cache[offset : offset + limit]

    def _click_bookmark_toggle(self, page: Any) -> str:
        """Click the save/bookmark toggle button on a post page and report confidence.

        Substack's bookmark button markup isn't officially documented (unlike the
        saved-list card markup captured from a real page for DOM extraction), so
        this can only detect whether *something* about the button's rendered
        state changed after the click — not that the intended direction (save
        vs. unsave) is what actually happened.

        Returns "confirmed" (button found, clicked, and its aria-label/aria-pressed/
        class fingerprint changed), "unconfirmed" (found and clicked but no change
        was detectable), "not_found" (no matching button on the page), or
        "click_failed" (found but the click itself raised).
        """
        btn = page.locator("button[aria-label*='bookmark' i], button[aria-label*='save' i]").first
        if btn.count() == 0:
            return "not_found"

        def _fingerprint():
            try:
                return (btn.get_attribute("aria-label"), btn.get_attribute("aria-pressed"), btn.get_attribute("class"))
            except Exception:
                return None

        before = _fingerprint()
        try:
            btn.click(timeout=3000)
        except Exception:
            return "click_failed"

        try:
            page.wait_for_timeout(300)
        except Exception:
            pass

        after = _fingerprint()
        if before is not None and after is not None and before != after:
            return "confirmed"
        return "unconfirmed"

    def save_post(self, url: str) -> tuple[SavedPost, str]:
        """Bookmark a post on Substack remotely and return (extracted metadata, confirmation status).

        Extracts the post's numeric ID and rich metadata from ``window._preloads``
        (server-rendered into every post page) and, when found, calls the real
        endpoint captured via `inspect-network` (``POST
        https://substack.com/api/v1/posts/saved`` with body ``{"post_id": ...}``)
        directly. Falls back to the best-effort DOM click (see
        ``_click_bookmark_toggle``) if the ID can't be extracted or that call
        doesn't confirm; confirmation is "confirmed", "unconfirmed", "not_found",
        or "click_failed".
        """
        return _run_playwright_sync(self._save_post_impl, url=url)

    def _save_post_impl(self, url: str, playwright_instance: Any = None) -> tuple[SavedPost, str]:
        self._ensure_authenticated()
        clean_url = canonicalize_url(url)

        def _do_save(p):
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(self.state_path))
            page = context.new_page()

            page.goto(clean_url, wait_until="domcontentloaded")

            if "sign-in" in page.url:
                browser.close()
                raise AuthRequiredError("Session expired during save. Please run 'substack-saved-mcp login'.")

            preloads = None
            try:
                preloads = page.evaluate("() => window._preloads")
            except Exception:
                pass

            post_obj = (preloads or {}).get("post") or {}
            pub_obj = (preloads or {}).get("pub") or {}
            post_id = post_obj.get("id")

            parsed = urlparse(clean_url)
            title = post_obj.get("title") or page.title().split("|")[0].strip() or "Substack Post"
            pub_name = pub_obj.get("name") or parsed.netloc.split(".")[0].capitalize()
            excerpt = post_obj.get("description") or post_obj.get("subtitle")
            published_at = post_obj.get("post_date")
            audience = post_obj.get("audience")

            toggle_status = self._click_bookmark_toggle(page)

            # Direct API call, keyed by the real numeric post_id, as an independent
            # (and generally more reliable) confirmation channel than the DOM click.
            api_confirmed = False
            if post_id is not None:
                try:
                    api_context = p.request.new_context(storage_state=str(self.state_path))
                    api_response = api_context.post(
                        "https://substack.com/api/v1/posts/saved", data={"post_id": post_id}
                    )
                    api_confirmed = bool(getattr(api_response, "ok", False))
                except Exception:
                    pass

            # Save updated storage state
            context.storage_state(path=str(self.state_path))
            browser.close()

            confirmation = "confirmed" if (api_confirmed or toggle_status == "confirmed") else toggle_status
            saved_post = SavedPost(
                substack_post_id=str(post_id) if post_id is not None else None,
                url=clean_url,
                title=title,
                publication_name=pub_name,
                publication_url=f"{parsed.scheme}://{parsed.netloc}",
                excerpt=excerpt,
                published_at=published_at,
                audience=audience,
                is_paywalled=1 if audience == "only_paid" else 0,
                is_saved=1,
            )
            return saved_post, confirmation

        if playwright_instance is not None:
            return _do_save(playwright_instance)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return _do_save(p)

    def fetch_post_content(self, url: str) -> dict[str, Any]:
        """Fetch a post's full body HTML by visiting its page.

        Reads ``window._preloads.post.body_html`` (the same server-rendered
        blob already relied on by ``save_post`` for title/audience/description),
        which is Substack's field name for full post content — ``parse_remote_post``
        already expects a ``body_html`` key from the API for this reason. Returns a
        dict with ``body_html`` (``None`` if not found on the page, e.g. the embed
        format changed or the post is paywalled beyond this account's access) plus
        the post's ``title`` and ``audience`` as read from the same blob.
        """
        return _run_playwright_sync(self._fetch_post_content_impl, url=url)

    def _fetch_post_content_impl(self, url: str, playwright_instance: Any = None) -> dict[str, Any]:
        self._ensure_authenticated()
        clean_url = canonicalize_url(url)

        def _do_fetch(p):
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(self.state_path))
            page = context.new_page()

            page.goto(clean_url, wait_until="domcontentloaded")

            if "sign-in" in page.url:
                browser.close()
                raise AuthRequiredError("Session expired while fetching content. Please run 'substack-saved-mcp login'.")

            preloads = None
            try:
                preloads = page.evaluate("() => window._preloads")
            except Exception:
                pass

            browser.close()

            post_obj = (preloads or {}).get("post") or {}
            return {
                "body_html": post_obj.get("body_html"),
                "title": post_obj.get("title"),
                "audience": post_obj.get("audience"),
            }

        if playwright_instance is not None:
            return _do_fetch(playwright_instance)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return _do_fetch(p)

    def unsave_post(self, url: str, post_id: int | None = None) -> str:
        """Unbookmark a post on Substack remotely; returns a confirmation status.

        When ``post_id`` (Substack's numeric post ID, i.e. ``SavedPost.substack_post_id``)
        is known — normally the case for any post that has been through a `sync` —
        this calls the real endpoint captured via `inspect-network`
        (``DELETE https://substack.com/api/v1/posts/saved`` with body
        ``{"post_id": ...}``) directly, without touching the DOM, and returns
        "confirmed" on an ok response. If that's unavailable or fails, or
        ``post_id`` is unknown, this falls back to the previous best-effort DOM
        click — see ``_click_bookmark_toggle`` for the meaning of its statuses.
        """
        return _run_playwright_sync(self._unsave_post_impl, url=url, post_id=post_id)

    def _unsave_post_impl(self, url: str, post_id: int | None = None, playwright_instance: Any = None) -> str:
        self._ensure_authenticated()
        clean_url = canonicalize_url(url)

        def _do_unsave(p):
            if post_id is not None:
                try:
                    api_context = p.request.new_context(storage_state=str(self.state_path))
                    response = api_context.delete(
                        "https://substack.com/api/v1/posts/saved",
                        data={"post_id": post_id},
                    )
                    if getattr(response, "ok", False):
                        return "confirmed"
                except Exception:
                    pass
                # Falls through to the DOM click below if the direct API call didn't confirm.

            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(self.state_path))
            page = context.new_page()

            page.goto(clean_url, wait_until="domcontentloaded")

            if "sign-in" in page.url:
                browser.close()
                raise AuthRequiredError("Session expired during unsave. Please run 'substack-saved-mcp login'.")

            toggle_status = self._click_bookmark_toggle(page)

            # Save updated state
            context.storage_state(path=str(self.state_path))
            browser.close()
            return toggle_status

        if playwright_instance is not None:
            return _do_unsave(playwright_instance)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return _do_unsave(p)
