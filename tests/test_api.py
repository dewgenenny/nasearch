"""
Integration tests for the NASearch API.

Assumes the dev container is running (docker compose -f docker-compose.dev.yml up -d --build)
with NOAUTH=true and /home/tom mounted as /data.
"""
import pytest


@pytest.fixture(scope="session")
def known_file(client):
    """A file that exists under /data, discovered dynamically via browse."""
    entries = client.get("/api/browse", params={"path": "/data"}).json()["entries"]
    f = next((e for e in entries if not e["is_dir"]), None)
    if f is None:
        pytest.skip("No files found directly under /data")
    return f


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


def test_search_finds_known_file(client, known_file):
    r = client.get("/api/search", params={"q": known_file["name"]})
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) >= 1
    assert any(known_file["name"] in item["path"] for item in results)


def test_search_result_fields(client, known_file):
    r = client.get("/api/search", params={"q": known_file["name"], "limit": 1})
    results = r.json()["results"]
    assert results, f"Expected at least one result for {known_file['name']}"
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


# ── Browse ───────────────────────────────────────────────────────────────────

def test_browse_response_shape(client):
    r = client.get("/api/browse", params={"path": "/data"})
    assert r.status_code == 200
    data = r.json()
    assert "path" in data
    assert "entries" in data
    assert isinstance(data["entries"], list)


def test_browse_entries_sorted_dirs_first(client):
    r = client.get("/api/browse", params={"path": "/data"})
    entries = r.json()["entries"]
    saw_file = False
    for e in entries:
        if not e["is_dir"]:
            saw_file = True
        if saw_file:
            assert not e["is_dir"], "Directories must appear before files"


def test_browse_entry_fields(client):
    r = client.get("/api/browse", params={"path": "/data"})
    entries = r.json()["entries"]
    assert entries, "Expected at least one entry under /data"
    e = entries[0]
    for field in ("path", "name", "dir", "ext", "icon", "is_dir"):
        assert field in e


def test_browse_path_traversal_blocked(client):
    r = client.get("/api/browse", params={"path": "../../etc"})
    assert r.status_code == 403


def test_browse_non_directory_returns_400(client):
    # Find a real file under /data from the browse response, then try to browse it
    entries = client.get("/api/browse", params={"path": "/data"}).json()["entries"]
    file_entry = next((e for e in entries if not e["is_dir"]), None)
    if file_entry is None:
        pytest.skip("No files found under /data to test with")
    r = client.get("/api/browse", params={"path": file_entry["path"]})
    assert r.status_code == 400


# ── Zip listing ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def sample_zip_path(client):
    """A .zip file under /data, found via search, or skipped if none exists."""
    results = client.get("/api/search", params={"q": ".zip", "ext": "zip", "limit": 1}).json()["results"]
    if not results:
        pytest.skip("No .zip files found in index")
    return results[0]["path"]


def test_ziplist_response_shape(client, sample_zip_path):
    r = client.get("/api/ziplist", params={"path": sample_zip_path})
    assert r.status_code == 200
    data = r.json()
    assert "path" in data
    assert "count" in data
    assert "entries" in data
    assert isinstance(data["entries"], list)


def test_ziplist_entry_fields(client, sample_zip_path):
    entries = client.get("/api/ziplist", params={"path": sample_zip_path}).json()["entries"]
    if entries:
        e = entries[0]
        assert "name" in e
        assert "size" in e
        assert "is_dir" in e


def test_ziplist_path_traversal_blocked(client):
    r = client.get("/api/ziplist", params={"path": "../../etc/passwd"})
    assert r.status_code == 403


def test_ziplist_non_zip_returns_400(client, known_file):
    r = client.get("/api/ziplist", params={"path": known_file["path"]})
    assert r.status_code == 400


# ── Settings ──────────────────────────────────────────────────────────────────

def test_settings_update_valid_interval(idle_client):
    r = idle_client.post("/api/settings", json={"interval_hours": 6})
    assert r.status_code == 200
    assert r.json()["settings"]["interval_hours"] == 6


def test_settings_persisted_to_status(idle_client):
    idle_client.post("/api/settings", json={"interval_hours": 12})
    data = idle_client.get("/api/status").json()
    assert data["interval_hours"] == 12


def test_settings_rejects_invalid_interval(idle_client):
    r = idle_client.post("/api/settings", json={"interval_hours": 99})
    assert r.status_code == 400


def test_settings_reset_to_zero(idle_client):
    # Leave the container in a clean state (manual-only mode)
    r = idle_client.post("/api/settings", json={"interval_hours": 0})
    assert r.status_code == 200


# ── File serving ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def sample_file_path(client, known_file):
    """A real file path within /data, for testing file-serving endpoints."""
    return known_file["path"]


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
