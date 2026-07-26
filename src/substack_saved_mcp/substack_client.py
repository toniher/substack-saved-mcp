"""Playwright client for Substack authentication, saved post extractions, and write operations."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from substack_saved_mcp.config import get_browser_dir, get_storage_state_path
from substack_saved_mcp.models import SavedPost
from substack_saved_mcp.url_utils import canonicalize_url

logger = logging.getLogger(__name__)


class SubstackClientError(Exception):
    """Base exception for Substack client operations."""
    pass


class AuthRequiredError(SubstackClientError):
    """Raised when Substack session is expired, invalid, or unauthenticated."""
    pass


def perform_interactive_login(browser_dir: Optional[Path] = None) -> Path:
    """Launch a visible browser window for the user to log in to Substack.

    Saves storage state (cookies, local storage) to storage_state.json once complete.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SubstackClientError("Playwright is not installed. Please run 'pip install playwright && playwright install'")

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
        page.goto("https://substack.com/saved", wait_until="domcontentloaded")

        context.storage_state(path=str(state_file))
        browser.close()

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

    def __init__(self, storage_state_path: Optional[Path] = None):
        self.state_path = storage_state_path or get_storage_state_path()

    def _ensure_authenticated(self) -> None:
        """Check if storage state exists."""
        if not self.state_path.exists():
            raise AuthRequiredError(
                f"No saved Substack session found at {self.state_path}. "
                "Please run 'substack-saved-mcp login' first to authenticate."
            )

    def fetch_saved_posts_page(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Fetch a page of saved posts from Substack using Playwright request context."""
        self._ensure_authenticated()

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SubstackClientError("Playwright is not installed.")

        with sync_playwright() as p:
            api_context = p.request.new_context(storage_state=str(self.state_path))

            # Attempt fetching from Substack's saved posts / bookmarks API endpoints
            # API endpoint: https://substack.com/api/v1/bookmarks or /api/v1/saved_posts
            endpoint_url = f"https://substack.com/api/v1/bookmarks?limit={limit}&offset={offset}"
            response = api_context.get(endpoint_url)

            if response.status in (401, 403) or "sign-in" in response.url:
                raise AuthRequiredError("Substack session has expired or is invalid. Please run 'substack-saved-mcp login'.")

            if not response.ok:
                # Fallback endpoint if bookmarks is not matching
                endpoint_url_alt = f"https://substack.com/api/v1/saved_posts?limit={limit}&offset={offset}"
                response = api_context.get(endpoint_url_alt)

            if not response.ok:
                # If API fails, fall back to headless DOM extraction on https://substack.com/saved
                return self._fetch_via_dom(offset=offset, limit=limit, playwright_instance=p)

            try:
                data = response.json()
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict):
                    return data.get("bookmarks") or data.get("posts") or data.get("items") or []
                return []
            except Exception as e:
                logger.warning(f"JSON parsing error from Substack response: {e}. Falling back to DOM extraction.")
                return self._fetch_via_dom(offset=offset, limit=limit, playwright_instance=p)

    def _fetch_via_dom(self, offset: int = 0, limit: int = 20, playwright_instance: Any = None) -> List[Dict[str, Any]]:
        """Fallback method: Render https://substack.com/saved in headless browser and extract post cards."""
        self._ensure_authenticated()

        def _do_fetch(p):
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(self.state_path))
            page = context.new_page()

            page.goto("https://substack.com/saved", wait_until="networkidle")

            if "sign-in" in page.url or page.locator("text=Sign in").count() > 0:
                browser.close()
                raise AuthRequiredError("Substack session has expired or is invalid. Please run 'substack-saved-mcp login'.")

            # Extract post elements from DOM
            cards = page.locator("a[href*='/p/']").all()
            seen_urls = set()
            results: List[Dict[str, Any]] = []

            for card in cards:
                try:
                    href = card.get_attribute("href")
                    title = card.inner_text().strip()
                    if href and title and "/p/" in href:
                        clean = canonicalize_url(href)
                        if clean not in seen_urls:
                            seen_urls.add(clean)
                            # Extract publication name from hostname
                            parsed = urlparse(clean)
                            pub_name = parsed.netloc.split(".")[0].capitalize()

                            results.append({
                                "canonical_url": clean,
                                "title": title.split("\n")[0],
                                "publication_name": pub_name,
                                "publication_url": f"{parsed.scheme}://{parsed.netloc}",
                                "excerpt": title,
                                "saved_at": None,
                                "published_at": None,
                            })
                except Exception:
                    continue

            browser.close()
            return results

        if playwright_instance is not None:
            results = _do_fetch(playwright_instance)
        else:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                results = _do_fetch(p)

        return results[offset : offset + limit]

    def save_post(self, url: str) -> SavedPost:
        """Bookmark a post on Substack remotely and return extracted metadata."""
        self._ensure_authenticated()
        clean_url = canonicalize_url(url)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(self.state_path))
            page = context.new_page()

            page.goto(clean_url, wait_until="domcontentloaded")

            if "sign-in" in page.url:
                browser.close()
                raise AuthRequiredError("Session expired during save. Please run 'substack-saved-mcp login'.")

            # Extract title & metadata from page
            title = page.title().split("|")[0].strip() or "Substack Post"
            parsed = urlparse(clean_url)
            pub_name = parsed.netloc.split(".")[0].capitalize()

            # Attempt clicking bookmark button if present
            bookmark_btn = page.locator("button[aria-label*='bookmark' i], button[aria-label*='save' i]").first
            if bookmark_btn.count() > 0:
                try:
                    bookmark_btn.click(timeout=3000)
                except Exception:
                    pass

            # Also issue API request if post ID is found
            api_context = p.request.new_context(storage_state=str(self.state_path))
            # Try bookmark endpoint
            api_context.post(f"https://substack.com/api/v1/bookmark", data={"url": clean_url})

            # Save updated storage state
            context.storage_state(path=str(self.state_path))
            browser.close()

        return SavedPost(
            url=clean_url,
            title=title,
            publication_name=pub_name,
            publication_url=f"{parsed.scheme}://{parsed.netloc}",
            is_saved=1,
        )

    def unsave_post(self, url: str) -> bool:
        """Unbookmark a post on Substack remotely."""
        self._ensure_authenticated()
        clean_url = canonicalize_url(url)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(self.state_path))
            page = context.new_page()

            page.goto(clean_url, wait_until="domcontentloaded")

            if "sign-in" in page.url:
                browser.close()
                raise AuthRequiredError("Session expired during unsave. Please run 'substack-saved-mcp login'.")

            # Attempt clicking bookmark/unbookmark button if present
            bookmark_btn = page.locator("button[aria-label*='bookmark' i], button[aria-label*='save' i]").first
            if bookmark_btn.count() > 0:
                try:
                    bookmark_btn.click(timeout=3000)
                except Exception:
                    pass

            # Save updated state
            context.storage_state(path=str(self.state_path))
            browser.close()

        return True
