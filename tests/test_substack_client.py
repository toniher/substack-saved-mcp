"""Unit tests for SubstackSavedPostsClient's remote save/unsave confirmation logic."""

from pathlib import Path

from substack_saved_mcp.substack_client import SubstackSavedPostsClient


class MockButton:
    """Simulates a bookmark/save toggle button whose attributes may change on click."""

    def __init__(
        self, present=True, attrs_before=None, attrs_after=None, raise_on_click=False
    ):
        self._present = present
        self._attrs_before = attrs_before or {
            "aria-label": "Save",
            "aria-pressed": "false",
            "class": "btn",
        }
        self._attrs_after = (
            attrs_after if attrs_after is not None else dict(self._attrs_before)
        )
        self._raise_on_click = raise_on_click
        self._clicked = False

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self._present else 0

    def get_attribute(self, attr):
        attrs = self._attrs_after if self._clicked else self._attrs_before
        return attrs.get(attr)

    def click(self, timeout=None):
        if self._raise_on_click:
            raise Exception("click failed")
        self._clicked = True


class MockPage:
    """Minimal page double for _click_bookmark_toggle unit tests."""

    def __init__(self, button):
        self._button = button

    def locator(self, sel):
        return self._button

    def wait_for_timeout(self, ms):
        pass


class MockPageForImpl(MockPage):
    """Page double that also satisfies _save_post_impl/_unsave_post_impl's needs."""

    url = "https://pub.substack.com/p/some-post"

    def __init__(self, button, preloads=None):
        super().__init__(button)
        self._preloads = preloads

    def goto(self, url, **kwargs):
        pass

    def title(self):
        return "Some Post Title | Some Pub"

    def evaluate(self, script):
        return self._preloads


class MockContextForImpl:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page

    def storage_state(self, path=None):
        pass


class MockBrowserForImpl:
    def __init__(self, context):
        self._context = context

    def new_context(self, storage_state=None):
        return self._context

    def close(self):
        pass


class MockChromium:
    def __init__(self, page):
        self._page = page

    def launch(self, headless=True):
        return MockBrowserForImpl(MockContextForImpl(self._page))


class MockApiResponse:
    def __init__(self, ok=True):
        self.ok = ok


class MockApiRequestContext:
    def __init__(self, ok=True, delete_ok=None):
        self._ok = ok
        self._delete_ok = ok if delete_ok is None else delete_ok

    def post(self, url, data=None):
        return MockApiResponse(ok=self._ok)

    def delete(self, url, data=None):
        return MockApiResponse(ok=self._delete_ok)


class MockRequest:
    def __init__(self, ok=True, delete_ok=None):
        self._ok = ok
        self._delete_ok = delete_ok

    def new_context(self, storage_state=None):
        return MockApiRequestContext(ok=self._ok, delete_ok=self._delete_ok)


class MockPlaywrightForImpl:
    def __init__(self, page, api_ok=True, delete_ok=None):
        self.chromium = MockChromium(page)
        self.request = MockRequest(ok=api_ok, delete_ok=delete_ok)


def _client(tmp_path: Path) -> SubstackSavedPostsClient:
    state_file = tmp_path / "storage_state.json"
    state_file.write_text('{"cookies": [], "origins": []}')
    return SubstackSavedPostsClient(storage_state_path=state_file)


def test_click_bookmark_toggle_confirmed_on_attribute_change(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton(
        attrs_before={"aria-label": "Save", "aria-pressed": "false", "class": "btn"},
        attrs_after={
            "aria-label": "Saved",
            "aria-pressed": "true",
            "class": "btn active",
        },
    )
    assert client._click_bookmark_toggle(MockPage(button)) == "confirmed"


def test_click_bookmark_toggle_unconfirmed_when_state_unchanged(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton()  # attrs_after defaults to a copy identical to attrs_before
    assert client._click_bookmark_toggle(MockPage(button)) == "unconfirmed"


def test_click_bookmark_toggle_not_found(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton(present=False)
    assert client._click_bookmark_toggle(MockPage(button)) == "not_found"


def test_click_bookmark_toggle_click_failed(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton(raise_on_click=True)
    assert client._click_bookmark_toggle(MockPage(button)) == "click_failed"


def test_save_post_impl_confirmed_via_button_state_change(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton(
        attrs_before={"aria-label": "Save", "aria-pressed": "false", "class": "btn"},
        attrs_after={
            "aria-label": "Saved",
            "aria-pressed": "true",
            "class": "btn active",
        },
    )
    pw = MockPlaywrightForImpl(MockPageForImpl(button), api_ok=False)

    post, confirmation = client._save_post_impl(
        url="https://pub.substack.com/p/some-post", playwright_instance=pw
    )
    assert confirmation == "confirmed"
    assert post.url == "https://pub.substack.com/p/some-post"
    assert post.is_saved == 1


def test_save_post_impl_unconfirmed_when_no_signal_confirms_it(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton()  # unchanged attributes after click
    pw = MockPlaywrightForImpl(MockPageForImpl(button), api_ok=False)

    _, confirmation = client._save_post_impl(
        url="https://pub.substack.com/p/some-post", playwright_instance=pw
    )
    assert confirmation == "unconfirmed"


def test_save_post_impl_confirmed_via_api_response_when_button_missing(tmp_path: Path):
    """The DOM button and the direct post_id-keyed API POST are independent
    confirmation channels — either can confirm, and the API call only fires
    once window._preloads has yielded a numeric post_id."""
    client = _client(tmp_path)
    button = MockButton(present=False)
    preloads = {
        "post": {"id": 207627976, "title": "Some Post", "audience": "everyone"},
        "pub": {"name": "Some Pub"},
    }
    pw = MockPlaywrightForImpl(MockPageForImpl(button, preloads=preloads), api_ok=True)

    post, confirmation = client._save_post_impl(
        url="https://pub.substack.com/p/some-post", playwright_instance=pw
    )
    assert confirmation == "confirmed"
    assert post.substack_post_id == "207627976"
    assert post.title == "Some Post"
    assert post.publication_name == "Some Pub"


def test_save_post_impl_skips_api_call_when_post_id_unknown(tmp_path: Path):
    """Without a numeric post_id from window._preloads, the direct API call is
    never attempted (there's nothing correct to key it on) — only the DOM
    click can confirm."""
    client = _client(tmp_path)
    button = MockButton(present=False)
    pw = MockPlaywrightForImpl(MockPageForImpl(button), api_ok=True)

    post, confirmation = client._save_post_impl(
        url="https://pub.substack.com/p/some-post", playwright_instance=pw
    )
    assert confirmation == "not_found"
    assert post.substack_post_id is None


def test_unsave_post_impl_confirmed_via_button_state_change(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton(
        attrs_before={
            "aria-label": "Saved",
            "aria-pressed": "true",
            "class": "btn active",
        },
        attrs_after={"aria-label": "Save", "aria-pressed": "false", "class": "btn"},
    )
    pw = MockPlaywrightForImpl(MockPageForImpl(button))

    status = client._unsave_post_impl(
        url="https://pub.substack.com/p/some-post", playwright_instance=pw
    )
    assert status == "confirmed"


def test_unsave_post_impl_not_found(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton(present=False)
    pw = MockPlaywrightForImpl(MockPageForImpl(button))

    status = client._unsave_post_impl(
        url="https://pub.substack.com/p/some-post", playwright_instance=pw
    )
    assert status == "not_found"


def test_unsave_post_impl_confirmed_via_direct_api_when_post_id_known(tmp_path: Path):
    """When the numeric post_id is known, the real DELETE endpoint is used
    directly and the DOM is never touched — no browser/page interaction needed."""
    client = _client(tmp_path)
    button = MockButton(
        present=False
    )  # would report "not_found" if the DOM path were reached
    pw = MockPlaywrightForImpl(MockPageForImpl(button), delete_ok=True)

    status = client._unsave_post_impl(
        url="https://pub.substack.com/p/some-post",
        post_id=200489572,
        playwright_instance=pw,
    )
    assert status == "confirmed"


def test_fetch_post_content_impl_returns_body_html_from_preloads(tmp_path: Path):
    client = _client(tmp_path)
    preloads = {
        "post": {
            "body_html": "<p>Full content.</p>",
            "title": "Some Post",
            "audience": "everyone",
        }
    }
    pw = MockPlaywrightForImpl(
        MockPageForImpl(MockButton(present=False), preloads=preloads)
    )

    result = client._fetch_post_content_impl(
        url="https://pub.substack.com/p/some-post", playwright_instance=pw
    )
    assert result["body_html"] == "<p>Full content.</p>"
    assert result["title"] == "Some Post"
    assert result["audience"] == "everyone"


def test_fetch_post_content_impl_returns_none_when_preloads_lacks_body(tmp_path: Path):
    client = _client(tmp_path)
    preloads = {"post": {"title": "Some Post"}}
    pw = MockPlaywrightForImpl(
        MockPageForImpl(MockButton(present=False), preloads=preloads)
    )

    result = client._fetch_post_content_impl(
        url="https://pub.substack.com/p/some-post", playwright_instance=pw
    )
    assert result["body_html"] is None


def test_unsave_post_impl_falls_back_to_dom_when_api_delete_fails(tmp_path: Path):
    """If the direct DELETE call doesn't confirm, fall back to the DOM click."""
    client = _client(tmp_path)
    button = MockButton(
        attrs_before={
            "aria-label": "Saved",
            "aria-pressed": "true",
            "class": "btn active",
        },
        attrs_after={"aria-label": "Save", "aria-pressed": "false", "class": "btn"},
    )
    pw = MockPlaywrightForImpl(MockPageForImpl(button), delete_ok=False)

    status = client._unsave_post_impl(
        url="https://pub.substack.com/p/some-post",
        post_id=200489572,
        playwright_instance=pw,
    )
    assert status == "confirmed"  # confirmed via the DOM fallback, not the API
