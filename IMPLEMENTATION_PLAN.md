# Implementation plan: page-dedup SQLite VFS shim + benchmark

Executable plan for next session. Companion to `VFS_SHIM_DESIGN.md` (the "why")
and `RESULTS.md` (the measured evidence the shim must reproduce live).

Goal: a live, in-process VFS that intercepts SQLite page writes and ships only
changed, content-addressed pages to a store, plus a benchmark showing it beats
Litestream / rsync / FastCDC / full-copy on bytes uploaded. The offline tool
`sqlite_backup_dedup.py` already computes the target numbers; the shim does the
same thing live, and the benchmark proves it against real backup tools.

Interop rule: the shim MUST write the same store layout as `chunkstore.py`
(`store/objects/<sha256>`) and a manifest the Python tools can read. That gives
free cross-validation: live shim output must byte-match the offline tool on the
same workload.

--------------------------------------------------------------------------------
## Phase 0 - Setup (0.5 day)

- Vendor the SQLite amalgamation (`sqlite3.c`, `sqlite3.h`) into `shim/` for a
  self-contained C build later. `curl https://sqlite.org/2026/sqlite-amalgamation-*.zip`.
- `pip install apsw` (Another Python SQLite Wrapper) - exposes real SQLite's VFS
  layer to Python via subclassing. This is the fast path for Phase 1.
- Decide store dir convention: `shim_store/objects/<hash>`, manifests
  `shim_store/manifests/<snapshot_id>.json` (`{page_size, pages:[[page_no,hash]]}`).
- Add `requirements.txt` (apsw) and a `shim/` subdir.

Risk gate: if apsw won't install on the Mac's Python 3.9, jump straight to the
C prototype (Phase 3) - it has no Python dependency.

--------------------------------------------------------------------------------
## Phase 1 - Live VFS prototype in apsw (2-3 days) [fastest path to a working shim]

Why apsw first: it wraps *real* SQLite, so the VFS semantics are authentic, but
we write Python and reuse `chunkstore.sha256` + the store format directly. Proves
the mechanism end-to-end and produces byte numbers with minimal risk.

Build `shim/dedup_vfs.py`:

1. Subclass `apsw.VFS` wrapping the default VFS; subclass `apsw.VFSFile`.
2. In `VFSFile.xWrite(data, offset)`:
   - if this file is the main DB (not -wal/-journal/-shm): mark
     `page_no = offset // page_size` dirty in a Python `set`.
   - delegate to super().xWrite.
   - page_size: read header bytes 16-17 lazily on first write, or accept as ctor arg.
3. `snapshot(manifest_path)` method (called by the harness, e.g. on
   `PRAGMA wal_checkpoint` or explicitly):
   - for each dirty page_no: read current page bytes (super().xRead at
     page_no*page_size), sha256, if hash not in store -> write
     `store/objects/<hash>` (zlib), count bytes_uploaded += len; append
     `[page_no, hash]` to manifest; clear dirty set.
4. `restore(manifest, store, out_path)` - already exists in
   `sqlite_backup_dedup.py`; reuse it to reconstruct and assert byte-exact.

WAL handling in Phase 1: run the workload DB in `PRAGMA journal_mode=DELETE`
(writes go straight to the main db file, so `xWrite`->page_no is exact). Note
this simplification in the paper; Phase 2 removes it.

Validation (the key correctness check):
- Driver applies a workload to a DB through the dedup VFS, calling snapshot()
  at intervals. Independently, take plain file-copy snapshots of the same DB and
  run `sqlite_backup_dedup.py` on them.
- ASSERT: live bytes_uploaded == offline page-store bytes, and the page-hash
  sets match exactly. If they match, the shim is correct against the already-
  validated offline tool.

Deliverable: a working live dedup VFS + a passing cross-validation test.

--------------------------------------------------------------------------------
## Phase 2 - WAL-aware dirty tracking (1-2 days) [production realism]

Real apps (AnkiDroid, KOReader) use WAL. In WAL mode, page writes land in the
`-wal` file as frames, not the main DB. So:

1. Intercept `xWrite` to the `-wal` file.
2. Parse the WAL format: 32-byte WAL header, then frames of
   [24-byte frame-header + page_size payload]. Frame-header bytes 0..3 =
   big-endian target page number. Mark that page dirty.
3. On checkpoint, the changed-page set is exactly the union of WAL frame page
   numbers since the last snapshot - no bitset scan of the whole DB needed.
   This is the efficient production design and directly parallels how Litestream
   consumes WAL, except we ship deduplicated pages.

Validation: repeat Phase 1 cross-check but with `journal_mode=WAL`; live output
must still match offline `sqlite_backup_dedup.py` on WAL-checkpointed snapshots.

--------------------------------------------------------------------------------
## Phase 3 - C VFS port (3-5 days) [needed only for on-device latency/energy]

Port the hot path to C for production-representative numbers and Android/NDK.

`shim/dedupvfs.c`:
- Register a shim `sqlite3_vfs` wrapping `unix` (or `unix-excl`); wrap
  `sqlite3_file` with a struct holding the base file + dirty-page bitset.
- `xWrite`: same dirty-tracking logic (main-db page_no, or WAL frame parse).
- Public API:
  - `int dedupvfs_register(const char *base, const char *store_dir);`
  - `int dedupvfs_snapshot(const char *manifest_path);`  // hash+upload dirty, write manifest
  - `void dedupvfs_stats(uint64_t *bytes_uploaded, uint64_t *pages_new, uint64_t *pages_dedup);`
- SHA-256: vendor a small public-domain impl or use the platform's.
- Store format identical to Phase 1 so Python tools still validate/restore.
- Build (Mac): `cc -O2 sqlite3.c dedupvfs.c driver.c -o dedupdriver`.
- Build (Android): NDK `aarch64-linux-android` toolchain -> push binary via adb,
  run on S25+ / Tab S10+ for latency + coulomb-counter energy (see
  BATTERY-BUDGET notes in [[battery-budget-router-project]] for the
  BATTERY_PROPERTY_CHARGE_COUNTER caveats).

Validation: C shim output must byte-match Phase 1/2 and the offline tool.

--------------------------------------------------------------------------------
## Phase 4 - Benchmark harness (2-3 days) [the paper's comparison table]

Compare bytes-uploaded per interval + cumulative, on the REAL captured workloads
(AnkiDroid snap0..3, AntennaPod ap_snap*, tablet tab_snap*) and on longer
generated ones:

Baselines:
1. full-copy       = sum of snapshot sizes (naive).
2. rsync           = `rsync --only-write-batch` delta size between consecutive
                     snapshots (or `--stats` bytes-sent).
3. FastCDC         = existing FastCDC column in `sqlite_backup_dedup.py`.
4. Litestream      = install (`brew install benbjohnson/litestream/litestream`
                     or release binary); `litestream replicate` the DB to a local
                     file replica while replaying the workload; measure replica
                     generation size / WAL segment bytes shipped.
5. page-dedup      = our shim's bytes_uploaded.
6. page-dedup+delta (optional, Phase 5).

Metrics: bytes uploaded (headline), restore correctness (byte-exact), and on the
C build: upload latency + energy/query on device.

Output: one CSV + a `dataviz`-styled figure (bytes vs interval, per method) and
the cumulative-savings bar. (Load the dataviz skill before drawing.)

--------------------------------------------------------------------------------
## Phase 5 - Optional XOR-delta stage (1-2 days) [only if reviewers want the tail]

For changed pages that are near-duplicates of their prior version (RESULTS.md:
6/7, 11/17, 14/20 pages), add an optional stage: instead of storing the whole
new page, store XOR-delta vs the prior page's content (keyed by prior hash).
Positioned as secondary / DeltaFS-adjacent. Measure extra savings; report
honestly (it helps the tail, adds read-dependency on the base page).

--------------------------------------------------------------------------------
## Sequencing & effort

- Minimum publishable system: Phase 0 + 1 + 2 + 4 (~1.5 weeks) = live WAL-aware
  dedup VFS + benchmark beating Litestream on bytes. This alone upgrades the
  paper from measurement-study to system+measurement.
- Add Phase 3 only if you want on-device latency/energy numbers (strengthens the
  mobile-computing framing; uses the S25+ / Tab S10+ already set up).
- Phase 5 only if a reviewer pushes on the changed-page tail.

## Definition of done

1. Live shim reconstructs every test DB byte-exact (restore == original).
2. Live bytes-uploaded matches offline `sqlite_backup_dedup.py` (cross-validated).
3. Benchmark table: page-dedup < Litestream < FastCDC < rsync < full-copy on
   bytes for the realistic-change intervals, with the numbers from RESULTS.md
   reproduced live.
4. One figure + CSV committed under ChunkStore/.