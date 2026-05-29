# NASearch

Lightweight file search for self-hosted storage. FastAPI + plocate, no Elasticsearch, no bloat.

Point it at one or more directories, let it index, then search from a browser.

---

## Install

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

**Option A — HTTP Basic Auth (recommended):**

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

### HTTP Basic Auth

Set both variables in `docker-compose.yml`:

```yaml
environment:
  - AUTH_USER=admin
  - AUTH_PASS=your-strong-password-here
```

Both must be set. Credentials are validated with a constant-time compare to resist timing attacks.

### Use HTTPS

Basic auth credentials are Base64-encoded in every request (effectively cleartext). **Always put NASearch behind a TLS-terminating reverse proxy before exposing it outside your local network.** [Nginx Proxy Manager](https://nginxproxymanager.com/) and [Caddy](https://caddyserver.com/) are popular options.

### Why the container runs as root

NASearch runs as root inside the container by design. `updatedb` needs to crawl the entire directory tree — including paths owned by other users, Docker btrfs subvolumes, and system directories — to build a complete index. A restricted user silently misses anything it can't `stat`, defeating the point of the tool.

The actual risk surface is kept small by other means:

- All source volumes are mounted **read-only** — the process can never modify your files
- All file-serving paths are validated to prevent directory traversal outside `/data`
- HTTP Basic Auth gates the entire UI when credentials are configured
- Docker's own namespace and cgroup isolation still applies

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_USER` | _(unset)_ | Basic auth username; both must be set to enable auth |
| `AUTH_PASS` | _(unset)_ | Basic auth password |
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
