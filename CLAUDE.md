# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git policy

**Never commit or push without explicit approval from the user.** Always show what you intend to commit and wait for a clear go-ahead before running `git commit` or `git push`.

**Before proposing any commit for a new feature**, always pause and ask the user about security considerations — covering XSS, path traversal, injection, DoS vectors, and new dependencies. Wait for explicit sign-off before proceeding.

## What this project is

**NASearch** — a lightweight NAS file search UI. FastAPI serves a REST API and a single-page frontend; `plocate` (`updatedb` + `locate`) does the actual indexing and searching. No Elasticsearch, no Node, no build step.

Designed to run on Unraid via Docker. The array is mounted read-only at `/data`; the index DB and settings live in a persistent volume at `/index`.

## Dev workflow (required before pushing)

With ~250 users on this project, all changes must pass the test suite before being committed.

Tests run inside a clean `python:3.12-slim` container (matching CI) so dependency conflicts surface locally rather than on push.

### Run tests (one-liner)

```bash
docker compose -f docker-compose.dev.yml up --build --abort-on-container-exit --exit-code-from tests; docker compose -f docker-compose.dev.yml down
```

This builds the app image, starts the app container, then runs pytest inside a fresh Python 3.12 container against it. Exit code reflects the test result.

### Regression fixtures

The regression tests for reported bugs need conditions git can't carry — pre-1980 mtimes, directory names containing spaces, more files than `MAX_RESULTS`. Generate them into a **throwaway** directory (the script writes at the top level of the root it's given) and point the data mount at it:

```bash
sh tests/make_fixtures.sh /tmp/nasearch-data
DEV_DATA_PATH=/tmp/nasearch-data docker compose -f docker-compose.dev.yml up --build --abort-on-container-exit --exit-code-from tests
```

Without the fixtures those tests skip rather than fail, so the suite still runs against an ordinary data root — but a run that skips them isn't proof the bugs stayed fixed.

### App only (for manual testing in the browser)

```bash
docker compose -f docker-compose.dev.yml up -d --build nasearch
# ...test manually at http://localhost:8000...
docker compose -f docker-compose.dev.yml down
```

> **Never push if tests fail.** The `index-dev/` directory is git-ignored; don't commit it.

---

## Running locally (Docker)

```bash
# Build and start
docker compose up -d --build

# Trigger a re-index manually (the container uses the built-in scheduler by default)
curl -X POST http://localhost:8000/api/reindex
```

> **Note:** The README references `docker compose run --rm indexer`, but there is no separate `indexer` service in `docker-compose.yml`. Re-indexing is handled by the in-app scheduler and the `/api/reindex` endpoint.

To develop without Docker, install deps and run uvicorn directly:

```bash
pip install fastapi "uvicorn[standard]"
uvicorn app.main:app --reload
```

(`plocate` must also be installed on the host for indexing/searching to work.)

## Architecture

Everything meaningful lives in two files:

### `app/main.py` — FastAPI backend

| Concern | Detail |
|---|---|
| Config | Read from env vars at startup: `LOCATE_DB`, `DATA_PATH`, `PRUNE_PATHS`, `MAX_RESULTS`, `AUTH_USER`, `AUTH_PASS` |
| Auth | HTTP Basic Auth middleware; enabled only when both `AUTH_USER` and `AUTH_PASS` are set. Protects all routes including static files. Uses `secrets.compare_digest` to resist timing attacks. |
| Settings | Persisted to `/index/settings.json` — `interval_hours` and last-run metadata |
| Indexer | `run_index_sync()` wraps `updatedb` in a subprocess; called via `run_in_executor` so it doesn't block the event loop. Records `last_attempted` on every run and `last_error` on every outcome |
| Scheduler | `scheduler_loop()` is an async task (started in the FastAPI lifespan) that re-indexes on the configured interval; sleeps in ≤10 min slices so settings changes land without a restart. `seconds_until_next_index()` enforces a retry floor of `min(1 h, interval)` from the last *attempt*, so a failing index can't re-crawl the array in a hot loop. The loop body catches everything except `CancelledError` — an uncaught exception here would kill the task and silently stop all future indexing |
| Search | `GET /api/search` shells out to `locate -d <db> -i -n <n> -- <patterns>`. locate ANDs multiple patterns, so an extension filter is passed as an extra `*.ext` pattern and applied *inside* the index — filtering after the fetch cap starved broad queries |
| Archives | `index_archives=False` prunes archive-shaped directories at index time. `updatedb --prunenames` matches literal basenames only (globs are silently ignored), so `_find_archive_dirs()` walks the tree and passes real paths via `--prunepaths`. That list is one space-separated string, so paths containing whitespace can't be expressed — the search-time `_ARCHIVE_RE` filter is applied whenever the setting is off, which is what actually guarantees archive contents stay out of results |
| File serving | `GET /api/file` validates path is within `DATA_PATH` via `safe_resolve()` (path traversal protection), then streams via `FileResponse` which supports HTTP Range requests (needed for video seeking). `dl=1` forces `Content-Disposition: attachment`. |
| State | `indexer_state` dict is in-memory (not persisted); reflects current run/error/progress |

API endpoints:
- `GET /api/search?q=&ext=&limit=` — search the index
- `GET /api/status` — DB existence, size, indexer state, last-run info, interval setting, `last_attempted` / `last_error`
- `POST /api/reindex` — fire-and-forget background re-index
- `POST /api/settings` — update `interval_hours` (must be one of: 0, 1, 6, 12, 24, 48, 168) and `index_archives`
- `GET /api/file?path=&dl=` — serve a file inline or as download

Static files are served from `/app/static` and mounted last, so API routes take precedence.

### `app/static/index.html` — Single-file SPA

Vanilla JS + CSS, no build toolchain. All styles are `<style>` and all logic is `<script>`. The frontend:
- Debounces search input (250 ms) and hits `/api/search`
- Polls `/api/status` every 10 s to update the header status dot
- Manages the settings panel (interval selector, manual re-index, index info)
- **Download**: each result row has a `↓` button that triggers `downloadFile()` — creates a hidden `<a download>` element pointing to `/api/file?dl=1`
- **Preview modal**: clicking any result row opens a full-screen preview modal. File type is detected client-side from the extension via the `EXT` map. Supported preview types:
  - `image` — `<img>` tag
  - `video` — `<video>` with controls; HTTP Range requests enable seeking
  - `audio` — `<audio>` with controls + styled metadata display
  - `pdf` — `<embed>` filling the modal
  - `text` — streams up to 100 KB via `ReadableStream`, displays in `<pre>`; shows truncation notice and download button if file is larger
  - unsupported types — shows file info panel with a download button

## Configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `LOCATE_DB` | `/index/files.db` | Path to the plocate database |
| `DATA_PATH` | `/data` | Root directory `updatedb` scans and that `/api/file` is allowed to serve |
| `PRUNE_PATHS` | `/data/appdata /data/system /data/domains /data/isos` | Space-separated paths to skip during indexing |
| `MAX_RESULTS` | `500` | Hard cap on results returned by `/api/search` |
| `AUTH_USER` | _(unset)_ | Basic auth username; auth disabled if either `AUTH_USER` or `AUTH_PASS` is unset |
| `AUTH_PASS` | _(unset)_ | Basic auth password |
