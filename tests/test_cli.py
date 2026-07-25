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
        is_saved=1,
    ), setup_cli_db)

    runner = CliRunner()

    res_list = runner.invoke(cli, ["list"])
    assert res_list.exit_code == 0
    assert "CLI Test Post" in res_list.output
    assert "CLI Times" in res_list.output

    res_search = runner.invoke(cli, ["search", "testing"])
    assert res_search.exit_code == 0
    assert "CLI Test Post" in res_search.output
