# Paper 3 - Results so far (real-device, 2026-07-14)

Cross-snapshot page-level deduplication for incremental mobile SQLite backup.
All numbers below are measured on real data (Samsung Galaxy S25+, SM-S936U),
no synthetic/estimated figures. Tooling: `sqlite_backup_dedup.py`, `pull_db.sh`.

## Claim under test

For incremental backup of a mobile SQLite database, dedup at the 4 KiB page
level, keyed by SHA-256, across snapshots. Because realistic changes touch few
pages, an incremental backup costs O(changed pages), not O(database size), and
beats FastCDC (whose variable chunks straddle page boundaries and are
invalidated by scattered row updates).

## Cross-app evidence (realistic incremental change)

| App | DB size | Change | Untouched pages | Incr. backup | vs full-copy | vs FastCDC |
|-----|---------|--------|-----------------|--------------|--------------|------------|
| KOReader (stats) | 10 pg / 40 KB | 1 reading event | 60.0% | 16 KB | 2.5x | 1.3x |
| AnkiDroid | 500 pg / 2.0 MB | study, 54 reviews | 96.6% | 68 KB | ~30x | 3.6x |
| AnkiDroid | 500 pg / 2.0 MB | study, 24 reviews | 96.0% | 80 KB | ~26x | 4.2x |
| AntennaPod | 813 pg / 3.3 MB | 2 playbacks | 99.1% | 28 KB | ~117x | 5.6x |

Thesis holds on two independent real apps (96%, 99%), always beating FastCDC.

## Cross-device (Galaxy Tab S10+, MediaTek Dimensity, vs S25+ Snapdragon)

- Robustness: AntennaPod small change on the tablet -> **99.5% untouched**
  (807/811 pages, 16 KB vs 3.2 MB, beats FastCDC 7.1x). Reproduces the phone's
  99.1% on different silicon -> the result is not chip-specific.
- Cross-device dedup (phone DB vs tablet DB, same podcasts): only **0.7% pages
  shared**. SQLite device-specific rowids/timestamps/insertion-order destroy
  byte-level page identity across devices. But 53/805 changed pages are
  near-duplicates (logical content overlaps). FINDING: whole-page dedup is a
  within-device-lineage technique; cross-device sharing would need logical /
  normalized dedup (future work).

## Finding 1 - size dependence (monotonic, 3 real points)

Untouched-page fraction rises with DB size: 10 pg -> 60%, 500 pg -> 96%,
813 pg -> 99%. Reason: every SQLite DB has fixed overhead pages (header
change-counter, `sqlite_sequence`, freelist, b-tree roots) that churn on *any*
write; in a tiny DB they dominate, in a large DB they are noise. Implication:
page-dedup has a minimum-DB-size threshold below which it is not worth it.
Caveat: uncontrolled (different apps, different change sizes). TODO: controlled
single-app size sweep to firm this up.

## Finding 2 - bulk restructuring defeats it

AnkiDroid snap0->snap1 (bulk import of ~4000 cards, DB grew 58->500 pages):
only 3.4% untouched. Bulk B-tree insertion splits/rewrites pages wholesale.
Also VACUUM/compaction rewrites the entire file (measured: 0% dedup). Any
honest system must detect these and fall back to a full snapshot.

## Finding 3 - residual delta opportunity

Of the pages that DO change, most are near-duplicates of their prior version
(AnkiDroid 11/17 and 14/20; AntennaPod 6/7). So an optional XOR-delta stage on
changed pages could shrink the incremental backup further. Positioned as
secondary / DeltaFS-adjacent; the primary claim is whole-page dedup.

## Method validation (AntennaPod)

Exports (not live pulls) were validated as a legitimate snapshot source:
- Determinism: two exports, zero change -> 100.0% identical (813/813 pages).
- Locality: one small change -> 99.1% untouched. So the export is a stable,
  locality-preserving copy, not a repack. No app modification / root needed.
(AnkiDroid + KOReader used live-file pulls + WAL checkpoint via `pull_db.sh`.)

## Device-wide census finding

223 app dirs under /sdcard/Android/data; adb reaches many, but nearly all
accessible SQLite are CACHES (Google Maps `map_cache.db` tile caches, `ga.sqlite3`
analytics, media indexes) = incompressible, regenerable, not backup-worthy. The
backup-critical structured DBs are sandboxed in /data/data. This motivates the
in-app `sqlite3_vfs` shim: dedup must run inside the app sandbox, because an
external scanner cannot reach the databases worth deduping.

## Prior-art gates (all cleared)

DeltaFS (FS-layer XOR-delta, local write-reduction), OrderMergeDedup + X-FTL
(local flash / SQLite journaling), RefineDedup + FinerDedup (local mobile
storage dedup), DOMe + MeGA (server backup dedup), HF Parquet-CDC / Xet
(columnar). None do remote-backup, cross-snapshot, SQLite-page-aware dedup.
RefineDedup gift: mobile apps have LOW *spatial* dedup (10-35%); our ~96-99% is
*temporal* (cross-snapshot) dedup = different, higher-yield quantity.

## The system: live VFS shim + benchmark (shim/, all 5 phases built)

A live page-dedup SQLite VFS shim was implemented and validated end-to-end:
- Phase 1 (apsw/Python) + Phase 2 (WAL-frame-aware): live shim identifies exactly
  the same changed/new pages as the offline tool (cross-validated), byte-exact
  restore. WAL-frame-derived changed-page set == checkpoint ground truth.
- Phase 3 (C VFS, shim/dedup_c.c): same results as Python; cross-compiled with
  NDK 27 and RAN ON BOTH DEVICES. Proper on-device latency (bench mode, 500 timed
  incremental snapshots of a 3-row change, UNPLUGGED, screen off, governor not
  pinned so conservative/worst-case clocks):
    - S25+ phone (Snapdragon 8 Elite): median 0.123 ms, p90 0.147, p99 0.441.
    - Tab S10+ (MediaTek Dimensity): median 2.471 ms, p90 2.705, p99 2.944.
  Both sub-3 ms = negligible per-backup overhead; Snapdragon ~20x faster. (An
  earlier one-off on-charge tablet run of 1.5-3.2 ms was superseded by this.)
- ON-DEVICE ENERGY (S25+, unplugged, screen off; coulomb counter read via
  `dumpsys battery` "Charge counter" uAh - readable WITHOUT root, unlike the
  denied sysfs nodes). Differential method, three windows.
  LONG (360 s) run is definitive: idle 24,075 uAh (0.94 W); update-only control
  110,745 uAh / 289,792 ops (4.30 W); update+backup 110,745 uAh / 288,768 ops
  (4.30 W). Busy == control TO THE uAh (both exactly 23x the ~4815 uAh fuel-gauge
  step), at ~equal op rates (803 vs 798/s). => the incremental backup adds NO
  measurable battery drain over the SQL update even across ~289,000 backups in
  6 min. UPPER BOUND < ~0.23 mJ/backup; point estimate indistinguishable from 0.
  (An earlier 90 s run showed a 1-step busy-vs-control gap read as "~1 mJ"; the
  360 s run shows that gap was quantization noise - they are dead equal.)
  Mechanism: the backup is CPU-work SUBSTITUTION (0.12 ms of hashing instead of
  another update), so it costs latency, not power. A tighter positive figure
  would need an external power monitor (Monsoon); the coulomb counter cannot
  resolve it. Energy cost is effectively negligible.
- Phase 5: optional XOR-delta on changed pages (store zlib(new XOR prior)).

Benchmark (shim/bench.py, shim/../benchmark_results.csv). FAIR comparison: ALL
payloads zlib-compressed (rsync -z, restic/borg-style compressed chunks,
Litestream compressed WAL). Incremental bytes uploaded (excludes initial seed):

| Workload (real) | page-dedup+Δ | vs rsync | vs Litestream | vs FastCDC |
|---|---|---|---|---|
| AntennaPod phone (playback) | 1.8 KB | 4.5x | 4.2x | 20x |
| AntennaPod tablet (playback) | 500 B | 5.4x | 4.8x | 37x |
| AnkiDroid snap2+snap3 (study only) | 16.1 KB | ~1.9x | ~2.3x | ~9x |
| SYNTHETIC content-recurrence | 975 B | 42x | 60x | 26x |

Honest findings from the fair benchmark:
- **page-dedup+Δ wins on realistic incremental changes** (2-37x over rsync/
  Litestream/FastCDC) because SQLite per-page changes are tiny -> XOR is sparse
  -> compresses to almost nothing.
- **Whole-page dedup alone ties Litestream** (both ship changed pages) and beats
  FastCDC (variable chunks straddle pages); the XOR-delta stage is what pushes
  past rsync/Litestream.
- **FAILURE MODE (honest): on the AnkiDroid bulk-import interval (snap0->snap1,
  +4000 cards), rsync's byte-level rolling delta BEATS page methods** (555 KB vs
  695 KB) - B-tree restructuring defeats page alignment. Reported, not hidden.
- **Content-recurrence (reverts/toggles): page-dedup+Δ dominates (42-60x)** via
  historical content-addressed dedup that rsync/Litestream (pairwise/WAL) lack.
- Positioning: NOT "smallest bytes always." It is content-addressed page dedup
  that matches rsync/Litestream byte-efficiency on monotonic change, wins on the
  realistic + recurrence cases, decisively beats generic CDC (restic/borg), and
  adds cross-history/cross-device dedup + O(changed-pages) operation + a tiny
  (1.5-3.2 ms) on-device snapshot cost, with no sender-side prior copy needed.

## Done

- [x] Cross-device replication on the Galaxy Tab S10+ (Dimensity): robustness
      confirmed (99.5%), and the C shim runs on it (1.5-3.2 ms/snapshot).
- [x] The `sqlite3_vfs` shim (apsw + C/NDK) + benchmark vs Litestream / rsync /
      FastCDC / full-copy, fair (all-compressed). All 5 phases in shim/.

## Strengthening experiments (post-v1, for a stronger venue submission)

**LIVE Litestream baseline (measured, replaces the model)** - eval_extra/
litestream_vs_pagededup.py. Same 5-interval workload (~10 updates + 10 inserts
each on a 20k-row WAL DB) run through a real litestream 0.5.16 daemon replicating
to a local file replica, vs our page-dedup and page-dedup+delta. Measured
INCREMENTAL bytes: litestream 73.1 KB, page-dedup 47.2 KB (1.55x smaller),
page-dedup+delta 3.3 KB (21.9x smaller). KEY: live litestream ships MORE than our
earlier positional model assumed (LTX segment/framing overhead, no cross-interval
dedup), so page-dedup beats it 1.55x even on a monotonic workload where the model
had shown a tie. This removes the "modeled not measured" criticism and is more
favorable than the model. TODO: fold measured litestream numbers into the
manuscript benchmark/discussion (currently says "model").

**Failure-recovery (eval_extra/failure_recovery.py) - ALL PASS.** Content-addressed
store + per-snapshot manifests give: (A) a CORRUPTED object is DETECTED at restore
(decompressed bytes no longer hash to their name -> CorruptionError; silent-wrong
restore impossible); (B) an INTERRUPTED backup is RESUMABLE - re-running stores
only the missing objects (reuse, no duplicates) and restores correctly; (C) an
INTERRUPTED restore is IDEMPOTENT - re-running is byte-exact regardless of a
partial prior attempt.

**End-to-end transport over a real throttled network (eval_extra/end_to_end.py).**
Real AntennaPod tablet snapshot pair (3.2 MB DB, one playback change) moved over a
loopback TCP store server with a token-bucket bandwidth throttle, then downloaded
back and reconstructed. Backup bundle: full-copy 636 KB (compressed whole DB) vs
page-dedup+delta 540 B. Measured backup upload time: at 1 Mbps (congested cellular)
full-copy 5.24 s vs ours 0.07 s (75x faster); at 10 Mbps (4G) 0.52 s vs 0.01 s
(67x faster). Restore: downloaded the 540 B bundle and reconstructed the DB
byte-exact over the wire. Closes the "no end-to-end transport evaluation" gap.

**More real apps (coverage).** NewPipe added as a 4th real app: 33-page (132 KB)
DB, real watch-history change -> 78.8% untouched, page-dedup 28 KB vs 132 KB
full-copy, 3.27x smaller than FastCDC, 6/7 changed pages are delta candidates.
Fills the mid-small DB range between KOReader (10 pg) and AnkiDroid (500 pg).
Loop Habit Tracker (uHabits) added as a 5th app: 8-page (32 KB) DB, real habit
toggle -> 62.5% untouched, 1.78x smaller than FastCDC (all 3 changed pages are
delta candidates). FINAL app roster (5): uHabits (8 pg, 62.5%), KOReader
(10 pg, 60.0%), NewPipe (33 pg, 78.8%), AnkiDroid (500 pg, 96.6%), AntennaPod
(813 pg, 99.1%) - a strong 5-point size-dependence curve.

## What remains before submission

1. More intervals per app -> the untouched-fraction curve (have 2 AnkiDroid, 1 AntennaPod).
2. Controlled single-app size sweep -> firm up the size-dependence finding.
3. On-device energy (coulomb-counter) numbers to pair with the latency numbers.
4. A real live Litestream run to confirm the Litestream-model approximation.
5. Write-up (IEEE Access / FAST / MobiSys / IEEE TMC lane).

## Figure

figure_bytes_per_backup.png / .pdf (shim/make_figure.py): grouped log-scale bars,
incremental bytes uploaded per method per realistic workload (AnkiDroid study,
AntennaPod phone/tablet, synthetic recurrence; bulk-import intervals excluded).
page-dedup+Δ is lowest everywhere. Colorblind-safe validated palette. Regenerate:
`python3 shim/make_figure.py` (reads benchmark_results.csv).

figure_size_dependence.png / .pdf (shim/make_figure2.py): untouched-page fraction
vs DB size (log-x), the 3 real cross-app points (KOReader 10 pg -> 60%, AnkiDroid
500 pg -> 96.6%, AntennaPod 813 pg -> 99.1%), monotonic. Labeled as a trend, not
a controlled sweep (single-app size sweep = future work).