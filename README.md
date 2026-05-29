# NASearch

Lightweight NAS file search. FastAPI + plocate, no Elasticsearch, no bloat.

---

## Install

### 1. Clone the repo onto your Unraid box

```bash
ssh root@bertha
git clone https://github.com/dewgenenny/nasearch.git /opt/nasearch
cd /opt/nasearch
```

To update later:

```bash
cd /opt/nasearch
git pull
docker compose up -d --build
```

### 2. Configure auth

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

Only use Option B on a fully trusted, firewalled network. See the [Security](#security) section below.

### 3. Build and start

```bash
docker compose up -d --build
```

The container builds the initial index automatically on first start (may take a few minutes depending on array size). Then browse to **http://bertha:8000**.

To trigger a manual re-index at any time:

```bash
curl -X POST http://localhost:8000/api/reindex
```

---

## Security

NASearch runs as root inside the container and can serve any file on your array. That's an intentional trade-off — see [Why root?](#why-the-container-runs-as-root) below. Because of this, the app **refuses to start** unless you have explicitly acknowledged the auth situation via one of the two options above.

### Enable HTTP Basic Auth

Set both variables in `docker-compose.yml`:

```yaml
environment:
  - AUTH_USER=admin
  - AUTH_PASS=your-strong-password-here
```

Both must be set or auth is disabled. Credentials are validated with a constant-time compare to resist timing attacks.

### Use HTTPS

Basic auth credentials are Base64-encoded in every request (effectively cleartext). **Always put NASearch behind a TLS-terminating reverse proxy before exposing it outside your local network.** On Unraid, [Nginx Proxy Manager](https://nginxproxymanager.com/) is the most common option.

### Why the container runs as root

NASearch runs as root inside the container by design. `updatedb` needs to crawl the entire array — including paths owned by other users, Docker btrfs subvolumes, and system directories — to build a complete index. A restricted user would silently miss anything it can't `stat`, defeating the point of the tool.

The actual risk surface is kept small by other means:

- `/data` is mounted **read-only**, so the process can never modify your files
- All file-serving paths are validated to prevent directory traversal
- HTTP Basic Auth gates the entire UI when credentials are configured
- Docker's own namespace and cgroup isolation still applies

---

## Configuration

Edit `docker-compose.yml` to adjust:

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_USER` | _(unset)_ | Basic auth username; both must be set to enable auth |
| `AUTH_PASS` | _(unset)_ | Basic auth password |
| `NOAUTH` | `false` | Set `true` to start without auth (acknowledged risk) |
| `MAX_RESULTS` | `500` | Cap on results returned per search |
| `PRUNE_PATHS` | See compose file | Space-separated paths to skip during indexing |
| `ZIP_MAX_FILES` | `2000` | Max files in a folder zip download |
| `ZIP_MAX_BYTES` | `2147483648` | Max uncompressed size of a folder zip (2 GB) |

---

## Excluding paths from the index

Set `PRUNE_PATHS` in `docker-compose.yml` (space-separated):

```yaml
environment:
  - PRUNE_PATHS=/data/appdata /data/system /data/domains /data/isos
```
