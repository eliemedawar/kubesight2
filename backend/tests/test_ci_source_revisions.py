"""Listing a repository's branches, tags and commits.

Every branch picker in KubeSight reads this — the Source tab, Run Build, the
registration wizard, and any pipeline parameter sourced from ``branches``. The
failure it exists to prevent is subtle and was live: the list looked fine, it
was just missing most of the branches, and nothing said so.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from api.services import application_intelligence_bitbucket as bb


def _fake_repository(*, branches: int, tags: int, sortable: bool = True):
    """A repository with a known number of refs, served page by page.

    Stands in for ``_collect`` rather than the network, so the test exercises
    the real budgeting, paging and kind handling without a Bitbucket account.
    """
    calls = []

    def collect(url, token, repository_ref, *, limit, max_pages=5, **_kwargs):
        path = urlsplit(url).path
        query = parse_qs(urlsplit(url).query)
        sort = query.get("sort", [""])[0]
        calls.append((path.rsplit("/", 1)[-1], sort))

        if sort and not sortable:
            # What an unsupported sort field looks like: a plain rejection.
            raise bb.BitbucketMetadataError(
                "Bitbucket metadata could not be loaded.", status=400
            )

        if path.endswith("/refs/branches"):
            total, prefix = branches, "branch"
        elif path.endswith("/refs/tags"):
            total, prefix = tags, "tag"
        elif path.endswith("/commits"):
            return [
                {"hash": f"{index:040x}", "message": f"commit {index}"}
                for index in range(min(total_commits, limit))
            ]
        else:
            return []

        # Paging is what the real limit interacts with: 100 per page, capped by
        # both `limit` and `max_pages`.
        available = min(total, limit, max_pages * 100)
        return [
            {"name": f"{prefix}-{index:04d}", "target": {"hash": f"{index:040x}"}}
            for index in range(available)
        ]

    total_commits = 25
    return collect, calls


def _kinds(payload):
    counts = {}
    for item in payload["items"]:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# The bug
# ---------------------------------------------------------------------------

def test_hundreds_of_tags_do_not_crowd_out_the_branches(monkeypatch):
    """The live failure: one /refs call capped at 200 and sorted by name meant
    a repository with 291 tags and 216 branches showed 24 branches. The branch
    somebody wanted was usually absent, and the list gave no hint of it."""
    collect, _calls = _fake_repository(branches=216, tags=291)
    monkeypatch.setattr(bb, "_collect", collect)

    payload = bb.list_revisions("acme/profile", "token")

    assert _kinds(payload)["branch"] == 216
    assert _kinds(payload)["tag"] == 291


def test_each_kind_gets_its_own_budget(monkeypatch):
    """Neither can starve the other, whichever is larger."""
    collect, _calls = _fake_repository(branches=900, tags=900)
    monkeypatch.setattr(bb, "_collect", collect)

    counts = _kinds(bb.list_revisions("acme/huge", "token"))
    assert counts["branch"] == bb.MAX_BRANCH_ITEMS
    assert counts["tag"] == bb.MAX_TAG_ITEMS


def test_a_budget_that_bit_is_reported(monkeypatch):
    """A shorter list is not the same as a complete one, and a picker that
    silently drops what somebody is looking for teaches them not to trust it."""
    collect, _calls = _fake_repository(branches=900, tags=5)
    monkeypatch.setattr(bb, "_collect", collect)

    payload = bb.list_revisions("acme/huge", "token")
    assert payload["truncated"]["branches"] is True
    assert payload["truncated"]["tags"] is False


def test_nothing_is_reported_as_truncated_when_it_all_fits(monkeypatch):
    collect, _calls = _fake_repository(branches=12, tags=3)
    monkeypatch.setattr(bb, "_collect", collect)

    payload = bb.list_revisions("acme/small", "token")
    assert payload["truncated"] == {"branches": False, "tags": False}


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def test_refs_are_requested_newest_first(monkeypatch):
    """If a budget has to bite it should bite the stale end. Sorting by name
    would instead drop everything after 'f' in the alphabet."""
    collect, calls = _fake_repository(branches=10, tags=10)
    monkeypatch.setattr(bb, "_collect", collect)

    bb.list_revisions("acme/profile", "token")
    assert ("branches", bb._REF_SORT) in calls
    assert ("tags", bb._REF_SORT) in calls


def test_a_refused_sort_still_produces_a_list(monkeypatch):
    """The sort is an optimisation. A provider that stops supporting it must
    cost ordering, not the entire branch picker."""
    collect, calls = _fake_repository(branches=10, tags=4, sortable=False)
    monkeypatch.setattr(bb, "_collect", collect)

    counts = _kinds(bb.list_revisions("acme/profile", "token"))
    assert counts["branch"] == 10
    assert counts["tag"] == 4
    # It tried the sorted endpoint first, then fell back.
    assert ("branches", bb._REF_SORT) in calls
    assert ("branches", "") in calls


# ---------------------------------------------------------------------------
# Only fetching what the caller will show
# ---------------------------------------------------------------------------

def test_a_branch_picker_does_not_pay_for_tags(monkeypatch):
    """Five pages of tags to fill a branch dropdown is several seconds spent on
    a list the control never shows."""
    collect, calls = _fake_repository(branches=200, tags=450)
    monkeypatch.setattr(bb, "_collect", collect)

    payload = bb.list_revisions("acme/profile", "token", kinds=("branch",))

    assert _kinds(payload) == {"branch": 200}
    assert not any(name == "tags" for name, _sort in calls)
    assert not any(name == "commits" for name, _sort in calls)


def test_the_default_is_still_everything(monkeypatch):
    """Run Build offers branches, tags and commits; nothing about the new
    filtering may change what a caller that asks for nothing receives."""
    collect, _calls = _fake_repository(branches=3, tags=2)
    monkeypatch.setattr(bb, "_collect", collect)

    counts = _kinds(bb.list_revisions("acme/profile", "token"))
    assert set(counts) == {"branch", "tag", "commit"}


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_a_refs_kind_comes_from_the_endpoint_not_the_payload(monkeypatch):
    """The rows are labelled by which endpoint returned them, so a provider
    that stops setting `type` cannot turn every branch into an unknown kind and
    empty the picker."""
    def collect(url, token, repository_ref, *, limit, max_pages=5, **_kwargs):
        if "/refs/branches" in url:
            return [{"name": "main", "target": {"hash": "a" * 40}}]  # no "type"
        return []

    monkeypatch.setattr(bb, "_collect", collect)
    payload = bb.list_revisions("acme/profile", "token", kinds=("branch",))
    assert payload["items"][0]["type"] == "branch"
    assert payload["items"][0]["value"] == "main"


def test_the_source_port_passes_the_kind_filter_through(monkeypatch):
    """The CI catalog asks for branches only; that has to survive the port."""
    from api.services.ci import source as source_port
    from api.services.ci.source import bitbucket as ci_bitbucket

    seen = {}

    def fake(repository_ref, token, credential_type="oauth", principal="", kinds=()):
        seen["kinds"] = kinds
        return {
            "items": [{"value": "main", "label": "Branch — main", "type": "branch",
                       "commit": "a" * 40}],
            "count": 1,
            "truncated": {"branches": False, "tags": False},
        }

    provider = source_port.get_provider("bitbucket")
    monkeypatch.setattr(ci_bitbucket, "list_revisions", fake)
    monkeypatch.setattr(
        provider, "_credential_parts", lambda credential: ("token", "oauth", "")
    )

    ref = provider.parse_repository_url("https://bitbucket.org/acme/profile")
    options = provider.list_revisions(ref, object(), kinds=("branch",))

    assert seen["kinds"] == ("branch",)
    assert [item.value for item in options] == ["main"]
    assert options[0].kind == "branch"


def test_the_source_port_defaults_to_every_kind(monkeypatch):
    """A caller that asks for nothing still gets what it always got."""
    from api.services.ci import source as source_port
    from api.services.ci.source import bitbucket as ci_bitbucket

    seen = {}

    def fake(repository_ref, token, credential_type="oauth", principal="", kinds=()):
        seen["kinds"] = kinds
        return {"items": [], "count": 0, "truncated": {"branches": False, "tags": False}}

    provider = source_port.get_provider("bitbucket")
    monkeypatch.setattr(ci_bitbucket, "list_revisions", fake)
    monkeypatch.setattr(
        provider, "_credential_parts", lambda credential: ("token", "oauth", "")
    )
    provider.list_revisions(
        provider.parse_repository_url("https://bitbucket.org/acme/profile"), object()
    )
    assert seen["kinds"] == ("branch", "tag", "commit")


def test_a_rate_limit_is_not_retried_as_if_the_sort_were_the_problem(monkeypatch):
    """Asking a struggling endpoint the same thing again, immediately, is how a
    rate limit becomes a longer rate limit. Only an outright rejection means
    "this variant of the request is unacceptable"."""
    calls = []

    def collect(url, token, repository_ref, *, limit, max_pages=5, **_kwargs):
        calls.append(url)
        raise bb.BitbucketMetadataError(
            "Bitbucket rate-limited the metadata request. Try again shortly.", status=429
        )

    monkeypatch.setattr(bb, "_collect", collect)
    with pytest.raises(bb.BitbucketMetadataError) as exc:
        bb.list_revisions("acme/profile", "token", kinds=("branch",))

    assert "rate-limited" in str(exc.value)
    assert len(calls) == 1


def test_a_timeout_is_not_retried_either(monkeypatch):
    calls = []

    def collect(url, token, repository_ref, *, limit, max_pages=5, **_kwargs):
        calls.append(url)
        raise bb.BitbucketMetadataError("Bitbucket metadata is temporarily unavailable.")

    monkeypatch.setattr(bb, "_collect", collect)
    with pytest.raises(bb.BitbucketMetadataError):
        bb.list_revisions("acme/profile", "token", kinds=("branch",))
    assert len(calls) == 1
