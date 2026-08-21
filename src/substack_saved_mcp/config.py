"""Configuration settings and filesystem path management."""

import os
from pathlib import Path

APP_NAME = "substack-saved-mcp"

_SAVED_POSTS_SOURCES = frozenset({"auto", "legacy", "unified", "dom"})


def get_saved_posts_source() -> str:
    """Return which saved-posts fetch source to use: 'auto' (default, tries legacy
    then the newer unified reader API then DOM), or a forced 'legacy'/'unified'/'dom'
    for testing and rollback. Falls back to 'auto' on an unrecognized value rather
    than raising, since a typo here shouldn't break every sync."""
    value = (os.getenv("SUBSTACK_SAVED_POSTS_SOURCE") or "auto").strip().lower()
    return value if value in _SAVED_POSTS_SOURCES else "auto"


_DEFAULT_FULLY_READ_THRESHOLD = 0.95


def get_fully_read_threshold() -> float:
    """Return the max_read_progress fraction at or above which a post counts as
    fully read. Defaults to 0.95, overridable via SUBSTACK_SAVED_FULLY_READ_THRESHOLD.
    Falls back to the default on an unparseable or out-of-(0, 1] value, since a typo
    here shouldn't break every read-state classification."""
    raw = os.getenv("SUBSTACK_SAVED_FULLY_READ_THRESHOLD")
    if not raw:
        return _DEFAULT_FULLY_READ_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_FULLY_READ_THRESHOLD
    return value if 0 < value <= 1 else _DEFAULT_FULLY_READ_THRESHOLD


def get_default_data_dir() -> Path:
    """Return OS-appropriate application data directory."""
    if env_dir := os.getenv("SUBSTACK_SAVED_DATA_DIR"):
        return Path(env_dir).expanduser().resolve()

    xdg_data = os.getenv("XDG_DATA_HOME")
    if xdg_data:
        return (Path(xdg_data) / APP_NAME).resolve()

    # Default to user home local share or fallback
    return (Path.home() / ".local" / "share" / APP_NAME).resolve()


def get_db_path() -> Path:
    """Return path to SQLite database."""
    if env_db := os.getenv("SUBSTACK_SAVED_DB_PATH"):
        return Path(env_db).expanduser().resolve()
    return get_default_data_dir() / "saved_posts.sqlite"


def get_browser_dir() -> Path:
    """Return path to Playwright browser context / storage state directory."""
    if env_browser := os.getenv("SUBSTACK_SAVED_BROWSER_DIR"):
        return Path(env_browser).expanduser().resolve()
    return get_default_data_dir() / "browser_state"


def get_storage_state_path() -> Path:
    """Return path to storage_state.json."""
    return get_browser_dir() / "storage_state.json"


def ensure_app_dirs() -> None:
    """Ensure data and browser state directories exist with restrictive permissions (0o700)."""
    data_dir = get_default_data_dir()
    browser_dir = get_browser_dir()

    data_dir.mkdir(parents=True, exist_ok=True)
    browser_dir.mkdir(parents=True, exist_ok=True)

    # Restrict permissions to user-only (read/write/execute) on POSIX systems
    if os.name == "posix":
        try:
            data_dir.chmod(0o700)
            browser_dir.chmod(0o700)
        except Exception:
            pass
