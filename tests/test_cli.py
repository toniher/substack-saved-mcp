"""Integration tests for CLI subcommands using Click CliRunner."""

from click.testing import CliRunner
import pytest

from substack_saved_mcp.cli import cli
from substack_saved_mcp.database import init_db, upsert_post
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
    upsert_post(SavedPost(
        url="https://cli.substack.com/p/cli-post",
        title="CLI Test Post",
        publication_name="CLI Times",
        excerpt="Testing CLI output rendering.",
        audience="only_paid",
        is_saved=1,
    ), setup_cli_db)

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
    upsert_post(SavedPost(
        url="https://cli.substack.com/p/free-post",
        title="Free Post",
        publication_name="CLI Times",
        audience="everyone",
        is_saved=1,
    ), setup_cli_db)
    upsert_post(SavedPost(
        url="https://cli.substack.com/p/paid-post",
        title="Paid Post",
        publication_name="CLI Times",
        audience="only_paid",
        is_saved=1,
    ), setup_cli_db)

    runner = CliRunner()

    res_filtered = runner.invoke(cli, ["list", "--audience", "only_paid"])
    assert res_filtered.exit_code == 0
    assert "Paid Post" in res_filtered.output
    assert "Free Post" not in res_filtered.output

    res_audiences = runner.invoke(cli, ["audiences"])
    assert res_audiences.exit_code == 0
    assert "everyone" in res_audiences.output
    assert "only_paid" in res_audiences.output
