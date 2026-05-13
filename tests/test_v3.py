"""Unit tests for cli_anything.nightscout.core.v3 — generic v3 CRUD."""

from __future__ import annotations

from unittest import mock

import pytest

from cli_anything.nightscout.core import v3


CONN = {"server_url": "https://x", "api_token": "tok"}


# ─── v3_list ──────────────────────────────────────────────────────────────

class TestV3List:
    def test_default_params_and_path(self):
        captured = {}

        def fake_get(path, *, base_url, version, token=None, params=None, **_):
            captured["path"] = path
            captured["base_url"] = base_url
            captured["version"] = version
            captured["token"] = token
            captured["params"] = params
            return {"status": 200, "result": []}

        with mock.patch.object(v3.backend, "get", fake_get):
            v3.v3_list("activity", conn=CONN)

        assert captured["path"] == "/activity"
        assert captured["version"] == "v3"
        assert captured["token"] == "tok"
        assert captured["base_url"] == "https://x"
        assert captured["params"] == {"limit": 100}

    def test_limit_and_filter_propagate(self):
        captured = {}

        def fake_get(path, *, base_url, version, token=None, params=None, **_):
            captured["params"] = params
            return {"status": 200, "result": []}

        with mock.patch.object(v3.backend, "get", fake_get):
            v3.v3_list(
                "activity",
                conn=CONN,
                limit=25,
                sort="-created_at",
                filter={"eventType$eq": "Exercise", "created_at$gte": "2025-01-01"},
            )

        assert captured["params"]["limit"] == 25
        assert captured["params"]["sort"] == "-created_at"
        assert captured["params"]["eventType$eq"] == "Exercise"
        assert captured["params"]["created_at$gte"] == "2025-01-01"

    def test_unwraps_status_result_envelope(self):
        def fake_get(*a, **kw):
            return {"status": 200, "result": [{"_id": "a"}, {"_id": "b"}]}

        with mock.patch.object(v3.backend, "get", fake_get):
            res = v3.v3_list("activity", conn=CONN)

        assert isinstance(res, list)
        assert [r["_id"] for r in res] == ["a", "b"]

    def test_passes_bare_list_through(self):
        def fake_get(*a, **kw):
            return [{"_id": "x"}]

        with mock.patch.object(v3.backend, "get", fake_get):
            res = v3.v3_list("activity", conn=CONN)

        assert res == [{"_id": "x"}]

    def test_unknown_envelope_returns_empty(self):
        def fake_get(*a, **kw):
            return {"weird": "shape"}

        with mock.patch.object(v3.backend, "get", fake_get):
            res = v3.v3_list("activity", conn=CONN)

        assert res == []

    def test_empty_collection_raises(self):
        with pytest.raises(ValueError, match="collection"):
            v3.v3_list("", conn=CONN)


# ─── v3_get ───────────────────────────────────────────────────────────────

class TestV3Get:
    def test_returns_unwrapped_result_dict(self):
        captured = {}

        def fake_get(path, *, base_url, version, token=None, **_):
            captured["path"] = path
            captured["version"] = version
            captured["token"] = token
            return {"status": 200, "result": {"_id": "abc", "eventType": "Exercise"}}

        with mock.patch.object(v3.backend, "get", fake_get):
            res = v3.v3_get("activity", "abc", conn=CONN)

        assert captured["path"] == "/activity/abc"
        assert captured["version"] == "v3"
        assert captured["token"] == "tok"
        assert res == {"_id": "abc", "eventType": "Exercise"}

    def test_passes_bare_dict_through(self):
        def fake_get(*a, **kw):
            return {"_id": "abc"}

        with mock.patch.object(v3.backend, "get", fake_get):
            res = v3.v3_get("activity", "abc", conn=CONN)

        assert res == {"_id": "abc"}

    def test_empty_identifier_raises(self):
        with pytest.raises(ValueError, match="identifier"):
            v3.v3_get("activity", "", conn=CONN)

    def test_empty_collection_raises(self):
        with pytest.raises(ValueError, match="collection"):
            v3.v3_get("", "abc", conn=CONN)


# ─── v3_create ────────────────────────────────────────────────────────────

class TestV3Create:
    def test_posts_with_data(self):
        captured = {}

        def fake_post(path, *, data, base_url, version, token=None, **_):
            captured["path"] = path
            captured["data"] = data
            captured["version"] = version
            captured["token"] = token
            return {"status": 200, "identifier": "new-id"}

        with mock.patch.object(v3.backend, "post", fake_post):
            res = v3.v3_create(
                "activity", {"eventType": "Exercise", "duration": 30}, conn=CONN
            )

        assert captured["path"] == "/activity"
        assert captured["version"] == "v3"
        assert captured["token"] == "tok"
        assert captured["data"] == {"eventType": "Exercise", "duration": 30}
        assert res == {"status": 200, "identifier": "new-id"}

    def test_empty_collection_raises(self):
        with pytest.raises(ValueError, match="collection"):
            v3.v3_create("", {"x": 1}, conn=CONN)


# ─── v3_update ────────────────────────────────────────────────────────────

class TestV3Update:
    def test_puts_with_data(self):
        captured = {}

        def fake_put(path, *, data, base_url, version, token=None, **_):
            captured["path"] = path
            captured["data"] = data
            captured["version"] = version
            captured["token"] = token
            return {"status": 200}

        with mock.patch.object(v3.backend, "put", fake_put):
            v3.v3_update("activity", "abc", {"notes": "edited"}, conn=CONN)

        assert captured["path"] == "/activity/abc"
        assert captured["version"] == "v3"
        assert captured["token"] == "tok"
        assert captured["data"] == {"notes": "edited"}

    def test_empty_identifier_raises(self):
        with pytest.raises(ValueError, match="identifier"):
            v3.v3_update("activity", "", {"x": 1}, conn=CONN)

    def test_empty_collection_raises(self):
        with pytest.raises(ValueError, match="collection"):
            v3.v3_update("", "abc", {"x": 1}, conn=CONN)


# ─── v3_delete ────────────────────────────────────────────────────────────

class TestV3Delete:
    def test_deletes_by_identifier(self):
        captured = {}

        def fake_delete(path, *, base_url, version, token=None, **_):
            captured["path"] = path
            captured["version"] = version
            captured["token"] = token
            return {}

        with mock.patch.object(v3.backend, "delete", fake_delete):
            v3.v3_delete("activity", "abc-123", conn=CONN)

        assert captured["path"] == "/activity/abc-123"
        assert captured["version"] == "v3"
        assert captured["token"] == "tok"

    def test_empty_identifier_raises(self):
        with pytest.raises(ValueError, match="identifier"):
            v3.v3_delete("activity", "", conn=CONN)

    def test_empty_collection_raises(self):
        with pytest.raises(ValueError, match="collection"):
            v3.v3_delete("", "abc", conn=CONN)


# ─── v3_search ────────────────────────────────────────────────────────────

class TestV3Search:
    def test_builds_regex_filter_on_notes_and_eventtype(self):
        captured = {}

        def fake_get(path, *, base_url, version, token=None, params=None, **_):
            captured["path"] = path
            captured["params"] = params
            return {"status": 200, "result": [{"_id": "match"}]}

        with mock.patch.object(v3.backend, "get", fake_get):
            res = v3.v3_search("activity", conn=CONN, query="run", limit=10)

        assert captured["path"] == "/activity"
        assert captured["params"]["limit"] == 10
        assert captured["params"]["notes$re"] == "run"
        assert captured["params"]["eventType$re"] == "run"
        assert res == [{"_id": "match"}]

    def test_default_limit_is_100(self):
        captured = {}

        def fake_get(path, *, params=None, **_):
            captured["params"] = params
            return {"status": 200, "result": []}

        with mock.patch.object(v3.backend, "get", fake_get):
            v3.v3_search("activity", conn=CONN, query="run")

        assert captured["params"]["limit"] == 100

    def test_empty_collection_raises(self):
        with pytest.raises(ValueError, match="collection"):
            v3.v3_search("", conn=CONN, query="run")


# ─── path-traversal / collection-name validation ──────────────────────────

class TestCollectionValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            "../etc/passwd",
            "food/x",
            "food\\x",
            "Food",          # caps
            "food1",         # digit
            "food_bar",      # underscore
            "food-bar",      # hyphen
            "food bar",      # space
            ".food",         # leading dot
            "food.",         # trailing dot
            "/food",         # leading slash
            "food/",         # trailing slash
        ],
    )
    def test_bad_collection_rejected_everywhere(self, bad):
        with pytest.raises(ValueError):
            v3.v3_list(bad, conn=CONN)
        with pytest.raises(ValueError):
            v3.v3_get(bad, "abc", conn=CONN)
        with pytest.raises(ValueError):
            v3.v3_create(bad, {"x": 1}, conn=CONN)
        with pytest.raises(ValueError):
            v3.v3_update(bad, "abc", {"x": 1}, conn=CONN)
        with pytest.raises(ValueError):
            v3.v3_delete(bad, "abc", conn=CONN)
        with pytest.raises(ValueError):
            v3.v3_search(bad, conn=CONN, query="x")

    @pytest.mark.parametrize(
        "good",
        ["activity", "food", "treatments", "entries", "devicestatus", "profile"],
    )
    def test_valid_collections_accepted(self, good):
        # Each function should *not* raise ValueError when collection is valid.
        with mock.patch.object(v3.backend, "get", lambda *a, **kw: []):
            v3.v3_list(good, conn=CONN)
        with mock.patch.object(v3.backend, "get", lambda *a, **kw: {}):
            v3.v3_get(good, "id", conn=CONN)
        with mock.patch.object(v3.backend, "post", lambda *a, **kw: {}):
            v3.v3_create(good, {"x": 1}, conn=CONN)
        with mock.patch.object(v3.backend, "put", lambda *a, **kw: {}):
            v3.v3_update(good, "id", {"x": 1}, conn=CONN)
        with mock.patch.object(v3.backend, "delete", lambda *a, **kw: {}):
            v3.v3_delete(good, "id", conn=CONN)
