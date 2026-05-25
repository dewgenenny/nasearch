# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**NASearch** — a lightweight NAS file search UI. FastAPI serves a REST API and a single-page frontend; `plocate` (`updatedb` + `locate`) does the actual indexing and searching. No Elasticsearch, no Node, no build step.

Designed to run on Unraid via Docker. The array is mounted read-only at `/data`; the index DB and settings live in a persistent volume at `/index`.

## Running locally (Docker)

```bash
# Build and start
docker compose up -d --build

# Trigger a re-index manually (the container uses the built-in scheduler by default)
curl -X POST http://localhost:8000/api/reindex
```

> **Note:** The README references `docker compose run --rm indexer`, but there is no separate `indexer` service in `docker-compose.yml`. Re-indexing is now handled by the in-app scheduler and the `/api/reindex` endpoint.

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
| Config | Read from env vars at startup: `LOCATE_DB`, `DATA_PATH`, `PRUNE_PATHS`, `MAX_RESULTS` |
| Settings | Persisted to `/index/settings.json` — currently just `interval_hours` and last-run metadata |
| Indexer | `run_index_sync()` wraps `updatedb` in a subprocess; called via `run_in_executor` so it doesn't block the event loop |
| Scheduler | `scheduler_loop()` is an async task (started in the FastAPI lifespan) that re-indexes on the configured interval; sleeps 10 min when interval is 0 (manual-only) |
| Search | `GET /api/search` shells out to `locate -d <db> -i -- <pattern>`; client-side extension filter is applied post-locate when both `q` and `ext` are provided |
| State | `indexer_state` dict is in-memory (not persisted); reflects current run/error/progress |

API endpoints:
- `GET /api/search?q=&ext=&limit=` — search the index
- `GET /api/status` — DB existence, size, indexer state, last-run info, interval setting
- `POST /api/reindex` — fire-and-forget background re-index
- `POST /api/settings` — update `interval_hours` (must be one of: 0, 1, 6, 12, 24, 48, 168)

Static files are served from `/app/static` and mounted last, so API routes take precedence.

### `app/static/index.html` — Single-file SPA

Vanilla JS + CSS, no build toolchain. All styles are inline `<style>` and all logic is inline `<script>`. The frontend:
- Debounces search input (250 ms) and hits `/api/search`
- Polls `/api/status` every 10 s to update the header status dot
- Manages the settings panel (interval selector, manual re-index button, index info)

## Configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `LOCATE_DB` | `/index/files.db` | Path to the plocate database |
| `DATA_PATH` | `/data` | Root directory `updatedb` scans |
| `PRUNE_PATHS` | `/data/appdata /data/system /data/domains /data/isos` | Space-separated paths to skip |
| `MAX_RESULTS` | `500` | Hard cap on results returned by the API |
