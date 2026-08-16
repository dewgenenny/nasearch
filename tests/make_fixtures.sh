#!/usr/bin/env sh
# Build the fixture tree the regression tests look for.
#
# The tests need conditions that can't be committed to git (pre-1980 mtimes,
# directory names containing spaces, more files than MAX_RESULTS), so they are
# generated on the host and mounted in as /data.
#
#   sh tests/make_fixtures.sh /path/to/data-root
#   DEV_DATA_PATH=/path/to/data-root docker compose -f docker-compose.dev.yml up -d --build
#
# Point it at a dedicated throwaway directory, not your real array: it writes
# files at the top level of the root as well as under _nasearch_fixtures/, and
# wipes that subdirectory on each run.
#
# Tests that need these files skip themselves when the tree is absent, so the
# suite still runs against a plain data root.
set -eu

ROOT="${1:?usage: make_fixtures.sh <data-root>}"
FIX="$ROOT/_nasearch_fixtures"

# Loose files at the top level — several tests pick an arbitrary file directly
# under /data to work with, and skip when there isn't one.
echo "top-level fixture file" > "$ROOT/nasearchfixture_root.txt"
echo "<h1>fixture</h1>"       > "$ROOT/nasearchfixture_page.html"

rm -rf "$FIX"
mkdir -p "$FIX/oldstamp" "$FIX/archives" "$FIX/bulk"

# ── #5: a file ZIP's DOS timestamp field cannot represent ─────────────────────
echo "restored from tape" > "$FIX/oldstamp/ancient.txt"
echo "ordinary file"      > "$FIX/oldstamp/normal.txt"
touch -t 197001010101 "$FIX/oldstamp/ancient.txt"

# ── #6: archive-shaped directories, one of them unprunable (space in path) ────
mkdir -p "$FIX/archives/bundle.zip" "$FIX/archives/my bundle.zip"
echo x > "$FIX/archives/bundle.zip/nasearchfixture_in_archive.txt"
echo x > "$FIX/archives/my bundle.zip/nasearchfixture_in_spaced_archive.txt"
echo x > "$FIX/archives/nasearchfixture_outside_archive.txt"

# A genuine .zip file, so /api/ziplist has something real to open — the
# directories above only look like archives.
( cd "$FIX/archives" && rm -f real.zip &&
  python3 -c "import zipfile
with zipfile.ZipFile('real.zip','w') as z:
    z.writestr('nasearchfixture_zipped.txt', 'zipped content')" )

# ── #7/#8: more matches than MAX_RESULTS, with the rare extension sorting last
# so a post-fetch extension filter is guaranteed to starve.
i=1
while [ "$i" -le 600 ]; do
  echo x > "$FIX/bulk/nasearchbulk_$(printf '%04d' "$i").log"
  i=$((i + 1))
done
for n in 1 2 3; do
  echo x > "$FIX/bulk/nasearchbulk_zzz_rare_$n.dat"
done

echo "fixtures written to $FIX"
