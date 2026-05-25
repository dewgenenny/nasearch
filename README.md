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

### 3. Build the initial index

This will take a few minutes the first time depending on array size:

```bash
docker compose run --rm indexer
```

Then browse to **http://bertha:8000**

---

## Updating the index

### Manually

```bash
docker compose run --rm indexer
```

### Via cron (Unraid User Scripts plugin, or /etc/cron.d/)

```
0 3 * * * cd /opt/nasearch && docker compose run --rm indexer >> /var/log/nasearch-index.log 2>&1
```

---

## Configuration

Edit `docker-compose.yml` to adjust:

- `MAX_RESULTS` — cap on results returned (default 500)
- `--prunepaths` in the indexer command — directories to skip (add appdata, domains, etc.)
- Port mapping if 8000 is taken

---

## Excluding paths from the index

Edit the indexer command in `docker-compose.yml`:

```yaml
command: >
  bash -c "updatedb -l 0 -o /index/files.db -U /data
    --prunepaths '/data/appdata /data/system /data/domains'
    && echo 'Done.'"
```
