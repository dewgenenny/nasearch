# NASearch

Lightweight file search for self-hosted storage. FastAPI + plocate, no Elasticsearch, no bloat.

Point it at one or more directories, let it index, then search from a browser.

## Why not just use `find`?

The shell has excellent tools for this — `find`, `locate`, `fd`, `ripgrep` — and if you're already comfortable in a terminal they're great. NASearch is for a different situation: searching a large NAS array from a browser, sharing access with people who aren't comfortable on the command line, or quickly previewing a photo or video without downloading it first.

The bigger practical issue with `find /mnt/user | grep something` on a large array is speed. `find` traverses the filesystem in real time, touching every directory on every disk. On an array with millions of files across spinning drives, that can take several minutes. NASearch uses `plocate`, which pre-builds a compressed index — searches return in milliseconds regardless of array size. The trade-off is a small staleness window between index runs, handled by the configurable re-index schedule and manual re-index button.

---

## Install

### Unraid — Community Applications

Search for **NASearch** in the Community Applications store and install directly from there. All config fields (port, paths, auth) are exposed in the Unraid template UI.

### Manual / Docker Compose

### 1. Clone the repo onto your server

```bash
git clone https://github.com/dewgenenny/nasearch.git /opt/nasearch
cd /opt/nasearch
```

To update later:

```bash
cd /opt/nasearch
git pull
docker compose up -d --build
```

### 2. Configure your volumes

Edit `docker-compose.yml` to mount the directories you want indexed.

**Single volume** (simplest):

```yaml
volumes:
  - /mnt/user:/data:ro
  - ./index:/index
```

**Multiple volumes** — mount each source as a named subdirectory under `/data`:

```yaml
volumes:
  - /mnt/user:/data/nas:ro
  - /media/external:/data/external:ro
  - /home:/data/home:ro
  - ./index:/index
```

NASearch indexes the entire `/data` tree, so all mounted sources appear in search results. Result paths show the full mount path (e.g. `/data/nas/movies/...`, `/data/external/photos/...`), making it clear which volume a file lives on.

You can exclude noisy subdirectories with `PRUNE_PATHS` — see [Configuration](#configuration).

**On Unraid**, the default single-volume setup works out of the box:

```yaml
volumes:
  - /mnt/user:/data:ro
  - ./index:/index
```

### 3. Configure auth

NASearch **will not start** until you make an explicit auth choice. Open `docker-compose.yml` and uncomment one of the two options in the `# Auth` section:

**Option A — Session auth (recommended):**

```yaml
- AUTH_USER=admin
- AUTH_PASS=your-strong-password-here
```

**Option B — No auth (acknowledged risk):**

```yaml
- NOAUTH=true
```

Only use Option B on a fully trusted, firewalled network. See [Security](#security) below.

### 4. Build and start

```bash
docker compose up -d --build
```

The container builds the initial index on first start (may take a few minutes depending on how much data you have). Then browse to **http://your-server:8000**.

To trigger a manual re-index at any time:

```bash
curl -X POST http://localhost:8000/api/reindex
```

---

## Security

NASearch runs as root inside the container and can serve any file under `/data`. That's an intentional trade-off — see [Why root?](#why-the-container-runs-as-root) below. Because of this, the app **refuses to start** unless you have explicitly acknowledged the auth situation via one of the two options above.

### Session auth

Set both variables in `docker-compose.yml`:

```yaml
environment:
  - AUTH_USER=admin
  - AUTH_PASS=your-strong-password-here
```

NASearch uses session cookie authentication. Credentials are submitted once via a login form; the server validates them with a constant-time compare and issues a signed `httpOnly` session cookie (24-hour expiry by default, configurable with `SESSION_HOURS`). The cookie is scoped with `SameSite=lax` for CSRF protection. Credentials are never sent again after the initial login — subsequent requests just carry the session cookie.

### Use HTTPS — and think carefully before exposing it at all

Without TLS, the login POST itself is cleartext on the wire. If you do expose NASearch outside your local network, **you must put it behind a TLS-terminating reverse proxy**. [Nginx Proxy Manager](https://nginxproxymanager.com/) and [Caddy](https://caddyserver.com/) are popular options.

That said, our strong recommendation is **don't expose it to the internet at all**. NASearch is a root-running process that can read and serve every file on your array. Even with auth and TLS in place, you're one vulnerability away from exposing everything. If you need remote access, a VPN (Tailscale, WireGuard) is a much safer boundary — access your local NASearch instance over the VPN rather than punching a hole in your firewall.

### Why the container runs as root

NASearch runs as root inside the container by design. `updatedb` needs to crawl the entire directory tree — including paths owned by other users, Docker btrfs subvolumes, and system directories — to build a complete index. A restricted user silently misses anything it can't `stat`, defeating the point of the tool.

The actual risk surface is kept small by other means:

- All source volumes are mounted **read-only** — the process can never modify your files
- All file-serving paths are validated to prevent directory traversal outside `/data`
- Session cookie auth gates the entire UI when credentials are configured
- Docker's own namespace and cgroup isolation still applies

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_USER` | _(unset)_ | Login username; both must be set to enable session auth |
| `AUTH_PASS` | _(unset)_ | Login password |
| `SESSION_HOURS` | `24` | Session cookie lifetime in hours |
| `NOAUTH` | `false` | Set `true` to start without auth (acknowledged risk) |
| `DATA_PATH` | `/data` | Root path that is indexed and served |
| `LOCATE_DB` | `/index/files.db` | Path to the plocate database |
| `PRUNE_PATHS` | See compose file | Space-separated paths to skip during indexing |
| `MAX_RESULTS` | `500` | Cap on results returned per search |
| `ZIP_MAX_FILES` | `2000` | Max files in a folder zip download |
| `ZIP_MAX_BYTES` | `2147483648` | Max uncompressed size of a folder zip (2 GB) |

### Excluding paths from the index

Set `PRUNE_PATHS` as a space-separated list of absolute paths to skip:

```yaml
environment:
  - PRUNE_PATHS=/data/nas/appdata /data/nas/system /data/nas/isos
```

This is useful for high-churn directories (Docker appdata, VM images) that would otherwise bloat the index with paths you never search for.
