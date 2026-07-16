#!/usr/bin/env bash
# General: pull a consistent, WAL-checkpointed snapshot of any on-device SQLite DB.
# Usage: ./pull_sqlite_snapshot.sh <device_db_path> <local_out.db>
# Pulls main + -wal and folds the WAL in locally, so snapshots are always the
# committed state regardless of whether the app checkpointed on its own.
set -euo pipefail

SRC="${1:?usage: ./pull_sqlite_snapshot.sh <device_db_path> <local_out.db>}"
OUT="${2:?usage: ./pull_sqlite_snapshot.sh <device_db_path> <local_out.db>}"
RAW="$(dirname "$OUT")/.raw_$(basename "$OUT")"
mkdir -p "$(dirname "$OUT")" "$RAW"

adb pull "$SRC"     "$RAW/db"     >/dev/null
adb pull "$SRC-wal" "$RAW/db-wal" >/dev/null 2>&1 || true
sqlite3 "$RAW/db" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
cp "$RAW/db" "$OUT"
rm -rf "$RAW"

PAGES=$(python3 -c "import struct;d=open('$OUT','rb').read();print(len(d)//struct.unpack('>H',d[16:18])[0])")
echo "wrote $OUT  (pages=$PAGES)"