"""Unit tests for SubstackSavedPostsClient's remote save/unsave confirmation logic."""

from pathlib import Path

from substack_saved_mcp.substack_client import SubstackSavedPostsClient


class MockButton:
    """Simulates a bookmark/save toggle button whose attributes may change on click."""

    def __init__(self, present=True, attrs_before=None, attrs_after=None, raise_on_click=False):
        self._present = present
        self._attrs_before = attrs_before or {"aria-label": "Save", "aria-pressed": "false", "class": "btn"}
        self._attrs_after = attrs_after if attrs_after is not None else dict(self._attrs_before)
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

    def goto(self, url, **kwargs):
        pass

    def title(self):
        return "Some Post Title | Some Pub"


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
    def __init__(self, ok=True):
        self._ok = ok

    def post(self, url, data=None):
        return MockApiResponse(ok=self._ok)


class MockRequest:
    def __init__(self, ok=True):
        self._ok = ok

    def new_context(self, storage_state=None):
        return MockApiRequestContext(ok=self._ok)


class MockPlaywrightForImpl:
    def __init__(self, page, api_ok=True):
        self.chromium = MockChromium(page)
        self.request = MockRequest(ok=api_ok)


def _client(tmp_path: Path) -> SubstackSavedPostsClient:
    state_file = tmp_path / "storage_state.json"
    state_file.write_text('{"cookies": [], "origins": []}')
    return SubstackSavedPostsClient(storage_state_path=state_file)


def test_click_bookmark_toggle_confirmed_on_attribute_change(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton(
        attrs_before={"aria-label": "Save", "aria-pressed": "false", "class": "btn"},
        attrs_after={"aria-label": "Saved", "aria-pressed": "true", "class": "btn active"},
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
        attrs_after={"aria-label": "Saved", "aria-pressed": "true", "class": "btn active"},
    )
    pw = MockPlaywrightForImpl(MockPageForImpl(button), api_ok=False)

    post, confirmation = client._save_post_impl(url="https://pub.substack.com/p/some-post", playwright_instance=pw)
    assert confirmation == "confirmed"
    assert post.url == "https://pub.substack.com/p/some-post"
    assert post.is_saved == 1


def test_save_post_impl_unconfirmed_when_no_signal_confirms_it(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton()  # unchanged attributes after click
    pw = MockPlaywrightForImpl(MockPageForImpl(button), api_ok=False)

    _, confirmation = client._save_post_impl(url="https://pub.substack.com/p/some-post", playwright_instance=pw)
    assert confirmation == "unconfirmed"


def test_save_post_impl_confirmed_via_api_response_when_button_missing(tmp_path: Path):
    """The DOM button and API POST are independent confirmation channels — either can confirm."""
    client = _client(tmp_path)
    button = MockButton(present=False)
    pw = MockPlaywrightForImpl(MockPageForImpl(button), api_ok=True)

    _, confirmation = client._save_post_impl(url="https://pub.substack.com/p/some-post", playwright_instance=pw)
    assert confirmation == "confirmed"


def test_unsave_post_impl_confirmed_via_button_state_change(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton(
        attrs_before={"aria-label": "Saved", "aria-pressed": "true", "class": "btn active"},
        attrs_after={"aria-label": "Save", "aria-pressed": "false", "class": "btn"},
    )
    pw = MockPlaywrightForImpl(MockPageForImpl(button))

    status = client._unsave_post_impl(url="https://pub.substack.com/p/some-post", playwright_instance=pw)
    assert status == "confirmed"


def test_unsave_post_impl_not_found(tmp_path: Path):
    client = _client(tmp_path)
    button = MockButton(present=False)
    pw = MockPlaywrightForImpl(MockPageForImpl(button))

    status = client._unsave_post_impl(url="https://pub.substack.com/p/some-post", playwright_instance=pw)
    assert status == "not_found"
