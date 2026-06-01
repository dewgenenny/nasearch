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
