"""
Pytest configuration for NASearch integration tests.

Tests run against a live container. Start it before running:
    docker compose -f docker-compose.dev.yml up -d --build

The BASE_URL env var overrides the default (http://localhost:8000).
"""
import os
import time

import httpx
import pytest


BASE_URL = os.environ.get("NASEARCH_URL", "http://localhost:8000").rstrip("/")


def wait_for_server(url: str, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/api/status", timeout=2).raise_for_status()
            return
        except Exception:
            time.sleep(2)
    pytest.exit(f"NASearch at {url} did not become ready within {timeout}s", returncode=1)


def wait_for_index(client: httpx.Client, timeout: int = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get("/api/status")
        data = r.json()
        if data.get("db_exists") and not data.get("indexer", {}).get("running"):
            return
        time.sleep(2)
    pytest.fail(f"Index was not built within {timeout}s")


@pytest.fixture
def idle_client(client):
    """Client fixture that waits for any running reindex to complete first."""
    wait_for_index(client)
    return client


FIXTURE_ROOT = "/data/_nasearch_fixtures"


@pytest.fixture
def reindex_now(client):
    """Trigger a re-index and block until it has finished."""
    def _run():
        assert client.post("/api/reindex").status_code in (200, 409)
        wait_for_index(client)
    return _run


@pytest.fixture(scope="session")
def fixtures(client):
    """Gate for tests that need the generated fixture tree.

    Some conditions can't be committed to git — pre-1980 mtimes, directory names
    with spaces, more files than MAX_RESULTS — so tests/make_fixtures.sh builds
    them on the host and the data root is mounted from there. Without that
    tree these tests skip rather than fail, so the suite still runs against an
    ordinary data root.
    """
    r = client.get("/api/browse", params={"path": FIXTURE_ROOT})
    if r.status_code != 200:
        pytest.skip(
            f"{FIXTURE_ROOT} not present — run tests/make_fixtures.sh <root> and "
            "point DEV_DATA_PATH at it to enable the regression tests"
        )
    return FIXTURE_ROOT


@pytest.fixture(scope="session")
def client():
    wait_for_server(BASE_URL)
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        # Trigger an index if the DB doesn't exist yet; tolerate 409 if already running.
        status = c.get("/api/status").json()
        if not status.get("db_exists"):
            r = c.post("/api/reindex")
            assert r.status_code in (200, 409)
        wait_for_index(c)
        yield c
