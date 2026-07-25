"""Unit and integration tests for SQLite database repository and FTS5 search."""

import pytest
from pathlib import Path

from substack_saved_mcp.database import (
    get_post,
    get_status,
    init_db,
    list_posts,
    list_publications,
    search_posts,
    soft_delete_post,
    upsert_post,
)
from substack_saved_mcp.models import SavedPost


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test_saved_posts.sqlite"
    init_db(db_path)
    return db_path


def test_init_db_and_status(temp_db: Path):
    status = get_status(temp_db)
    assert status.total_saved_posts == 0
    assert status.total_unsaved_posts == 0
    assert status.total_publications == 0


def test_upsert_and_get_post(temp_db: Path):
    post = SavedPost(
        url="https://ai.substack.com/p/future-of-llms",
        title="The Future of LLMs",
        publication_name="AI Research",
        author_name="Alice Smith",
        published_at="2026-01-15T10:00:00Z",
        saved_at="2026-02-01T12:00:00Z",
        excerpt="An in-depth analysis of large language model architecture.",
        is_saved=1,
    )

    saved = upsert_post(post, temp_db)
    assert saved.id is not None
    assert saved.title == "The Future of LLMs"
    assert saved.url == "https://ai.substack.com/p/future-of-llms"

    fetched = get_post(saved.id, temp_db)
    assert fetched is not None
    assert fetched.title == "The Future of LLMs"
    assert fetched.saved_at == "2026-02-01T12:00:00Z"


def test_fts5_search(temp_db: Path):
    p1 = SavedPost(
        url="https://tech.substack.com/p/quantum-computing",
        title="Quantum Computing Breaksthrough",
        publication_name="Tech Weekly",
        author_name="Bob Jones",
        published_at="2026-03-01T00:00:00Z",
        saved_at="2026-03-02T00:00:00Z",
        excerpt="Exploring qubit entanglement and superconducting circuits.",
        is_saved=1,
    )
    p2 = SavedPost(
        url="https://cooking.substack.com/p/perfect-pasta",
        title="Perfect Handmade Pasta",
        publication_name="Culinary Arts",
        author_name="Chef Maria",
        published_at="2026-03-05T00:00:00Z",
        saved_at="2026-03-06T00:00:00Z",
        excerpt="Secrets to making traditional egg pasta at home.",
        is_saved=1,
    )

    upsert_post(p1, temp_db)
    upsert_post(p2, temp_db)

    # Search for quantum
    results = search_posts("quantum", db_path=temp_db)
    assert len(results) == 1
    assert results[0].title == "Quantum Computing Breaksthrough"

    # Search for pasta
    results_pasta = search_posts("pasta", db_path=temp_db)
    assert len(results_pasta) == 1
    assert results_pasta[0].publication_name == "Culinary Arts"


def test_soft_delete_post(temp_db: Path):
    post = SavedPost(
        url="https://news.substack.com/p/daily-briefing",
        title="Daily News Briefing",
        publication_name="Daily News",
        published_at="2026-04-01T00:00:00Z",
        saved_at="2026-04-01T01:00:00Z",
        is_saved=1,
    )
    saved = upsert_post(post, temp_db)
    assert saved.is_saved == 1

    # Soft delete / unsave
    unsaved = soft_delete_post(saved.url, temp_db)
    assert unsaved is not None
    assert unsaved.is_saved == 0
    assert unsaved.unsaved_at is not None

    # Listing active saved posts should now exclude unsaved post
    active_posts = list_posts(db_path=temp_db)
    assert len(active_posts) == 0

    # Status should reflect 1 unsaved post
    status = get_status(temp_db)
    assert status.total_saved_posts == 0
    assert status.total_unsaved_posts == 1


def test_list_publications(temp_db: Path):
    upsert_post(SavedPost(url="https://pub1.com/p/1", title="Post 1", publication_name="Pub One"), temp_db)
    upsert_post(SavedPost(url="https://pub1.com/p/2", title="Post 2", publication_name="Pub One"), temp_db)
    upsert_post(SavedPost(url="https://pub2.com/p/1", title="Post 3", publication_name="Pub Two"), temp_db)

    pubs = list_publications(temp_db)
    assert len(pubs) == 2
    assert pubs[0].publication_name == "Pub One"
    assert pubs[0].post_count == 2
    assert pubs[1].publication_name == "Pub Two"
    assert pubs[1].post_count == 1
