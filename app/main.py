import subprocess
import os
import re
import sys
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
_DATA_ROOT     = os.path.normpath(DATA_PATH)    # textual normalisation — matches CodeQL's sanitiser pattern
_DATA_ROOT_SEP = _DATA_ROOT + os.sep
_DATA_ROOT_REAL = os.path.realpath(DATA_PATH)   # symlinks resolved — used by helper checks below
PRUNE_PATHS   = os.environ.get("PRUNE_PATHS", "/data/appdata /data/system /data/domains /data/isos")
MAX_RESULTS   = int(os.environ.get("MAX_RESULTS", "500"))
AUTH_USER     = os.environ.get("AUTH_USER",   "")
AUTH_PASS     = os.environ.get("AUTH_PASS",   "")
NOAUTH        = os.environ.get("NOAUTH",      "false").strip().lower() == "true"
ZIP_MAX_FILES  = int(os.environ.get("ZIP_MAX_FILES", "2000"))
ZIP_MAX_BYTES  = int(os.environ.get("ZIP_MAX_BYTES", str(2 * 1024 ** 3)))  # 2 GB
SESSION_HOURS  = int(os.environ.get("SESSION_HOURS", "24"))
COOKIE_SECURE  = os.environ.get("COOKIE_SECURE", "false").strip().lower() == "true"
SETTINGS_FILE  = "/index/settings.json"
SESSION_COOKIE = "nasearch_session"

# In-memory session store: token → {exp, csrf}
_sessions: dict[str, dict] = {}

def _session_create() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf  = secrets.token_urlsafe(32)
    _sessions[token] = {"exp": time.time() + SESSION_HOURS * 3600, "csrf": csrf}
    return token, csrf

def _session_valid(token: str | None) -> bool:
    if not token:
        return False
    entry = _sessions.get(token)
    if entry is None:
        return False
    if time.time() > entry["exp"]:
        _sessions.pop(token, None)
        return False
    return True

def _session_csrf(token: str | None) -> Optional[str]:
    entry = _sessions.get(token or "")
    return entry["csrf"] if entry else None

def _session_delete(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)

# ── CSRF ─────────────────────────────────────────────────────────────────────
def _csrf_ok(request: Request) -> bool:
    """Validate X-CSRF-Token header. Skipped in NOAUTH mode (no sessions to hijack)."""
    if not _auth_enabled:
        return True
    expected = _session_csrf(request.cookies.get(SESSION_COOKIE))
    if not expected:
        return False
    provided = request.headers.get("X-CSRF-Token", "")
    return bool(provided) and secrets.compare_digest(provided, expected)

# ── Login rate limiter ────────────────────────────────────────────────────────
_login_failures: dict[str, list[float]] = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW       = 900   # 15-minute sliding window + lockout

def _rate_limit_ok(ip: str) -> bool:
    now      = time.time()
    recent   = [t for t in _login_failures.get(ip, []) if now - t < _LOGIN_WINDOW]
    _login_failures[ip] = recent
    return len(recent) < _LOGIN_MAX_ATTEMPTS

def _rate_limit_record(ip: str) -> None:
    now = time.time()
    # Bound memory: drop IPs whose failures have all aged out of the window,
    # so an attacker rotating source IPs can't grow the dict indefinitely.
    if len(_login_failures) > 10_000:
        for k in [k for k, v in _login_failures.items() if not v or now - v[-1] >= _LOGIN_WINDOW]:
            _login_failures.pop(k, None)
    _login_failures.setdefault(ip, []).append(now)

def _rate_limit_clear(ip: str) -> None:
    _login_failures.pop(ip, None)

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
        file=sys.stderr,
    )
    raise SystemExit(1)

# Archive extensions that may be FUSE-mounted as directories on Unraid/similar NAS systems.
# Paths like /data/backup.zip/some/file.txt indicate the zip is mounted as a directory.
_ARCHIVE_RE = re.compile(
    r'\.(zip|7z|rar|tar\.gz|tgz|tar\.bz2|tbz2|tar\.xz|txz)/',
    re.IGNORECASE,
)
# Same extensions, anchored at the end of a directory name — used to find the
# archive directories that have to be handed to updatedb as explicit paths.
_ARCHIVE_DIR_RE = re.compile(
    r'\.(zip|7z|rar|tar\.gz|tgz|tar\.bz2|tbz2|tar\.xz|txz)$',
    re.IGNORECASE,
)
# updatedb takes --prunepaths as one space-separated string, so the list has a
# practical length ceiling. Beyond it we stop pruning and let the search-time
# filter cover the rest.
_PRUNEPATHS_MAX_CHARS = 100_000

DEFAULT_SETTINGS = {
    "interval_hours": 24,   # 0 = manual only
    "index_archives": True,  # when False, updatedb skips archive-mounted directories
    "last_indexed": None,
    "last_duration_seconds": None,
    "last_attempted": None,  # written on every run, success or failure
    "last_error": None,      # None after a successful run
}

# ── State ─────────────────────────────────────────────────────────────────────
indexer_state = {
    "running": False,
    "progress": None,
    "error": None,
    # Monotonic-ish wall clock of the last attempt. Held in memory as well as in
    # settings.json so the retry floor still holds when settings can't be
    # written — which is exactly the situation where indexing keeps failing.
    "last_attempt_ts": None,
    "archive_prune": None,
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
def _find_archive_dirs(root: str, already_pruned: set[str]) -> tuple[list[str], int]:
    """Enumerate directories whose name looks like a mounted archive.

    updatedb's --prunenames matches literal basenames, not globs, so the
    ``*.zip``-style patterns this used to pass were silently ignored and archive
    contents were indexed regardless of the setting. --prunepaths does take real
    paths, so they have to be found up front — one extra walk of the tree, which
    is why this only runs when archive indexing is turned off.

    Returns (prunable, unprunable_count). --prunepaths is a single
    space-separated string, so paths containing whitespace cannot be expressed
    and are counted rather than passed; the search-time filter covers those.
    """
    prunable: list[str] = []
    unprunable = 0
    budget = _PRUNEPATHS_MAX_CHARS

    for dirpath, dirnames, _ in os.walk(root, topdown=True, followlinks=False):
        keep = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if full in already_pruned:
                continue  # configured prune path — don't descend
            if not _ARCHIVE_DIR_RE.search(name):
                keep.append(name)
                continue
            # Archive directory: prune it if we can express it, and never
            # descend into it either way.
            if any(c.isspace() for c in full) or len(full) + 1 > budget:
                unprunable += 1
            else:
                prunable.append(full)
                budget -= len(full) + 1
        dirnames[:] = keep

    return prunable, unprunable


def _record_index_result(error: Optional[str], duration: Optional[float] = None) -> None:
    """Persist the outcome of an index run. Best-effort: bookkeeping must never
    mask the real failure, and the in-memory retry floor holds without it."""
    try:
        settings = load_settings()
        settings["last_error"] = error
        if error is None:
            settings["last_indexed"] = datetime.now(timezone.utc).isoformat()
            if duration is not None:
                settings["last_duration_seconds"] = round(duration)
        save_settings(settings)
    except Exception as e:
        print(f"[indexer] could not persist run outcome: {e!r}", file=sys.stderr)


def run_index_sync():
    """Runs updatedb in a thread. Safe to call from asyncio via run_in_executor."""
    if indexer_state["running"]:
        return False, "Indexer already running"

    indexer_state["running"] = True
    indexer_state["progress"] = "Starting updatedb…"
    indexer_state["error"] = None
    started = datetime.now(timezone.utc)
    # Recorded before anything can fail, so a run that dies mid-way still counts
    # as an attempt and the scheduler backs off instead of retrying immediately.
    indexer_state["last_attempt_ts"] = time.time()
    settings = load_settings()
    settings["last_attempted"] = started.isoformat()
    try:
        save_settings(settings)
    except Exception as e:
        print(f"[indexer] could not record attempt: {e!r}", file=sys.stderr)

    try:
        prune_paths = PRUNE_PATHS.split()
        if not settings.get("index_archives", True):
            indexer_state["progress"] = "Locating archives to skip…"
            archive_dirs, unprunable = _find_archive_dirs(DATA_PATH, set(prune_paths))
            prune_paths += archive_dirs
            indexer_state["archive_prune"] = {"pruned": len(archive_dirs), "unprunable": unprunable}
            if unprunable:
                print(
                    f"[indexer] {unprunable} archive directories could not be excluded at "
                    "index time (whitespace in path, or prune list too long); they are "
                    "filtered out of search results instead",
                    file=sys.stderr,
                )
        else:
            indexer_state["archive_prune"] = None

        indexer_state["progress"] = "Starting updatedb…"
        cmd = [
            "updatedb", "-l", "0",
            "-o", DB_PATH,
            "-U", DATA_PATH,
            "--prunepaths", " ".join(prune_paths),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        duration = (datetime.now(timezone.utc) - started).total_seconds()

        if result.returncode != 0:
            indexer_state["error"] = result.stderr.strip() or "updatedb exited with error"
            _record_index_result(indexer_state["error"])
            return False, indexer_state["error"]

        _record_index_result(None, duration)
        indexer_state["progress"] = None
        return True, f"Indexed in {round(duration)}s"

    except subprocess.TimeoutExpired:
        indexer_state["error"] = "Indexer timed out after 1 hour"
        _record_index_result(indexer_state["error"])
        return False, indexer_state["error"]
    except Exception as e:
        indexer_state["error"] = str(e)
        _record_index_result(str(e))
        return False, str(e)
    finally:
        indexer_state["running"] = False
        indexer_state["progress"] = None


# ── Scheduler ─────────────────────────────────────────────────────────────────
_SCHEDULER_POLL_SECONDS = 600   # longest single sleep, so settings changes land promptly
_INDEX_RETRY_FLOOR_SECONDS = 3600


def _parse_timestamp(value) -> Optional[datetime]:
    """Parse a settings timestamp, tolerating anything a hand-edit might leave."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def seconds_until_next_index() -> float:
    """How long to wait before the next automatic index. <= 0 means run now."""
    settings = load_settings()
    try:
        interval_hours = float(settings.get("interval_hours", 24))
    except (TypeError, ValueError):
        interval_hours = 24.0

    if interval_hours <= 0:
        return _SCHEDULER_POLL_SECONDS  # manual only — just keep watching settings

    now = datetime.now(timezone.utc)
    last_ok = _parse_timestamp(settings.get("last_indexed"))
    if last_ok is None:
        wait = 0.0  # never indexed successfully — do it now
    else:
        wait = interval_hours * 3600 - (now - last_ok).total_seconds()

    # Retry floor. Without it, a failing run leaves last_indexed untouched, the
    # wait above stays at 0, and updatedb re-crawls the whole array every minute
    # forever — keeping every disk spun up until someone notices.
    floor = min(_INDEX_RETRY_FLOOR_SECONDS, interval_hours * 3600)
    last_try_ts = indexer_state.get("last_attempt_ts")
    if last_try_ts is not None:
        wait = max(wait, floor - (time.time() - last_try_ts))
    else:
        last_try = _parse_timestamp(settings.get("last_attempted"))
        if last_try is not None:
            wait = max(wait, floor - (now - last_try).total_seconds())

    return wait


async def scheduler_loop():
    """Async loop that re-indexes on the configured interval.

    Sleeps in slices rather than one long sleep so an interval change takes
    effect without a restart, and treats any unexpected error as "retry later".
    An uncaught exception here used to kill the task silently — indexing would
    simply never happen again, with nothing surfaced in the UI.
    """
    while True:
        try:
            if indexer_state["running"]:
                await asyncio.sleep(60)
                continue

            wait = seconds_until_next_index()
            if wait > 0:
                await asyncio.sleep(min(wait, _SCHEDULER_POLL_SECONDS))
                continue

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_index_sync)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(
                f"[scheduler] unexpected error: {e!r} — retrying in "
                f"{_SCHEDULER_POLL_SECONDS}s",
                file=sys.stderr,
            )
            await asyncio.sleep(_SCHEDULER_POLL_SECONDS)


# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    global _scheduler_task
    _scheduler_task = asyncio.create_task(scheduler_loop())
    yield
    _scheduler_task.cancel()


app = FastAPI(lifespan=lifespan)


# ── Security headers (middleware) ────────────────────────────────────────────
# CSP for the app's own pages (SPA + login). The SPA uses inline <script>/<style>
# and loads marked/dompurify from jsdelivr (SRI-pinned), fonts from Google Fonts.
# object-src 'self' is required for the <embed> PDF preview; media/img cover the
# /api/file preview URLs. NOT applied to /api/ responses — /api/file sets its own
# sandbox policy below.
_PAGE_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "connect-src 'self'; "
    "object-src 'self'; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Content-Security-Policy", _PAGE_CSP)
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


def _within_data_root_realpath(path: str) -> bool:
    """Symlink-aware containment check. Kept separate from the CodeQL-recognised
    normpath+startswith barrier so the static analysis is not interrupted by the
    extra realpath() call."""
    rp = os.path.realpath(path)
    return rp == _DATA_ROOT_REAL or rp.startswith(_DATA_ROOT_REAL + os.sep)


def safe_resolve(path: str) -> Optional[Path]:
    """Validate path is within DATA_PATH and return the canonical absolute path.

    Uses the exact ``os.path.normpath`` + ``startswith`` pattern documented as
    a sanitiser in CodeQL's py/path-injection rule
    (https://codeql.github.com/codeql-query-help/python/py-path-injection/).
    A second pass with ``os.path.realpath`` then blocks symlink escapes, which
    textual normalisation alone does not catch.
    """
    # First layer — textual normalisation. CodeQL recognises this as a sanitiser.
    fullpath = os.path.normpath(path)
    if not (fullpath == _DATA_ROOT or fullpath.startswith(_DATA_ROOT + os.sep)):
        return None
    # Second layer — resolve any symlinks and re-verify against the real root.
    realpath = os.path.realpath(fullpath)
    if not (realpath == _DATA_ROOT_REAL or realpath.startswith(_DATA_ROOT_REAL + os.sep)):
        return None
    return Path(realpath)


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
    :root{--bg:#0a0a0a;--bg2:#111;--border:#252525;--border-hi:#333;--amber:#ffb300;--amber-glow:rgba(255,179,0,.07);--text:#d8d8d8;--text-muted:#383838;--mono:'IBM Plex Mono',monospace;}
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
    html,body{height:100%;background:var(--bg);color:var(--text);font-family:var(--mono);display:flex;align-items:center;justify-content:center;}
    .card{width:100%;max-width:340px;padding:0 24px;}
    .logo{font-size:22px;font-weight:500;color:var(--amber);letter-spacing:2px;margin-bottom:5px;}
    .logo::before{content:'> ';color:var(--text-muted);font-weight:300;}
    .sub{font-size:10px;color:var(--text-muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:40px;}
    .field{margin-bottom:14px;}
    label{display:block;font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-muted);margin-bottom:6px;}
    input{width:100%;background:var(--bg2);border:1px solid var(--border-hi);color:var(--text);font-family:var(--mono);font-size:14px;padding:10px 14px;outline:none;}
    input:focus{border-color:var(--amber);box-shadow:0 0 0 1px var(--amber);}
    button{width:100%;margin-top:8px;background:none;border:1px solid var(--amber);color:var(--amber);font-family:var(--mono);font-size:12px;padding:11px;cursor:pointer;letter-spacing:1.5px;text-transform:uppercase;transition:background .12s;}
    button:hover{background:var(--amber-glow);}
    .err{margin-top:18px;font-size:11px;color:#ff4444;letter-spacing:.5px;text-align:center;}
  </style>
  <script>
  /* Apply saved colour palette before first paint */
  (function(){
    try {
      var t=localStorage.getItem('nasearch_theme');
      if(!t||t==='dark') return;
      function h2r(h){var m=/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(h);return m?[parseInt(m[1],16),parseInt(m[2],16),parseInt(m[3],16)]:null;}
      function r2h(c){return'#'+c.map(function(v){return Math.round(Math.max(0,Math.min(255,v))).toString(16).padStart(2,'0');}).join('');}
      function bl(a,b,t){return a.map(function(v,i){return v*(1-t)+b[i]*t;});}
      var bg,tx,ac;
      var saved=JSON.parse(localStorage.getItem('nasearch_palette_'+t)||'null');
      if(saved){
        bg=h2r(saved.bg);tx=h2r(saved.text);ac=h2r(saved.accent);
      } else {
        var PR={light:['#f5f5f0','#1a1a18','#b06000'],purple:['#0d0a1a','#e8e0ff','#b388ff'],slate:['#0f1117','#c8d3e8','#38bdf8']};
        if(!PR[t]) return;
        bg=h2r(PR[t][0]);tx=h2r(PR[t][1]);ac=h2r(PR[t][2]);
      }
      if(!bg||!tx||!ac) return;
      var lum=bg[0]*.299+bg[1]*.587+bg[2]*.114,dk=lum<128;
      var st=dk?[255,255,255]:[0,0,0],sm=dk?.04:.06;
      var v='--bg:'+r2h(bg)+';--bg2:'+r2h(bl(bg,st,sm))+';--border:'+r2h(bl(bg,st,sm*4))
         +';--border-hi:'+r2h(bl(bg,st,sm*6))+';--amber:'+r2h(ac)
         +';--amber-glow:rgba('+ac.join(',')+',0.07)'
         +';--text:'+r2h(tx)+';--text-muted:'+r2h(bl(bg,tx,.50));
      document.head.insertAdjacentHTML('beforeend','<style>:root{'+v+'}</style>');
    } catch(e){}
  })();
  </script>
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
    return HTMLResponse(_LOGIN_HTML.replace("{error}", ""))

@app.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    if not _rate_limit_ok(ip):
        return HTMLResponse(
            _LOGIN_HTML.replace("{error}", '<p class="err">too many attempts — try again in 15 minutes</p>'),
            status_code=429,
        )
    if _check_credentials(username, password):
        _rate_limit_clear(ip)
        token, _ = _session_create()
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=COOKIE_SECURE,
                        max_age=SESSION_HOURS * 3600, path="/")
        return resp
    _rate_limit_record(ip)
    return HTMLResponse(
        _LOGIN_HTML.replace("{error}", '<p class="err">invalid credentials</p>'),
        status_code=401,
    )

@app.post("/api/logout")
async def logout(request: Request):
    if not _csrf_ok(request):
        return JSONResponse({"error": "CSRF token missing or invalid"}, status_code=403)
    _session_delete(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

# ── API ───────────────────────────────────────────────────────────────────────
async def _run_locate(
    extra_args: list[str],
    patterns: list[str],
    timeout: float = 10,
) -> tuple[Optional[str], Optional[JSONResponse]]:
    """Run locate against the index, returning (stdout, error_response).

    Async subprocess so the event loop isn't blocked while locate runs.
    Exactly one of the two return slots is ever populated.
    """
    cmd = ["locate", "-d", DB_PATH, "-i", *extra_args, "--", *patterns]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None, JSONResponse({"error": "Search timed out"}, status_code=504)
    except FileNotFoundError:
        return None, JSONResponse({"error": "'locate' not found in container"}, status_code=500)
    return stdout.decode("utf-8", errors="replace"), None


@app.get("/api/search")
async def search(
    q: str = Query("", min_length=0),
    ext: Optional[str] = Query(None),
    limit: int = Query(MAX_RESULTS, ge=1, le=MAX_RESULTS),
    no_archives: bool = Query(False),
):
    if not q and not ext:
        return JSONResponse({"results": [], "total": 0, "truncated": False, "total_matches": 0})

    if not Path(DB_PATH).exists():
        return JSONResponse(
            {"error": f"Index not found at {DB_PATH}. Trigger a re-index first."},
            status_code=503,
        )

    # locate ANDs multiple patterns together, so folding the extension in here
    # filters inside the index instead of after the fetch cap. Post-filtering
    # starved broad queries: the cap could be used up entirely by files of the
    # wrong extension before the filter ever ran.
    patterns = []
    if q:
        patterns.append(q)
    if ext:
        patterns.append(f"*.{ext.lstrip('.')}")

    # Archive contents are excluded on request, and always when indexing them is
    # turned off — see _find_archive_dirs() for why the index alone can't be
    # trusted to have kept them out.
    hide_archives = no_archives or not load_settings().get("index_archives", True)

    # Fetch one row beyond the page so the extra row reveals that more matches
    # exist. The archive filter runs after locate (plocate has no negative
    # patterns), so when it is active take the full cap as headroom instead.
    fetch_n = MAX_RESULTS + 1 if hide_archives else limit + 1

    # The listing and the true match count are two separate locate runs, so
    # issue them together rather than paying for them back to back. The count
    # is skipped when the archive filter is on, since locate would be counting
    # rows we are about to throw away.
    runs = [_run_locate(["-n", str(fetch_n)], patterns)]
    if not hide_archives:
        runs.append(_run_locate(["-c"], patterns, timeout=5))
    gathered = await asyncio.gather(*runs)

    listing, err = gathered[0]
    if err is not None:
        return err

    lines = [l for l in listing.splitlines() if l.strip()]
    # A full fetch window means locate had more to give, whether or not the
    # archive filter later thins the rows below the page size.
    window_full = len(lines) >= fetch_n
    if hide_archives:
        lines = [l for l in lines if not _ARCHIVE_RE.search(l)]

    truncated = len(lines) > limit or window_full
    lines = lines[:limit]

    total_matches = None
    if len(gathered) > 1:
        count_out, count_err = gathered[1]
        if count_err is None:
            try:
                total_matches = int(count_out.strip().splitlines()[0])
            except (ValueError, IndexError):
                pass  # unparseable count is not worth failing the search over
        if total_matches is not None and total_matches > len(lines):
            truncated = True

    results = []
    for path in lines:
        p = Path(path)
        results.append({
            "path": path,
            "name": p.name,
            "dir": str(p.parent),
            "ext": p.suffix.lower().lstrip("."),
            "icon": get_icon(path),
            "size": None,
            "size_bytes": None,
            "mtime": None,
            "is_dir": False,  # filled by /api/enrich
        })

    return JSONResponse({
        "results": results,
        "total": len(results),
        "truncated": truncated,
        "total_matches": total_matches,
    })


@app.get("/api/status")
async def status(request: Request):
    settings = load_settings()
    db_exists = Path(DB_PATH).exists()
    db_size = format_size(DB_PATH) if db_exists else None
    csrf = _session_csrf(request.cookies.get(SESSION_COOKIE)) if _auth_enabled else None
    return {
        "db_exists": db_exists,
        "db_size": db_size,
        "indexer": indexer_state,
        "last_indexed": settings.get("last_indexed"),
        "last_duration_seconds": settings.get("last_duration_seconds"),
        "last_attempted": settings.get("last_attempted"),
        "last_error": settings.get("last_error"),
        "interval_hours": settings.get("interval_hours", 24),
        "index_archives": settings.get("index_archives", True),
        "auth_enabled": bool(_auth_enabled),
        "csrf_token": csrf,
    }


@app.post("/api/enrich")
async def enrich_paths(request: Request, body: dict):
    """Stat a batch of paths and return is_dir / size / mtime.

    Called by the frontend after the initial search results render, so
    the search response itself never blocks on filesystem metadata.
    Each path is validated against DATA_PATH before being stat'd.
    """
    if not _csrf_ok(request):
        return JSONResponse({"error": "CSRF token missing or invalid"}, status_code=403)

    raw = body.get("paths", [])
    if not isinstance(raw, list):
        return JSONResponse({"error": "paths must be a list"}, status_code=400)

    # Validate and cap — reuse safe_resolve so traversal is impossible
    valid: list[tuple[str, Path]] = []
    for p in raw[:MAX_RESULTS]:
        if not isinstance(p, str):
            continue
        full = safe_resolve(p)
        if full:
            valid.append((p, full))

    def stat_all() -> dict:
        out: dict = {}
        for orig, full in valid:
            try:
                st = full.stat()
                is_dir = stat_mod.S_ISDIR(st.st_mode)
                size_bytes = st.st_size if not is_dir else None
                out[orig] = {
                    "is_dir":     is_dir,
                    "ext":        "" if is_dir else full.suffix.lower().lstrip("."),
                    "icon":       get_icon(str(full)),
                    "size":       format_size_bytes(size_bytes) if size_bytes is not None else None,
                    "size_bytes": size_bytes,
                    "mtime":      int(st.st_mtime),
                }
            except OSError:
                pass  # file disappeared between locate and stat — just omit it
        return out

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, stat_all)
    return JSONResponse(result)


@app.post("/api/reindex")
async def reindex(request: Request, background_tasks: BackgroundTasks):
    if not _csrf_ok(request):
        return JSONResponse({"error": "CSRF token missing or invalid"}, status_code=403)
    if indexer_state["running"]:
        return JSONResponse({"error": "Indexer already running"}, status_code=409)
    loop = asyncio.get_event_loop()
    background_tasks.add_task(loop.run_in_executor, None, run_index_sync)
    return {"ok": True, "message": "Indexing started"}


@app.post("/api/settings")
async def update_settings(request: Request, body: dict):
    if not _csrf_ok(request):
        return JSONResponse({"error": "CSRF token missing or invalid"}, status_code=403)
    settings = load_settings()
    raw = body.get("interval_hours")
    if raw is not None:
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid interval"}, status_code=400)
        if val not in [0, 1, 6, 12, 24, 48, 168]:
            return JSONResponse({"error": "Invalid interval"}, status_code=400)
        settings["interval_hours"] = val
    raw_archives = body.get("index_archives")
    if raw_archives is not None:
        settings["index_archives"] = bool(raw_archives)
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
    # Path sanitisation — exact pattern from CodeQL py/path-injection docs.
    fullpath = os.path.normpath(path)
    if not fullpath.startswith(_DATA_ROOT_SEP) and fullpath != _DATA_ROOT:
        return JSONResponse({"error": "Access denied: path outside data root"}, status_code=403)
    # Symlink-escape protection (separate from the CodeQL-recognised barrier above).
    if not _within_data_root_realpath(fullpath):
        return JSONResponse({"error": "Access denied: path outside data root"}, status_code=403)

    if not os.path.isfile(fullpath):
        return JSONResponse({"error": "File not found"}, status_code=404)

    mime_type, _ = mimetypes.guess_type(fullpath)
    mime_type = mime_type or "application/octet-stream"

    disposition = "attachment" if dl else "inline"
    encoded_name = quote(os.path.basename(fullpath), safe="")
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_name}",
        "Cache-Control": "private, max-age=3600",
    }

    if not dl:
        # Stored-XSS hardening: files on the array are untrusted content. Served
        # inline on this origin, an HTML/XML file would execute scripts with full
        # access to the app (session, CSRF token). Downgrade those types to plain
        # text — the SPA's text preview fetches raw bytes, so it is unaffected.
        if mime_type in ("text/html", "application/xhtml+xml", "text/xml", "application/xml"):
            mime_type = "text/plain"
        # SVG must keep its MIME type for <img> previews to work (nosniff is set
        # globally), so neutralise script execution on direct navigation instead.
        # PDFs are exempt: Chrome's viewer won't render under a sandbox policy.
        if mime_type != "application/pdf":
            headers["Content-Security-Policy"] = "sandbox"

    return FileResponse(fullpath, media_type=mime_type, headers=headers)


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


_ZIP_MIN_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_ZIP_MAX_DATE_TIME = (2107, 12, 31, 23, 59, 58)


def _zip_date_time(mtime: float) -> tuple:
    """Clamp an mtime into the range ZIP's DOS timestamp field can represent.

    ZipInfo raises ValueError for years before 1980, and years after 2107
    overflow the field. Epoch-0 mtimes are common on data restored from tape or
    copied by tools that drop metadata, and the raise happened mid-stream —
    after StreamingResponse had already committed to a 200 — so the client got
    a full-looking download with a truncated central directory.
    """
    try:
        dt = datetime.fromtimestamp(mtime)
    except (OSError, OverflowError, ValueError):
        return _ZIP_MIN_DATE_TIME
    if dt.year < 1980:
        return _ZIP_MIN_DATE_TIME
    if dt.year > 2107:
        return _ZIP_MAX_DATE_TIME
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def _scan_folder(folder: Path) -> dict:
    """Count files and bytes under folder. Returns early once limits are hit."""
    file_count = 0
    total_bytes = 0
    for entry in folder.rglob("*"):
        if not entry.is_file():
            continue
        entry_norm = os.path.normpath(str(entry))
        if not entry_norm.startswith(_DATA_ROOT_SEP) and entry_norm != _DATA_ROOT:
            continue
        if not _within_data_root_realpath(entry_norm):
            continue
        file_count += 1
        try:
            total_bytes += os.path.getsize(entry_norm)
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

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for fpath in sorted(folder.rglob("*")):
            if not fpath.is_file():
                continue
            fpath_norm = os.path.normpath(str(fpath))
            if not fpath_norm.startswith(_DATA_ROOT_SEP) and fpath_norm != _DATA_ROOT:
                continue
            if not _within_data_root_realpath(fpath_norm):
                continue
            arcname = fpath.relative_to(folder).as_posix()
            try:
                st = os.stat(fpath_norm)
                info = zipfile.ZipInfo(
                    filename=arcname,
                    date_time=_zip_date_time(st.st_mtime),
                )
                info.compress_type = zipfile.ZIP_STORED
                with zf.open(info, "w") as zentry, open(fpath_norm, "rb") as src:
                    while True:
                        data = src.read(CHUNK)
                        if not data:
                            break
                        zentry.write(data)
                        chunk = buf.drain()
                        if chunk:
                            yield chunk
            except (OSError, ValueError):
                # Skip files that disappear, are unreadable, or carry metadata
                # ZIP can't express. Never let one file abort the whole stream:
                # the response is already committed, so raising here would hand
                # the client a corrupt archive with no error.
                continue

            # Drain data descriptor written when zentry closes
            chunk = buf.drain()
            if chunk:
                yield chunk

    # Central directory + end-of-central-directory record
    final = buf.drain()
    if final:
        yield final


@app.get("/api/ziplist")
async def ziplist(path: str = Query(...)):
    import zipfile
    # Path sanitisation — see /api/file for rationale.
    fullpath = os.path.normpath(path)
    if not fullpath.startswith(_DATA_ROOT_SEP) and fullpath != _DATA_ROOT:
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not _within_data_root_realpath(fullpath):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.isfile(fullpath):
        return JSONResponse({"error": "File not found"}, status_code=404)

    try:
        with zipfile.ZipFile(fullpath, 'r') as zf:
            all_infos = sorted(zf.infolist(), key=lambda i: i.filename)
            truncated = len(all_infos) > MAX_RESULTS
            entries = []
            for info in all_infos[:MAX_RESULTS]:
                entries.append({
                    "name": info.filename,
                    "size": info.file_size,
                    "size_label": format_size_bytes(info.file_size) if not info.filename.endswith('/') else None,
                    "compressed": info.compress_size,
                    "is_dir": info.filename.endswith('/'),
                })
            return JSONResponse({"path": fullpath, "count": len(entries), "truncated": truncated, "entries": entries})
    except zipfile.BadZipFile:
        return JSONResponse({"error": "Not a valid zip file"}, status_code=400)
    except RuntimeError:
        # encrypted zip — don't expose the exception message
        return JSONResponse({"error": "File is encrypted or password-protected"}, status_code=400)


@app.get("/api/browse")
async def browse(path: str = Query(...)):
    # Path sanitisation — see /api/file for rationale.
    fullpath = os.path.normpath(path)
    if not fullpath.startswith(_DATA_ROOT_SEP) and fullpath != _DATA_ROOT:
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not _within_data_root_realpath(fullpath):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.exists(fullpath):
        return JSONResponse({"error": "Path not found"}, status_code=404)
    if not os.path.isdir(fullpath):
        return JSONResponse({"error": "Not a directory"}, status_code=400)

    entries = []
    try:
        with os.scandir(fullpath) as it:
            children = sorted(it, key=lambda e: (not e.is_dir(), e.name.lower()))
        for child in children:
            child_path = os.path.join(fullpath, child.name)
            is_dir = child.is_dir()
            st = None
            try:
                st = child.stat()
            except OSError:
                pass
            size_bytes = st.st_size if (st and not is_dir) else None
            ext = "" if is_dir else os.path.splitext(child.name)[1].lower().lstrip(".")
            entries.append({
                "path": child_path,
                "name": child.name,
                "dir": fullpath,
                "ext": ext,
                "icon": get_icon(child_path),
                "size": format_size_bytes(size_bytes) if size_bytes is not None else None,
                "size_bytes": size_bytes,
                "mtime": int(st.st_mtime) if st else None,
                "is_dir": is_dir,
            })
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)

    is_root = fullpath == _DATA_ROOT
    return JSONResponse({"path": fullpath, "is_root": is_root, "entries": entries})


@app.get("/api/zipcheck")
async def zip_check(path: str = Query(...)):
    """Return folder stats (file count, size) without downloading.
    The UI calls this before triggering /api/zip to surface errors early."""
    # Path sanitisation — see /api/file for rationale.
    fullpath = os.path.normpath(path)
    if not fullpath.startswith(_DATA_ROOT_SEP) and fullpath != _DATA_ROOT:
        return JSONResponse({"ok": False, "error": "Access denied"}, status_code=403)
    if not _within_data_root_realpath(fullpath):
        return JSONResponse({"ok": False, "error": "Access denied"}, status_code=403)
    if not os.path.isdir(fullpath):
        return JSONResponse({"ok": False, "error": "Not a directory"}, status_code=404)
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _scan_folder, Path(fullpath))
    return JSONResponse(info)


@app.get("/api/zip")
async def zip_folder_download(path: str = Query(...)):
    """Stream a folder as a ZIP_STORED archive.

    Runs a size-gate scan first (guards against direct URL access bypassing
    the frontend check). Then streams via a sync generator in a thread pool.
    """
    # Path sanitisation — see /api/file for rationale.
    fullpath = os.path.normpath(path)
    if not fullpath.startswith(_DATA_ROOT_SEP) and fullpath != _DATA_ROOT:
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not _within_data_root_realpath(fullpath):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.isdir(fullpath):
        return JSONResponse({"error": "Not a directory"}, status_code=404)

    folder_path = Path(fullpath)
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _scan_folder, folder_path)
    if not info["ok"]:
        return JSONResponse({"error": info["error"]}, status_code=413)

    encoded_name = quote(f"{folder_path.name}.zip", safe="")
    return StreamingResponse(
        _stream_zip(folder_path),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "X-File-Count": str(info["file_count"]),
            "X-Uncompressed-Size": str(info["total_bytes"]),
            "Cache-Control": "no-store",
        },
    )


app.mount("/", StaticFiles(directory="/app/static", html=True), name="static")
