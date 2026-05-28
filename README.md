# NASearch

Lightweight NAS file search. FastAPI + plocate, no Elasticsearch, no bloat.

## Setup on Unraid

### 1. Copy files to your Unraid box

```bash
scp -r nasearch/ root@bertha:/opt/nasearch
```

### 2. Build and start the web UI

```bash
cd /opt/nasearch
mkdir -p /opt/nasearch/index
docker compose up -d --build
```

The container will build the initial index automatically on first start (this may take a few minutes depending on array size). Then browse to **http://bertha:8000**

To trigger a manual re-index at any time:

```bash
curl -X POST http://localhost:8000/api/reindex
```

---

## Security

> **Auth is disabled by default.** Anyone who can reach port 8000 can search and download files from your array.

### Enable HTTP Basic Auth

Uncomment and set credentials in `docker-compose.yml`:

```yaml
environment:
  - AUTH_USER=admin
  - AUTH_PASS=your-strong-password-here
```

Both must be set or auth remains disabled.

### Use HTTPS

Basic auth credentials are sent Base64-encoded (effectively cleartext) in every request. **Always put NASearch behind a TLS-terminating reverse proxy before exposing it outside your local network.** On Unraid, [Nginx Proxy Manager](https://nginxproxymanager.com/) is the most common option.

### Rate limiting

The `/api/reindex` endpoint has no rate limiting. Anyone with access can trigger repeated re-indexing. Enable auth (above) if your instance is reachable beyond localhost.

---

## Configuration

Edit `docker-compose.yml` to adjust:

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_USER` | _(unset)_ | Basic auth username; auth disabled if either is unset |
| `AUTH_PASS` | _(unset)_ | Basic auth password |
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
