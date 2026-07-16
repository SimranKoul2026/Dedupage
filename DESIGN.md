# Design: a page-dedup VFS shim for incremental SQLite backup

Status: design draft (2026-07-14). Companion to the measurement in
`sqlite_backup_dedup.py`. This is the systems artifact that upgrades the paper
from "measurement study" to "measurement + system + benchmark."

## Goal

A drop-in `sqlite3_vfs` that transparently ships only *changed pages* of a
SQLite database to a remote content-addressed store, so an incremental backup
costs O(changed pages) instead of O(database size). No app code changes, no
kernel/filesystem changes, no root. Portable across Android/Linux/desktop.

## Why a VFS (and not the Backup API or a file watcher)

- SQLite's own Backup API copies *all* changed pages unconditionally and has no
  dedup/remote layer (verified: sqlite.org/backup.html has no dedup).
- A file-level watcher sees only the whole file (or WAL) and cannot cheaply tell
  *which 4 KiB pages* changed without re-hashing everything each time.
- The VFS layer is exactly where SQLite issues page-granular reads/writes
  (`xRead`/`xWrite` at page offsets). Intercepting there gives us the changed
  page set for free, at the natural granularity our measurements already use.

## Architecture

```
  app  ->  SQLite core  ->  [dedup VFS shim]  ->  real VFS (unix/android)
                                   |
                                   +--> dirty-page tracker (in-RAM bitset)
                                   +--> on snapshot(): hash dirty pages,
                                        push absent chunks to remote store,
                                        write a tiny manifest (page->hash list)
```

The shim wraps a base VFS. It passes every call through, and additionally:

1. **`xWrite(page_offset, buf)`** — mark `page_no = offset / page_size` dirty in
   an in-memory bitset. (WAL mode: also handle `-wal` frame writes; a WAL frame
   header carries the target page number, so we mark that page dirty.)
2. **`snapshot()`** (our own API, or triggered on `xSync`/checkpoint) — for each
   dirty page: read current bytes, `SHA-256`, and if the hash is not already in
   the remote store, upload the 4 KiB chunk. Emit/append a manifest entry
   `{page_no: hash}`. Clear the bitset.
3. **restore(manifest, store)** — fetch each page's chunk by hash, write at
   `page_no * page_size`. (Already prototyped by `chunkstore`/`sqlite_backup_dedup`.)

The remote store is content-addressed (`store/objects/<hash>`), so a page whose
bytes recur across snapshots (or across devices) is stored once.

## WAL is the subtlety, and also the opportunity

Our measurements checkpoint the WAL, then diff whole pages. A production shim can
do better: SQLite's WAL is *already* a per-page change log. Each WAL frame names
the db page it supersedes. So in WAL mode the shim can read the changed-page set
directly from new WAL frames since the last snapshot, with **no dirty-tracking
bitset and no full-file rescan**. This is the natural, efficient design and it
mirrors how Litestream works, except Litestream ships raw WAL frames while we
ship *deduplicated, content-addressed* pages (a frame whose page content already
exists remotely costs nothing).

## Evaluation plan (vs baselines)

Baselines: (1) full-file copy each snapshot (naive), (2) `rsync` delta,
(3) FastCDC content-addressed (our `sqlite_backup_dedup` FastCDC column),
(4) **Litestream** (WAL-frame streaming, the real-world SOTA for SQLite backup).

Metrics per snapshot and cumulative:
- bytes uploaded (the headline),
- upload latency and CPU/energy on-device (Galaxy S25+, Pixel 10),
- restore correctness (byte-exact) and restore time.

Hypothesis from the real data so far: on realistic incremental intervals
(AnkiDroid study sessions: ~96% pages untouched) page-dedup uploads ~3.6-4.2x
fewer bytes than FastCDC, and beats raw-WAL Litestream whenever pages recur
(re-reviewing cards, re-reading pages, cross-device shared content). Where a page
changed only slightly (delta candidates, ~65% of changed pages in the AnkiDroid
data), an optional XOR-delta-against-prior-hash stage recovers most of the rest,
but delta is positioned as DeltaFS-adjacent and secondary.

## Open design questions

- Snapshot trigger: on every `xSync`, on WAL checkpoint, or on a timer? Affects
  granularity vs overhead. Measure.
- Page size discovery: read header offset 16 at open (already implemented).
- `auto_vacuum` / `VACUUM`: rewrites all pages, defeats dedup for that interval.
  Detect (page count shrink / sqlite_sequence reset) and fall back to full
  snapshot; report frequency in the workload study.
- Encryption/privacy for remote store (out of scope for v1, note as future work).