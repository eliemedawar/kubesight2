"""Bitbucket reads are shared for a short while, and walk only what is asked."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from api.services import application_intelligence_bitbucket as bb


@pytest.fixture()
def cached(monkeypatch):
    """Turn the cache on (it is off under TESTING) and start it empty."""
    monkeypatch.setattr(bb, "_cache_enabled", lambda: True)
    bb.clear_cache()
    yield
    bb.clear_cache()


def _counting_collect(calls):
    def collect(url, token, repository_ref, *, limit, max_pages=5, **_kwargs):
        path = urlsplit(url).path
        calls.append(path)
        if path.endswith("/refs/branches"):
            return [{"name": "main", "target": {"hash": "a" * 40}}]
        if path.endswith("/refs/tags"):
            return [{"name": "v1", "target": {"hash": "b" * 40}}]
        if path.endswith("/commits"):
            return [{"hash": "c" * 40, "message": "m"}]
        return [{"type": "commit_file", "path": "src/main/App.java"}]

    return collect


def test_the_same_listing_is_fetched_once_while_fresh(monkeypatch, cached):
    calls = []
    monkeypatch.setattr(bb, "_collect", _counting_collect(calls))
    first = bb.list_revisions("acme/app", "token", kinds=("branch", "tag"))
    first["items"].clear()  # a caller mutating its copy must not touch the cache
    second = bb.list_revisions("acme/app", "token", kinds=("tag", "branch"))
    assert len(calls) == 2  # branches + tags, once
    assert [item["value"] for item in second["items"]] == ["main", "v1"]


def test_a_different_credential_or_kind_set_is_its_own_entry(monkeypatch, cached):
    calls = []
    monkeypatch.setattr(bb, "_collect", _counting_collect(calls))
    bb.list_revisions("acme/app", "token-1", kinds=("branch",))
    bb.list_revisions("acme/app", "token-2", kinds=("branch",))
    bb.list_revisions("acme/app", "token-1", kinds=("branch", "commit"))
    assert len(calls) == 1 + 1 + 2


def test_failures_are_not_cached(monkeypatch, cached):
    attempts = []

    def failing(*args, **kwargs):
        attempts.append(1)
        raise bb.BitbucketMetadataError("down")

    monkeypatch.setattr(bb, "_collect", failing)
    for _ in range(2):
        with pytest.raises(bb.BitbucketMetadataError):
            bb.list_revisions("acme/app", "token", kinds=("branch",))
    assert len(attempts) == 2


def test_a_commit_is_cached_longer_than_a_branch():
    assert bb._revision_ttl("a" * 40) > bb._revision_ttl("main")
    assert bb._revision_ttl("v1.2.3") == bb._revision_ttl("develop")


def test_the_tree_walk_starts_at_the_directory_asked_for(monkeypatch, cached):
    calls = []
    monkeypatch.setattr(bb, "_collect", _counting_collect(calls))
    listing = bb.list_tree("acme/app", "token", "main", path="src/main")
    assert calls == ["/2.0/repositories/acme/app/src/main/src/main/"]
    assert listing["paths"] == ["src/main/App.java"]
    bb.list_tree("acme/app", "token", "main", path="src/main")
    assert len(calls) == 1
    with pytest.raises(ValueError):
        bb.list_tree("acme/app", "token", "main", path="../etc")


def test_the_cache_is_off_outside_an_app_context():
    assert bb._cache_enabled() is False
