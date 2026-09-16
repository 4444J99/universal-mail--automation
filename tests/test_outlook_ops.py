"""Offline tests for the OutlookProvider mutation/folder surface.

tests/test_outlook_errors.py pins the U097 error-honesty invariants (never
turn API failure into empty success, None only for 404, never clobber
categories you could not read). This file covers the adjacent Graph surfaces
that had no offline coverage:

  * apply_label / archive   — the /move mutation moves to the resolved folder
    (or the well-known "archive)"), reporting True ONLY on a successful POST.
  * remove_label            — folder-based providers cannot remove labels.
  * star / unstar / mark_read / mark_unread — PATCH payload shapes, the
    midnight-UTC due-date encoding for star(), and honest False on failure.
  * ensure_label_exists     — hierarchical folder walk (Work/Dev/GitHub),
    mid-path cache reuse, the displayName race-resolution fallback, and
    RuntimeError when neither create nor find succeeds.
  * ensure_category_exists  — create + cache, default color resolution,
    post-create race refresh, and RuntimeError when still absent.
  * _init_folder_cache      — recursive child-folder caching with non-fatal
    subtree failures and parent-prefixed cache keys.
  * list_messages page_token — a skiptoken is used verbatim as the URL.

Every request is scripted on monkeypatched _api_get/_api_post/_api_patch;
nothing touches the network.
"""

from datetime import datetime

import pytest

from providers.outlook import (
    GRAPH_API_BASE,
    GRAPH_API_CATEGORIES,
    GRAPH_API_FOLDERS,
    OutlookProvider,
)


def _provider():
    return OutlookProvider(client_id="test-client-id")


class _Log:
    """Records _api_* calls and returns a fresh folder/category id per POST."""

    def __init__(self):
        self.posts = []
        self.gets = []
        self.patches = []
        self.post_raises = False
        self.get_result = {"value": []}

    def post(self, url, data):
        if self.post_raises:
            raise RuntimeError("offline")
        self.posts.append((url, data))
        return {"id": f"id{len(self.posts)}"}

    def get(self, url, params=None):
        self.gets.append((url, params))
        return self.get_result

    def patch(self, url, data):
        self.patches.append((url, data))
        return {}


# -- apply_label / archive: /move mutations --------------------------------
def test_apply_label_moves_to_resolved_folder(monkeypatch):
    p = _provider()
    monkeypatch.setattr(p, "ensure_label_exists", lambda l: "folder-42")
    log = _Log()
    monkeypatch.setattr(p, "_api_post", log.post)
    assert p.apply_label("m1", "Work") is True
    assert log.posts == [(
        f"{GRAPH_API_BASE}/me/messages/m1/move", {"destinationId": "folder-42"})]


def test_apply_label_false_when_move_fails(monkeypatch):
    p = _provider()
    monkeypatch.setattr(p, "ensure_label_exists", lambda l: "folder-42")

    def boom(url, data):
        raise RuntimeError("offline")

    monkeypatch.setattr(p, "_api_post", boom)
    assert p.apply_label("m1", "Work") is False


def test_archive_moves_to_well_known_archive_folder(monkeypatch):
    p = _provider()
    log = _Log()
    monkeypatch.setattr(p, "_api_post", log.post)
    assert p.archive("m1") is True
    assert log.posts == [(f"{GRAPH_API_BASE}/me/messages/m1/move",
                          {"destinationId": "archive"})]


def test_archive_false_when_move_fails(monkeypatch):
    p = _provider()

    def boom(url, data):
        raise RuntimeError("offline")

    monkeypatch.setattr(p, "_api_post", boom)
    assert p.archive("m1") is False


def test_remove_label_not_supported_for_folder_based():
    assert _provider().remove_label("m1", "Work") is False


# -- star / unstar / read-state PATCH payloads ------------------------------
def test_star_flags_without_due_date(monkeypatch):
    p = _provider()
    log = _Log()
    monkeypatch.setattr(p, "_api_patch", log.patch)
    assert p.star("m1") is True
    assert log.patches == [(f"{GRAPH_API_BASE}/me/messages/m1",
                            {"flag": {"flagStatus": "flagged"}})]


def test_star_encodes_due_date_as_midnight_utc(monkeypatch):
    # Graph dueDateTime is whole-day: the time component is dropped and the
    # request pins a UTC midnight so the flag tiles at the TO-DO date boundary.
    p = _provider()
    log = _Log()
    monkeypatch.setattr(p, "_api_patch", log.patch)
    assert p.star("m1", due_date=datetime(2026, 6, 3, 9, 30)) is True
    assert log.patches[0][1] == {"flag": {"flagStatus": "flagged",
                                          "dueDateTime": {"dateTime": "2026-06-03T00:00:00Z",
                                                          "timeZone": "UTC"}}}


def test_star_false_when_patch_fails(monkeypatch):
    p = _provider()

    def boom(url, data):
        raise RuntimeError("offline")

    monkeypatch.setattr(p, "_api_patch", boom)
    assert p.star("m1") is False


def test_unstar_clears_flag(monkeypatch):
    p = _provider()
    log = _Log()
    monkeypatch.setattr(p, "_api_patch", log.patch)
    assert p.unstar("m1") is True
    assert log.patches == [(f"{GRAPH_API_BASE}/me/messages/m1",
                            {"flag": {"flagStatus": "notFlagged"}})]


def test_unstar_false_when_patch_fails(monkeypatch):
    p = _provider()

    def boom(url, data):
        raise RuntimeError("offline")

    monkeypatch.setattr(p, "_api_patch", boom)
    assert p.unstar("m1") is False


@pytest.mark.parametrize("method,expected", [
    ("mark_read", {"isRead": True}),
    ("mark_unread", {"isRead": False}),
])
def test_read_state_patch_payloads(monkeypatch, method, expected):
    p = _provider()
    log = _Log()
    monkeypatch.setattr(p, "_api_patch", log.patch)
    assert getattr(p, method)("m1") is True
    assert log.patches == [(f"{GRAPH_API_BASE}/me/messages/m1", expected)]


def test_read_state_false_when_patch_fails(monkeypatch):
    p = _provider()

    def boom(url, data):
        raise RuntimeError("offline")

    monkeypatch.setattr(p, "_api_patch", boom)
    assert p.mark_read("m1") is False
    assert p.mark_unread("m1") is False


# -- speed: ensure_label_exists hierarchical folder walk ---------------------
def test_ensure_label_creates_nested_path_top_down(monkeypatch):
    p = _provider()
    log = _Log()
    monkeypatch.setattr(p, "_api_post", log.post)
    assert p.ensure_label_exists("Work/Dev/GitHub") == "id3"
    assert [u for u, _ in log.posts] == [
        GRAPH_API_FOLDERS,
        f"{GRAPH_API_FOLDERS}/id1/childFolders",
        f"{GRAPH_API_FOLDERS}/id2/childFolders",
    ]
    assert [d for _, d in log.posts] == [
        {"displayName": "Work"}, {"displayName": "Dev"}, {"displayName": "GitHub"}]
    assert p._folder_cache == {"Work": "id1", "Work/Dev": "id2",
                               "Work/Dev/GitHub": "id3"}


def test_ensure_label_reuses_existing_mid_path(monkeypatch):
    p = _provider()
    p._folder_cache["Work"] = "f0"
    log = _Log()
    monkeypatch.setattr(p, "_api_post", log.post)
    assert p.ensure_label_exists("Work/Dev/GitHub") == "id2"
    assert [u for u, _ in log.posts] == [
        f"{GRAPH_API_FOLDERS}/f0/childFolders",   # parent is the cached "Work"
        f"{GRAPH_API_FOLDERS}/id1/childFolders",
    ]
    assert p._folder_cache["Work/Dev/GitHub"] == "id2"


def test_ensure_label_cached_path_needs_no_http(monkeypatch):
    p = _provider()
    p._folder_cache["Work/Dev"] = "cached"
    log = _Log()
    monkeypatch.setattr(p, "_api_post", log.post)
    assert p.ensure_label_exists("Work/Dev") == "cached"
    assert log.posts == []


def test_ensure_label_race_resolves_by_display_name(monkeypatch):
    # The folder was created by another connection: the POST fails and the
    # fallback must find it with a displayName filter under the same parent.
    p = _provider()

    class _Race:
        def __init__(self):
            self.calls = 0

        def post(self, url, data):
            self.calls += 1
            raise RuntimeError("already exists")

        def get(self, url, params=None):
            assert params == {"$filter": "displayName eq 'Work'"}
            return {"value": [{"id": "found-id"}]}

    race = _Race()
    monkeypatch.setattr(p, "_api_post", race.post)
    monkeypatch.setattr(p, "_api_get", race.get)
    assert p.ensure_label_exists("Work") == "found-id"
    assert p._folder_cache["Work"] == "found-id"


def test_ensure_label_runtime_error_when_create_and_find_fail(monkeypatch):
    p = _provider()

    def boom(url, data):
        raise RuntimeError("offline")

    monkeypatch.setattr(p, "_api_post", boom)
    monkeypatch.setattr(p, "_api_get", boom)
    with pytest.raises(RuntimeError, match="create or find"):
        p.ensure_label_exists("Work")


# -- ensure_category_exists ---------------------------------------------------
def test_ensure_category_returns_cached_id_without_http(monkeypatch):
    p = _provider()
    p._category_cache["Critical"] = "cat-1"
    log = _Log()
    monkeypatch.setattr(p, "_api_post", log.post)
    assert p.ensure_category_exists("Critical") == "cat-1"
    assert log.posts == []


def test_ensure_category_creates_with_resolved_color(monkeypatch):
    p = _provider()
    log = _Log()
    monkeypatch.setattr(p, "_api_post", log.post)
    assert p.ensure_category_exists("Critical", color="red") == "id1"
    assert log.posts == [(GRAPH_API_CATEGORIES,
                          {"displayName": "Critical", "color": "preset0"})]
    assert p._category_cache["Critical"] == "id1"


def test_ensure_category_unknown_color_defaults_to_blue(monkeypatch):
    p = _provider()
    log = _Log()
    monkeypatch.setattr(p, "_api_post", log.post)
    p.ensure_category_exists("Critical", color="neon")
    assert log.posts[0][1]["color"] == "preset7"    # blue


def test_ensure_category_race_after_failed_create(monkeypatch):
    # Another connection created the category between cache and create: the
    # failed POST refreshes the cache and the category is found on retry.
    p = _provider()

    def boom(url, data):
        raise RuntimeError("already exists")

    monkeypatch.setattr(p, "_api_post", boom)
    monkeypatch.setattr(p, "_init_category_cache",
                        lambda: p._category_cache.update({"Critical": "cat-2"}))
    assert p.ensure_category_exists("Critical") == "cat-2"


def test_ensure_category_runtime_error_when_still_absent(monkeypatch):
    p = _provider()

    def boom(url, data):
        raise RuntimeError("already exists")

    monkeypatch.setattr(p, "_api_post", boom)
    monkeypatch.setattr(p, "_init_category_cache", lambda: None)   # still absent
    with pytest.raises(RuntimeError, match="Failed to create category"):
        p.ensure_category_exists("Critical")


def test_get_category_cache_returns_isolated_copy():
    p = _provider()
    p._category_cache["Critical"] = "cat-1"
    snapshot = p.get_category_cache()
    snapshot["Critical"] = "mutated"
    assert p._category_cache["Critical"] == "cat-1"


# -- _init_folder_cache: recursive child caching ------------------------------
def test_init_folder_cache_fetches_children_with_prefixed_names(monkeypatch):
    p = _provider()
    log = _Log()
    log.get_result = {"value": []}
    calls = []

    def get(url, params=None):
        if url == GRAPH_API_FOLDERS:
            calls.append("TOP")
            return {"value": [{"displayName": "Work", "id": "f1"}]}
        if url == f"{GRAPH_API_FOLDERS}/f1/childFolders":
            calls.append("CHILD")
            return {"value": [{"displayName": "Dev", "id": "f2"}]}
        if url == f"{GRAPH_API_FOLDERS}/f2/childFolders":
            calls.append("GRANDCHILD")
            return {"value": []}
        raise AssertionError(f"unexpected folder url {url}")

    monkeypatch.setattr(p, "_api_get", get)
    p._init_folder_cache()
    assert p._folder_cache == {"Work": "f1", "Work/Dev": "f2"}
    assert calls == ["TOP", "CHILD", "GRANDCHILD"]


def test_init_folder_cache_child_failure_is_non_fatal(caplog, monkeypatch):
    p = _provider()

    def get(url, params=None):
        if url == GRAPH_API_FOLDERS:
            return {"value": [{"displayName": "Work", "id": "f1"}]}
        if url == f"{GRAPH_API_FOLDERS}/f1/childFolders":
            raise RuntimeError("offline")
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(p, "_api_get", get)
    with caplog.at_level("WARNING"):
        p._init_folder_cache()
    assert p._folder_cache == {"Work": "f1"}          # top level still cached
    assert "Failed to fetch child folders under 'Work'" in caplog.text


# -- list_messages page_token: skiptoken used verbatim ------------------------
def test_list_messages_page_token_uses_url_verbatim(monkeypatch):
    p = _provider()
    log = _Log()
    log.get_result = {"value": [{
        "id": "m1", "subject": "s", "from": {"emailAddress": {"address": "a@b.c"}},
        "flag": {}, "receivedDateTime": "2026-06-01T00:00:00Z",
    }]}
    monkeypatch.setattr(p, "_api_get", log.get)
    token = "https://graph.microsoft.com/v1.0/skip?$skiptoken=abc"  # allow-secret: test literal
    result = p.list_messages(limit=5, page_token=token)
    assert log.gets == [(token, None)]                # no query rebuild
    assert [m.id for m in result.messages] == ["m1"]
    assert result.next_page_token is None