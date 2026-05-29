import subprocess
import os
import stat as stat_mod
import io
import json
import zipfile
import asyncio
import time
import mimetypes
import secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Iterator, Optional
from urllib.parse import quote

from fastapi import FastAPI, Query, BackgroundTasks, Request, Form
from fastapi.responses import JSONResponse, FileResponse, Response, StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH       = os.environ.get("LOCATE_DB",   "/index/files.db")
DATA_PATH     = os.environ.get("DATA_PATH",   "/data")
PRUNE_PATHS   = os.environ.get("PRUNE_PATHS", "/data/appdata /data/system /data/domains /data/isos")
MAX_RESULTS   = int(os.environ.get("MAX_RESULTS", "500"))
AUTH_USER     = os.environ.get("AUTH_USER",   "")
AUTH_PASS     = os.environ.get("AUTH_PASS",   "")
NOAUTH        = os.environ.get("NOAUTH",      "false").strip().lower() == "true"
ZIP_MAX_FILES  = int(os.environ.get("ZIP_MAX_FILES", "2000"))
ZIP_MAX_BYTES  = int(os.environ.get("ZIP_MAX_BYTES", str(2 * 1024 ** 3)))  # 2 GB
SESSION_HOURS  = int(os.environ.get("SESSION_HOURS", "24"))
SETTINGS_FILE  = "/index/settings.json"
SESSION_COOKIE = "nasearch_session"

# In-memory session store: token → expiry timestamp
_sessions: dict[str, float] = {}

def _session_create() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_HOURS * 3600
    return token

def _session_valid(token: str | None) -> bool:
    if not token:
        return False
    exp = _sessions.get(token)
    if exp is None:
        return False
    if time.time() > exp:
        _sessions.pop(token, None)
        return False
    return True

def _session_delete(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)

# ── Startup safety gate ───────────────────────────────────────────────────────
_auth_enabled = AUTH_USER and AUTH_PASS
if not _auth_enabled and not NOAUTH:
    print(
        "\n"
        "╔══════════════════════════════════════════════════════════════╗\n"
        "║                  NASearch won't start                       ║\n"
        "╠══════════════════════════════════════════════════════════════╣\n"
        "║  NASearch runs as root and can serve any file on your array. ║\n"
        "║  You must choose one of:                                     ║\n"
        "║                                                              ║\n"
        "║  A) Enable session auth (recommended):                       ║\n"
        "║     Set AUTH_USER and AUTH_PASS in docker-compose.yml        ║\n"
        "║                                                              ║\n"
        "║  B) Acknowledge you understand the risk (no auth):           ║\n"
        "║     Set NOAUTH=true in docker-compose.yml                    ║\n"
        "║                                                              ║\n"
        "║  See README.md for details.                                  ║\n"
        "╚══════════════════════════════════════════════════════════════╝\n",
        file=__import__("sys").stderr,
    )
    raise SystemExit(1)

DEFAULT_SETTINGS = {
    "interval_hours": 24,   # 0 = manual only
    "last_indexed": None,
    "last_duration_seconds": None,
}

# ── State ─────────────────────────────────────────────────────────────────────
indexer_state = {
    "running": False,
    "progress": None,
    "error": None,
}
_scheduler_task: Optional[asyncio.Task] = None


# ── Auth helpers ─────────────────────────────────────────────────────────────

def _check_credentials(username: str, password: str) -> bool:
    return (
        secrets.compare_digest(username, AUTH_USER or "") and
        secrets.compare_digest(password, AUTH_PASS or "")
    )


# ── Settings helpers ──────────────────────────────────────────────────────────
def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
            return {**DEFAULT_SETTINGS, **data}
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(s: dict):
    Path(SETTINGS_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ── Indexer ───────────────────────────────────────────────────────────────────
def run_index_sync():
    """Runs updatedb in a thread. Safe to call from asyncio via run_in_executor."""
    if indexer_state["running"]:
        return False, "Indexer already running"

    indexer_state["running"] = True
    indexer_state["progress"] = "Starting updatedb…"
    indexer_state["error"] = None
    started = datetime.now(timezone.utc)

    try:
        cmd = [
            "updatedb", "-l", "0",
            "-o", DB_PATH,
            "-U", DATA_PATH,
            "--prunepaths", PRUNE_PATHS,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        duration = (datetime.now(timezone.utc) - started).total_seconds()

        if result.returncode != 0:
            indexer_state["error"] = result.stderr.strip() or "updatedb exited with error"
            return False, indexer_state["error"]

        settings = load_settings()
        settings["last_indexed"] = datetime.now(timezone.utc).isoformat()
        settings["last_duration_seconds"] = round(duration)
        save_settings(settings)
        indexer_state["progress"] = None
        return True, f"Indexed in {round(duration)}s"

    except subprocess.TimeoutExpired:
        indexer_state["error"] = "Indexer timed out after 1 hour"
        return False, indexer_state["error"]
    except Exception as e:
        indexer_state["error"] = str(e)
        return False, str(e)
    finally:
        indexer_state["running"] = False
        indexer_state["progress"] = None


# ── Scheduler ─────────────────────────────────────────────────────────────────
async def scheduler_loop():
    """Async loop that re-indexes on the configured interval."""
    while True:
        settings = load_settings()
        interval_hours = settings.get("interval_hours", 24)

        if interval_hours <= 0:
            # Manual only — check again in 10 minutes in case settings change
            await asyncio.sleep(600)
            continue

        last = settings.get("last_indexed")
        if last:
            last_dt = datetime.fromisoformat(last)
            now = datetime.now(timezone.utc)
            elapsed_hours = (now - last_dt).total_seconds() / 3600
            wait_hours = max(0, interval_hours - elapsed_hours)
        else:
            wait_hours = 0  # Never indexed — do it now

        if wait_hours > 0:
            await asyncio.sleep(wait_hours * 3600)

        # Re-check interval hasn't been disabled while we were sleeping
        settings = load_settings()
        if settings.get("interval_hours", 24) > 0:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_index_sync)

        # After indexing, wait the full interval before next run
        await asyncio.sleep(60)  # brief pause before re-evaluating


# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    global _scheduler_task
    _scheduler_task = asyncio.create_task(scheduler_loop())
    yield
    _scheduler_task.cancel()


app = FastAPI(lifespan=lifespan)


# ── Security headers (middleware) ────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


# ── Auth gate (middleware) ────────────────────────────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not _auth_enabled:
        return await call_next(request)
    # Login/logout routes are always public
    if request.url.path in ("/login", "/api/logout"):
        return await call_next(request)
    if _session_valid(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)
    # API calls get a JSON 401; everything else redirects to login page
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "session expired"}, status_code=401)
    return RedirectResponse(url="/login", status_code=302)


# ── Helpers ───────────────────────────────────────────────────────────────────
def format_size_bytes(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def format_size(path: str) -> Optional[str]:
    try:
        return format_size_bytes(os.path.getsize(path))
    except Exception:
        return None


def get_icon(path: str) -> str:
    ext = Path(path).suffix.lower()
    icons = {
        ".stl": "🧊", ".3mf": "🧊", ".obj": "🧊",
        ".mp4": "🎬", ".mkv": "🎬", ".avi": "🎬", ".mov": "🎬", ".webm": "🎬",
        ".mp3": "🎵", ".flac": "🎵", ".wav": "🎵", ".ogg": "🎵", ".aac": "🎵", ".m4a": "🎵",
        ".jpg": "🖼️", ".jpeg": "🖼️", ".png": "🖼️", ".gif": "🖼️", ".webp": "🖼️",
        ".pdf": "📄", ".doc": "📝", ".docx": "📝", ".txt": "📝",
        ".zip": "📦", ".tar": "📦", ".gz": "📦", ".rar": "📦", ".7z": "📦",
        ".py": "🐍", ".js": "📜", ".ts": "📜", ".sh": "⚙️",
        ".iso": "💿", ".img": "💿",
        ".xlsx": "📊", ".csv": "📊",
    }
    return icons.get(ext, "📁" if not ext else "📄")


def safe_resolve(path: str) -> Optional[Path]:
    """Resolve path and ensure it falls within DATA_PATH. Returns None on violation."""
    try:
        full = Path(path).resolve()
        root = Path(DATA_PATH).resolve()
        full.relative_to(root)  # raises ValueError if outside root
        return full
    except (ValueError, Exception):
        return None


# ── Login page ───────────────────────────────────────────────────────────────
_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>NASearch — sign in</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500&display=swap" rel="stylesheet">
  <style>
    :root{{--bg:#0a0a0a;--bg2:#111;--border:#252525;--border-hi:#333;--amber:#ffb300;--amber-glow:rgba(255,179,0,.07);--text:#d8d8d8;--text-muted:#383838;--mono:'IBM Plex Mono',monospace;}}
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
    html,body{{height:100%;background:var(--bg);color:var(--text);font-family:var(--mono);display:flex;align-items:center;justify-content:center;}}
    .card{{width:100%;max-width:340px;padding:0 24px;}}
    .logo{{font-size:22px;font-weight:500;color:var(--amber);letter-spacing:2px;margin-bottom:5px;}}
    .logo::before{{content:'> ';color:var(--text-muted);font-weight:300;}}
    .sub{{font-size:10px;color:var(--text-muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:40px;}}
    .field{{margin-bottom:14px;}}
    label{{display:block;font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-muted);margin-bottom:6px;}}
    input{{width:100%;background:var(--bg2);border:1px solid var(--border-hi);color:var(--text);font-family:var(--mono);font-size:14px;padding:10px 14px;outline:none;}}
    input:focus{{border-color:var(--amber);box-shadow:0 0 0 1px var(--amber);}}
    button{{width:100%;margin-top:8px;background:none;border:1px solid var(--amber);color:var(--amber);font-family:var(--mono);font-size:12px;padding:11px;cursor:pointer;letter-spacing:1.5px;text-transform:uppercase;transition:background .12s;}}
    button:hover{{background:var(--amber-glow);}}
    .err{{margin-top:18px;font-size:11px;color:#ff4444;letter-spacing:.5px;text-align:center;}}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">nasearch</div>
    <div class="sub">file index</div>
    <form method="post" action="/login">
      <div class="field">
        <label for="u">username</label>
        <input id="u" name="username" type="text" autocomplete="username" autofocus required>
      </div>
      <div class="field">
        <label for="p">password</label>
        <input id="p" name="password" type="password" autocomplete="current-password" required>
      </div>
      <button type="submit">sign in →</button>
      {error}
    </form>
  </div>
</body>
</html>"""

@app.get("/login")
async def login_page():
    return HTMLResponse(_LOGIN_HTML.format(error=""))

@app.post("/login")
async def login_submit(username: str = Form(""), password: str = Form("")):
    if _check_credentials(username, password):
        token = _session_create()
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        max_age=SESSION_HOURS * 3600, path="/")
        return resp
    return HTMLResponse(
        _LOGIN_HTML.format(error='<p class="err">invalid credentials</p>'),
        status_code=401,
    )

@app.post("/api/logout")
async def logout(request: Request):
    _session_delete(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

# ── API ───────────────────────────────────────────────────────────────────────
@app.get("/api/search")
async def search(
    q: str = Query("", min_length=0),
    ext: Optional[str] = Query(None),
    limit: int = Query(200, le=MAX_RESULTS),
):
    if not q and not ext:
        return JSONResponse({"results": [], "total": 0, "truncated": False})

    if not Path(DB_PATH).exists():
        return JSONResponse(
            {"error": f"Index not found at {DB_PATH}. Trigger a re-index first."},
            status_code=503,
        )

    pattern = q if q else f"*.{ext.lstrip('.')}"

    # Cap locate's output at MAX_RESULTS — without -n, a short query can return
    # hundreds of thousands of paths and buffer them all in Python memory.
    # When q+ext are both set we need extra headroom because we post-filter.
    fetch_n = MAX_RESULTS if (q and ext) else limit
    cmd = ["locate", "-d", DB_PATH, "-i", "-n", str(fetch_n), "--", pattern]

    # Use async subprocess so we don't block the event loop while locate runs.
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return JSONResponse({"error": "Search timed out"}, status_code=504)
    except FileNotFoundError:
        return JSONResponse({"error": "'locate' not found in container"}, status_code=500)

    lines = [l for l in stdout.decode("utf-8", errors="replace").splitlines() if l.strip()]

    if ext and q:
        ext_clean = ext.lstrip(".").lower()
        lines = [l for l in lines if l.lower().endswith(f".{ext_clean}")]

    truncated = len(lines) > limit
    lines = lines[:limit]

    results = []
    for path in lines:
        p = Path(path)
        st = None
        is_dir = False
        try:
            st = p.stat()
            is_dir = stat_mod.S_ISDIR(st.st_mode)
        except OSError:
            pass
        size_bytes = st.st_size if (st and not is_dir) else None
        results.append({
            "path": path,
            "name": p.name,
            "dir": str(p.parent),
            "ext": "" if is_dir else p.suffix.lower().lstrip("."),
            "icon": get_icon(path),
            "size": format_size_bytes(size_bytes) if size_bytes is not None else None,
            "size_bytes": size_bytes,
            "mtime": int(st.st_mtime) if st else None,
            "is_dir": is_dir,
        })

    return JSONResponse({"results": results, "total": len(results), "truncated": truncated})


@app.get("/api/status")
async def status():
    settings = load_settings()
    db_exists = Path(DB_PATH).exists()
    db_size = format_size(DB_PATH) if db_exists else None
    return {
        "db_exists": db_exists,
        "db_size": db_size,
        "indexer": indexer_state,
        "last_indexed": settings.get("last_indexed"),
        "last_duration_seconds": settings.get("last_duration_seconds"),
        "interval_hours": settings.get("interval_hours", 24),
        "auth_enabled": bool(_auth_enabled),
    }


@app.post("/api/reindex")
async def reindex(background_tasks: BackgroundTasks):
    if indexer_state["running"]:
        return JSONResponse({"error": "Indexer already running"}, status_code=409)
    loop = asyncio.get_event_loop()
    background_tasks.add_task(loop.run_in_executor, None, run_index_sync)
    return {"ok": True, "message": "Indexing started"}


@app.post("/api/settings")
async def update_settings(body: dict):
    settings = load_settings()
    if "interval_hours" in body:
        val = int(body["interval_hours"])
        if val not in [0, 1, 6, 12, 24, 48, 168]:
            return JSONResponse({"error": "Invalid interval"}, status_code=400)
        settings["interval_hours"] = val
    save_settings(settings)
    return {"ok": True, "settings": settings}


@app.get("/api/file")
async def serve_file(
    path: str = Query(..., description="Absolute path within DATA_PATH"),
    dl: bool = Query(False, description="Force download (attachment) vs inline preview"),
):
    """Serve a file from the NAS for download or inline preview.

    Path is validated to be within DATA_PATH before serving.
    Supports HTTP Range requests (required for video/audio seeking).
    """
    full_path = safe_resolve(path)
    if not full_path:
        return JSONResponse({"error": "Access denied: path outside data root"}, status_code=403)
    if not full_path.exists() or not full_path.is_file():
        return JSONResponse({"error": "File not found"}, status_code=404)

    mime_type, _ = mimetypes.guess_type(str(full_path))
    mime_type = mime_type or "application/octet-stream"

    disposition = "attachment" if dl else "inline"
    encoded_name = quote(full_path.name, safe="")
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_name}",
        "Cache-Control": "private, max-age=3600",
    }

    return FileResponse(str(full_path), media_type=mime_type, headers=headers)


# ── Folder zip ────────────────────────────────────────────────────────────────

class _NonSeekableBuf:
    """Write-only non-seekable sink. Forces zipfile to use data descriptors
    (flag bit 3), so CRC/sizes are written *after* file data rather than
    requiring seek-back — making the stream truly appendable."""

    def __init__(self) -> None:
        self._buf: bytearray = bytearray()
        self._pos: int = 0

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return False

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


def _scan_folder(folder: Path) -> dict:
    """Count files and bytes under folder. Returns early once limits are hit."""
    root = Path(DATA_PATH).resolve()
    file_count = 0
    total_bytes = 0
    for entry in folder.rglob("*"):
        if not entry.is_file():
            continue
        # Skip symlinks that resolve outside DATA_PATH
        try:
            entry.resolve().relative_to(root)
        except ValueError:
            continue
        file_count += 1
        try:
            total_bytes += entry.stat().st_size
        except OSError:
            pass
        if file_count > ZIP_MAX_FILES or total_bytes > ZIP_MAX_BYTES:
            return {
                "ok": False,
                "file_count": file_count,
                "total_bytes": total_bytes,
                "error": (
                    f"Folder too large: {file_count}+ files / "
                    f"~{format_size_bytes(total_bytes)} "
                    f"(limit: {ZIP_MAX_FILES} files / {format_size_bytes(ZIP_MAX_BYTES)})"
                ),
            }
    return {
        "ok": file_count > 0,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "size_label": format_size_bytes(total_bytes) if file_count else "0 B",
        "error": "Folder is empty" if file_count == 0 else None,
    }


def _stream_zip(folder: Path) -> Iterator[bytes]:
    """Sync generator yielding raw ZIP bytes.

    Uses ZIP_STORED (no compression) because NAS content is typically already
    compressed, and it avoids both CPU overhead and the need for seeking.
    Each file is read in 256 KB chunks so memory usage stays flat.

    Starlette's StreamingResponse wraps sync generators via iterate_in_threadpool,
    so this runs in a worker thread and never blocks the event loop.
    """
    CHUNK = 256 * 1024
    buf = _NonSeekableBuf()

    root = Path(DATA_PATH).resolve()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for fpath in sorted(folder.rglob("*")):
            if not fpath.is_file():
                continue
            # Skip symlinks that resolve outside DATA_PATH
            try:
                fpath.resolve().relative_to(root)
            except ValueError:
                continue
            arcname = fpath.relative_to(folder).as_posix()
            try:
                st = fpath.stat()
                dt = datetime.fromtimestamp(st.st_mtime)
                info = zipfile.ZipInfo(
                    filename=arcname,
                    date_time=(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second),
                )
                info.compress_type = zipfile.ZIP_STORED
                with zf.open(info, "w") as zentry, open(fpath, "rb") as src:
                    while True:
                        data = src.read(CHUNK)
                        if not data:
                            break
                        zentry.write(data)
                        chunk = buf.drain()
                        if chunk:
                            yield chunk
            except (OSError, PermissionError):
                continue  # skip files that disappear or are unreadable

            # Drain data descriptor written when zentry closes
            chunk = buf.drain()
            if chunk:
                yield chunk

    # Central directory + end-of-central-directory record
    final = buf.drain()
    if final:
        yield final


@app.get("/api/zipcheck")
async def zip_check(path: str = Query(...)):
    """Return folder stats (file count, size) without downloading.
    The UI calls this before triggering /api/zip to surface errors early."""
    full_path = safe_resolve(path)
    if not full_path:
        return JSONResponse({"ok": False, "error": "Access denied"}, status_code=403)
    if not full_path.exists() or not full_path.is_dir():
        return JSONResponse({"ok": False, "error": "Not a directory"}, status_code=404)
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _scan_folder, full_path)
    return JSONResponse(info)


@app.get("/api/zip")
async def zip_folder_download(path: str = Query(...)):
    """Stream a folder as a ZIP_STORED archive.

    Runs a size-gate scan first (guards against direct URL access bypassing
    the frontend check). Then streams via a sync generator in a thread pool.
    """
    full_path = safe_resolve(path)
    if not full_path:
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not full_path.exists() or not full_path.is_dir():
        return JSONResponse({"error": "Not a directory"}, status_code=404)

    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _scan_folder, full_path)
    if not info["ok"]:
        return JSONResponse({"error": info["error"]}, status_code=413)

    encoded_name = quote(f"{full_path.name}.zip", safe="")
    return StreamingResponse(
        _stream_zip(full_path),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "X-File-Count": str(info["file_count"]),
            "X-Uncompressed-Size": str(info["total_bytes"]),
            "Cache-Control": "no-store",
        },
    )


app.mount("/", StaticFiles(directory="/app/static", html=True), name="static")
