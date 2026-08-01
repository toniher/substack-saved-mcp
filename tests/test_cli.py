"""Integration tests for CLI subcommands using Click CliRunner."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from substack_saved_mcp.cli import cli
from substack_saved_mcp.database import get_post, init_db, upsert_post
from substack_saved_mcp.models import SavedPost


@pytest.fixture(autouse=True)
def setup_cli_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_test.sqlite"
    monkeypatch.setenv("SUBSTACK_SAVED_DB_PATH", str(db_path))
    init_db(db_path)
    return db_path


def test_cli_init_and_status():
    runner = CliRunner()
    res_init = runner.invoke(cli, ["init"])
    assert res_init.exit_code == 0
    assert "Initialized database" in res_init.output

    res_status = runner.invoke(cli, ["status"])
    assert res_status.exit_code == 0
    assert "Active Saved Posts" in res_status.output


def test_cli_search_and_list(setup_cli_db):
    upsert_post(
        SavedPost(
            url="https://cli.substack.com/p/cli-post",
            title="CLI Test Post",
            publication_name="CLI Times",
            excerpt="Testing CLI output rendering.",
            audience="only_paid",
            is_saved=1,
        ),
        setup_cli_db,
    )

    runner = CliRunner()

    res_list = runner.invoke(cli, ["list"])
    assert res_list.exit_code == 0
    assert "CLI Test Post" in res_list.output
    assert "CLI Times" in res_list.output
    assert "only_paid" in res_list.output

    res_search = runner.invoke(cli, ["search", "testing"])
    assert res_search.exit_code == 0
    assert "CLI Test Post" in res_search.output
    assert "only_paid" in res_search.output


def test_cli_audience_filter_and_command(setup_cli_db):
    upsert_post(
        SavedPost(
            url="https://cli.substack.com/p/free-post",
            title="Free Post",
            publication_name="CLI Times",
            audience="everyone",
            is_saved=1,
        ),
        setup_cli_db,
    )
    upsert_post(
        SavedPost(
            url="https://cli.substack.com/p/paid-post",
            title="Paid Post",
            publication_name="CLI Times",
            audience="only_paid",
            is_saved=1,
        ),
        setup_cli_db,
    )

    runner = CliRunner()

    res_filtered = runner.invoke(cli, ["list", "--audience", "only_paid"])
    assert res_filtered.exit_code == 0
    assert "Paid Post" in res_filtered.output
    assert "Free Post" not in res_filtered.output

    res_audiences = runner.invoke(cli, ["audiences"])
    assert res_audiences.exit_code == 0
    assert "everyone" in res_audiences.output
    assert "only_paid" in res_audiences.output


def test_cli_get_content_fetches_and_caches(setup_cli_db):
    upsert_post(
        SavedPost(
            url="https://cli.substack.com/p/content-post",
            title="Content Post",
            publication_name="CLI Times",
            is_saved=1,
        ),
        setup_cli_db,
    )

    runner = CliRunner()

    with patch("substack_saved_mcp.cli.SubstackSavedPostsClient") as mock_client_cls:
        mock_client_cls.return_value.fetch_post_content.return_value = {
            "body_html": "<p>Full text here.</p>",
            "title": "Content Post",
            "audience": "everyone",
        }
        res = runner.invoke(cli, ["get-content", "https://cli.substack.com/p/content-post"])
        assert res.exit_code == 0
        assert "Full text here." in res.output
        assert "Title: Content Post" in res.output

    cached = get_post("https://cli.substack.com/p/content-post", setup_cli_db)
    assert cached.content_text == "Full text here."


def test_cli_list_and_search_show_reading_time(setup_cli_db):
    upsert_post(
        SavedPost(
            url="https://cli.substack.com/p/long-read",
            title="Long Read",
            publication_name="CLI Times",
            excerpt="A lengthy essay worth your time.",
            word_count=850,
            reading_time_minutes=5,
            is_saved=1,
        ),
        setup_cli_db,
    )

    runner = CliRunner()

    res_list = runner.invoke(cli, ["list"])
    assert res_list.exit_code == 0
    assert "5 min" in res_list.output
    assert "850 words" in res_list.output

    res_search = runner.invoke(cli, ["search", "essay"])
    assert res_search.exit_code == 0
    assert "5 min" in res_search.output


def test_cli_search_shows_image_url_but_list_omits_it(setup_cli_db):
    """image_url is shown in 'search' detail output but deliberately left out of
    the terser 'list' output, since it's a long, uninformative CDN URL there."""
    upsert_post(
        SavedPost(
            url="https://cli.substack.com/p/with-image",
            title="Post With Image",
            publication_name="CLI Times",
            excerpt="Has a thumbnail worth noting.",
            image_url="https://substackcdn.com/image/fetch/example.jpeg",
            is_saved=1,
        ),
        setup_cli_db,
    )

    runner = CliRunner()

    res_list = runner.invoke(cli, ["list"])
    assert res_list.exit_code == 0
    assert "https://substackcdn.com/image/fetch/example.jpeg" not in res_list.output

    res_search = runner.invoke(cli, ["search", "thumbnail"])
    assert res_search.exit_code == 0
    assert "https://substackcdn.com/image/fetch/example.jpeg" in res_search.output


def test_cli_search_date_range_filters(setup_cli_db):
    """The 'search' command exposes the same published/saved date-range filters
    the DB and MCP layers already support."""
    upsert_post(
        SavedPost(
            url="https://cli.substack.com/p/old",
            title="Old Essay",
            publication_name="CLI Times",
            excerpt="shared keyword essay",
            published_at="2025-01-01T00:00:00Z",
            is_saved=1,
        ),
        setup_cli_db,
    )
    upsert_post(
        SavedPost(
            url="https://cli.substack.com/p/new",
            title="New Essay",
            publication_name="CLI Times",
            excerpt="shared keyword essay",
            published_at="2026-06-01T00:00:00Z",
            is_saved=1,
        ),
        setup_cli_db,
    )

    runner = CliRunner()
    res = runner.invoke(cli, ["search", "essay", "--published-after", "2026-01-01"])
    assert res.exit_code == 0
    assert "New Essay" in res.output
    assert "Old Essay" not in res.output


def test_cli_get_content_not_found(setup_cli_db):
    runner = CliRunner()
    res = runner.invoke(cli, ["get-content", "https://cli.substack.com/p/missing"])
    assert res.exit_code == 0
    assert "not found" in res.output
