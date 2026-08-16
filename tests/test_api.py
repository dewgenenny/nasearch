"""
Integration tests for the NASearch API.

Assumes the dev container is running (docker compose -f docker-compose.dev.yml up -d --build)
with NOAUTH=true and /home/tom mounted as /data.
"""
import re
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


def test_homepage_has_csp(client):
    r = client.get("/")
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'self'" in csp


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
    """A real .zip file under /data, found via search, or skipped if none exists.

    Directories can be named *.zip too (that's exactly what the archive filter
    exists for), and search results don't carry is_dir — it's filled in later by
    /api/enrich — so ask enrich which of the candidates is actually a file.
    """
    results = client.get("/api/search", params={"q": ".zip", "ext": "zip", "limit": 20}).json()["results"]
    paths = [r["path"] for r in results]
    if not paths:
        pytest.skip("No .zip files found in index")
    meta = client.post("/api/enrich", json={"paths": paths}).json()
    real = next((p for p in paths if meta.get(p, {}).get("is_dir") is False), None)
    if real is None:
        pytest.skip("No .zip regular files found in index (only archive-shaped directories)")
    return real


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


# ── Archive indexing settings ─────────────────────────────────────────────────

def test_status_includes_index_archives(client):
    data = client.get("/api/status").json()
    assert "index_archives" in data
    assert isinstance(data["index_archives"], bool)


def test_status_index_archives_defaults_true(client):
    data = client.get("/api/status").json()
    assert data["index_archives"] is True


def test_settings_index_archives_disable(idle_client):
    r = idle_client.post("/api/settings", json={"index_archives": False})
    assert r.status_code == 200
    assert r.json()["settings"]["index_archives"] is False
    idle_client.post("/api/settings", json={"index_archives": True})


def test_settings_index_archives_persisted_to_status(idle_client):
    idle_client.post("/api/settings", json={"index_archives": False})
    data = idle_client.get("/api/status").json()
    assert data["index_archives"] is False
    idle_client.post("/api/settings", json={"index_archives": True})


def test_settings_index_archives_re_enable(idle_client):
    idle_client.post("/api/settings", json={"index_archives": False})
    idle_client.post("/api/settings", json={"index_archives": True})
    assert idle_client.get("/api/status").json()["index_archives"] is True


# ── no_archives search filter ─────────────────────────────────────────────────

def test_search_no_archives_param_accepted(client):
    r = client.get("/api/search", params={"q": ".", "no_archives": "1"})
    assert r.status_code == 200
    data = r.json()
    assert "results" in data
    assert "truncated" in data


def test_search_no_archives_result_shape_unchanged(client):
    # Filtering must not break the result objects — fields are still present.
    r = client.get("/api/search", params={"q": ".", "no_archives": "1"})
    assert r.status_code == 200
    for item in r.json()["results"]:
        for field in ("path", "name", "dir", "ext", "is_dir"):
            assert field in item


_ARCHIVE_MID_PATH = re.compile(
    r'\.(zip|7z|rar|tar\.gz|tgz|tar\.bz2|tbz2|tar\.xz|txz)/',
    re.IGNORECASE,
)

def test_search_no_archives_filters_archive_paths(client):
    # Any result returned with no_archives=1 must not have an archive extension
    # mid-path (e.g. /data/backup.zip/file.txt).  True whether or not the test
    # index actually contains such paths.
    r = client.get("/api/search", params={"q": ".", "no_archives": "1"})
    assert r.status_code == 200
    for item in r.json()["results"]:
        assert not _ARCHIVE_MID_PATH.search(item["path"]), (
            f"Archive-internal path leaked through filter: {item['path']}"
        )


def test_search_without_no_archives_ignores_filter(client, known_file):
    # Default behaviour (no_archives absent) returns a superset — the filtered
    # result set must be a subset of the unfiltered one.
    #
    # Uses a narrow query on purpose. The two calls use different fetch windows
    # — the filtered one takes extra headroom, since rows it discards would
    # otherwise eat into the page — so at the result cap each can surface rows
    # the other never fetched. The subset property is only meaningful below it.
    params = {"q": known_file["name"]}
    full = client.get("/api/search", params=params).json()
    assert full["truncated"] is False, "pick a narrower query: this one hits the cap"
    filt = client.get("/api/search", params={**params, "no_archives": "1"}).json()
    assert {i["path"] for i in filt["results"]} <= {i["path"] for i in full["results"]}


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


@pytest.fixture(scope="module")
def sample_html_path(client):
    """An .html file under /data, found via search, or skipped if none exists."""
    results = client.get("/api/search", params={"q": ".html", "ext": "html", "limit": 1}).json()["results"]
    if not results:
        pytest.skip("No .html files found in index")
    return results[0]["path"]


def test_file_serve_html_inline_downgraded_to_text_plain(client, sample_html_path):
    # HTML from the array must never be served inline as text/html — it would
    # execute scripts on the app's origin (stored XSS).
    r = client.get("/api/file", params={"path": sample_html_path})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")


def test_file_serve_html_download_keeps_attachment(client, sample_html_path):
    r = client.get("/api/file", params={"path": sample_html_path, "dl": "1"})
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")


def test_file_serve_inline_has_sandbox_csp(client, sample_file_path):
    r = client.get("/api/file", params={"path": sample_file_path})
    assert r.status_code == 200
    if not r.headers["content-type"].startswith("application/pdf"):
        assert r.headers.get("content-security-policy") == "sandbox"


def test_search_negative_limit_rejected(client):
    r = client.get("/api/search", params={"q": "test", "limit": -1})
    assert r.status_code == 422


# ── Auth (NOAUTH mode) ────────────────────────────────────────────────────────

def test_no_redirect_to_login_in_noauth_mode(client):
    # With NOAUTH=true, all routes should be directly accessible
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200


# ── Enrich ───────────────────────────────────────────────────────────────────

def test_enrich_returns_expected_fields(client, known_file):
    r = client.post("/api/enrich", json={"paths": [known_file["path"]]})
    assert r.status_code == 200
    data = r.json()
    assert known_file["path"] in data
    entry = data[known_file["path"]]
    for field in ("is_dir", "ext", "icon", "size", "size_bytes", "mtime"):
        assert field in entry, f"Missing field '{field}' in enrich response"
    assert entry["is_dir"] is False
    assert isinstance(entry["mtime"], int)


def test_enrich_path_traversal_blocked(client):
    r = client.post("/api/enrich", json={"paths": ["../../etc/passwd"]})
    assert r.status_code == 200
    # Traversal paths are silently dropped — the response dict must be empty
    assert r.json() == {}


def test_enrich_non_list_body_returns_400(client):
    r = client.post("/api/enrich", json={"paths": "not-a-list"})
    assert r.status_code == 400


def test_enrich_non_string_paths_skipped(client, known_file):
    # Non-string entries are silently skipped; valid ones still resolve
    r = client.post("/api/enrich", json={"paths": [123, None, known_file["path"]]})
    assert r.status_code == 200
    data = r.json()
    assert known_file["path"] in data


def test_enrich_honours_max_results_cap(client):
    # Build a list larger than MAX_RESULTS (default 500); endpoint must not blow up
    big_list = [f"/data/nonexistent_{i}.txt" for i in range(600)]
    r = client.post("/api/enrich", json={"paths": big_list})
    assert r.status_code == 200
    # All paths are nonexistent so they get omitted from the result, but the
    # call must complete without error
    assert isinstance(r.json(), dict)


def test_enrich_missing_file_omitted(client):
    r = client.post("/api/enrich", json={"paths": ["/data/__definitely_does_not_exist__.xyz"]})
    assert r.status_code == 200
    # Nonexistent paths are omitted rather than causing a 500
    assert r.json() == {}


def test_enrich_empty_list_returns_empty(client):
    r = client.post("/api/enrich", json={"paths": []})
    assert r.status_code == 200
    assert r.json() == {}


# ── Unknown routes ────────────────────────────────────────────────────────────

def test_unknown_api_route_returns_404(client):
    r = client.get("/api/doesnotexist")
    assert r.status_code == 404


def test_status_exposes_index_attempt_and_error(client):
    """#4 — the scheduler computed its next run only from last_indexed, which a
    failed run never wrote, so failures re-crawled the array every 60s."""
    data = client.get("/api/status").json()
    assert "last_attempted" in data
    assert "last_error" in data
    assert data["last_attempted"] is not None


def test_zip_folder_with_pre_1980_mtime_is_valid(client, fixtures):
    """#5 — ZipInfo raises on pre-1980 timestamps. Mid-stream that truncated an
    already-committed 200 response into an archive no tool could open."""
    import io
    import zipfile

    r = client.get("/api/zip", params={"path": f"{fixtures}/oldstamp"})
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.testzip() is None
    assert sorted(zf.namelist()) == ["ancient.txt", "normal.txt"]
    # Clamped to the ZIP epoch floor, with contents intact
    assert zf.getinfo("ancient.txt").date_time == (1980, 1, 1, 0, 0, 0)
    assert zf.read("ancient.txt") == b"restored from tape\n"


def test_no_archives_filter_hides_archive_contents(client, fixtures):
    inside = client.get("/api/search", params={"q": "nasearchfixture_in"}).json()
    hidden = client.get(
        "/api/search", params={"q": "nasearchfixture_in", "no_archives": "1"}
    ).json()
    assert hidden["total"] == 0
    # The unfiltered query must still see them, or this proves nothing
    assert inside["total"] >= 1 or client.get("/api/status").json()["index_archives"] is False


def test_index_archives_off_excludes_archive_contents(client, fixtures, reindex_now):
    """#6 — updatedb --prunenames matches literal basenames, so the '*.zip'
    patterns were silently ignored and the setting did nothing.

    Leaves index_archives back on, and re-indexes, so later tests are unaffected.
    """
    try:
        r = client.post("/api/settings", json={"index_archives": False})
        assert r.status_code == 200
        reindex_now()

        data = client.get("/api/search", params={"q": "nasearchfixture_in"}).json()
        assert data["total"] == 0, "archive contents still returned with indexing off"

        prune = client.get("/api/status").json()["indexer"]["archive_prune"]
        assert prune["pruned"] >= 1
        # 'my bundle.zip' has a space, and --prunepaths is a space-separated
        # list, so it can't be excluded at index time — the search-time filter
        # is what keeps it out of results.
        assert prune["unprunable"] >= 1
    finally:
        client.post("/api/settings", json={"index_archives": True})
        reindex_now()


def test_search_ext_filter_survives_result_cap(client, fixtures):
    """#8 — the extension filter ran after locate's cap, so a broad query
    could burn the whole fetch window on the wrong extension. The fixture has
    600 .log files sorting ahead of 3 .dat files, well past the 500 cap."""
    data = client.get("/api/search", params={"q": "nasearchbulk", "ext": "dat"}).json()
    names = sorted(r["name"] for r in data["results"])
    assert names == [
        "nasearchbulk_zzz_rare_1.dat",
        "nasearchbulk_zzz_rare_2.dat",
        "nasearchbulk_zzz_rare_3.dat",
    ]


def test_search_reports_truncation_when_capped(client, fixtures):
    """#7 — truncated was hardcoded false: locate's own -n cap meant the
    'did we get more rows than the page' comparison could never be true."""
    data = client.get("/api/search", params={"q": "nasearchbulk"}).json()
    assert data["truncated"] is True
    assert data["total_matches"] == 603
    assert data["total"] < data["total_matches"]


def test_search_truncation_independent_of_limit(client, fixtures):
    data = client.get("/api/search", params={"q": "nasearchbulk", "limit": 10}).json()
    assert len(data["results"]) == 10
    assert data["truncated"] is True
    assert data["total_matches"] == 603


def test_search_not_truncated_when_complete(client, fixtures):
    data = client.get("/api/search", params={"q": "nasearchfixture_outside_archive"}).json()
    assert data["truncated"] is False
    assert data["total_matches"] == data["total"] == 1
