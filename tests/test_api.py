"""
Integration tests for the NASearch API.

Assumes the dev container is running (docker compose -f docker-compose.dev.yml up -d --build)
with NOAUTH=true and /home/tom mounted as /data.
"""
import pytest


# ── Homepage ──────────────────────────────────────────────────────────────────

def test_homepage_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ── Status ────────────────────────────────────────────────────────────────────

def test_status_shape(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "db_exists" in data
    assert "indexer" in data
    assert "interval_hours" in data
    assert "auth_enabled" in data


def test_status_db_exists_after_index(client):
    # conftest.py waits for the index, so this must be true
    data = client.get("/api/status").json()
    assert data["db_exists"] is True


def test_status_auth_disabled_in_noauth_mode(client):
    data = client.get("/api/status").json()
    assert data["auth_enabled"] is False


# ── Reindex ───────────────────────────────────────────────────────────────────

def test_reindex_accepted(client):
    # May return 200 (started) or 409 (already running); both are valid
    r = client.post("/api/reindex")
    assert r.status_code in (200, 409)


# ── Search ────────────────────────────────────────────────────────────────────

def test_search_response_shape(client):
    r = client.get("/api/search", params={"q": "."})
    assert r.status_code == 200
    data = r.json()
    assert "results" in data
    assert "total" in data
    assert "truncated" in data
    assert isinstance(data["results"], list)


def test_search_empty_query_returns_empty(client):
    r = client.get("/api/search")
    assert r.status_code == 200
    data = r.json()
    assert data["results"] == []
    assert data["total"] == 0


def test_search_finds_known_file(client):
    # requirements.txt is present in the repo (CI) and in /home/tom (local dev)
    r = client.get("/api/search", params={"q": "requirements.txt"})
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) >= 1
    paths = [item["path"] for item in results]
    assert any("requirements.txt" in p for p in paths)


def test_search_result_fields(client):
    r = client.get("/api/search", params={"q": "requirements.txt", "limit": 1})
    results = r.json()["results"]
    assert results, "Expected at least one result for requirements.txt"
    item = results[0]
    assert "path" in item
    assert "name" in item
    assert "dir" in item
    assert "ext" in item
    assert "is_dir" in item


def test_search_ext_filter(client):
    r = client.get("/api/search", params={"q": ".", "ext": "py"})
    assert r.status_code == 200
    results = r.json()["results"]
    # Every returned file should have a .py extension
    for item in results:
        if not item["is_dir"]:
            assert item["ext"] == "py", f"Unexpected ext in {item['path']}"


def test_search_limit_respected(client):
    r = client.get("/api/search", params={"q": ".", "limit": 5})
    assert r.status_code == 200
    data = r.json()
    assert len(data["results"]) <= 5


def test_search_truncated_flag(client):
    # With limit=1 and a broad query there should be more than 1 result
    r = client.get("/api/search", params={"q": ".", "limit": 1})
    data = r.json()
    if data["total"] >= 1:
        assert data["truncated"] is True


# ── Settings ──────────────────────────────────────────────────────────────────

def test_settings_update_valid_interval(client):
    r = client.post("/api/settings", json={"interval_hours": 6})
    assert r.status_code == 200
    assert r.json()["settings"]["interval_hours"] == 6


def test_settings_persisted_to_status(client):
    client.post("/api/settings", json={"interval_hours": 12})
    data = client.get("/api/status").json()
    assert data["interval_hours"] == 12


def test_settings_rejects_invalid_interval(client):
    r = client.post("/api/settings", json={"interval_hours": 99})
    assert r.status_code == 400


def test_settings_reset_to_zero(client):
    # Leave the container in a clean state (manual-only mode)
    r = client.post("/api/settings", json={"interval_hours": 0})
    assert r.status_code == 200


# ── File serving ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def sample_file_path(client):
    """Return the container path of requirements.txt, or skip if not found."""
    results = client.get("/api/search", params={"q": "requirements.txt", "limit": 1}).json()["results"]
    if not results:
        pytest.skip("requirements.txt not found in index")
    return results[0]["path"]


def test_file_serve_inline(client, sample_file_path):
    r = client.get("/api/file", params={"path": sample_file_path})
    assert r.status_code == 200


def test_file_serve_download(client, sample_file_path):
    r = client.get("/api/file", params={"path": sample_file_path, "dl": "1"})
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")


def test_file_serve_unknown_path_returns_404(client):
    r = client.get("/api/file", params={"path": "/data/this_file_does_not_exist_xyz.txt"})
    assert r.status_code == 404


def test_file_serve_path_traversal_blocked(client):
    r = client.get("/api/file", params={"path": "../../etc/passwd"})
    assert r.status_code == 403


# ── Auth (NOAUTH mode) ────────────────────────────────────────────────────────

def test_no_redirect_to_login_in_noauth_mode(client):
    # With NOAUTH=true, all routes should be directly accessible
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200


# ── Unknown routes ────────────────────────────────────────────────────────────

def test_unknown_api_route_returns_404(client):
    r = client.get("/api/doesnotexist")
    assert r.status_code == 404
