# Dedupage

Page-aligned, content-addressed deduplication for incremental **mobile SQLite backup**,
and a measurement study of how redundant those backups actually are.

Most of a mobile app's SQLite database does not change between backups, yet the
common practice re-uploads the whole database or the raw write-ahead log every
time. Dedupage measures this cross-snapshot page redundancy on real Android
devices and exploits it: it stores each unique 4 KiB page once, keyed by its hash,
and optionally XOR-deltas a changed page against its prior version.

Paper: *Dedupage: How Redundant Are Mobile SQLite Backups?* (under submission; arXiv
link to be added). See [`RESULTS.md`](RESULTS.md) for the full measured results.

## Key measured results

- A realistic incremental change leaves **60.0 to 99.5 percent** of database pages
  untouched, and the fraction rises with database size (3 apps, 2 devices).
- Page-aligned deduplication with an XOR-delta stage uploads **4.2 to 60x** fewer
  bytes than Litestream, rsync, and FastCDC on realistic and content-recurrence
  workloads (all payloads compressed for fairness).
- On-device incremental-snapshot latency: **0.12 ms** median (Snapdragon S25+),
  **2.47 ms** (MediaTek Tab S10+). Per-backup energy is below the coulomb-counter
  resolution.
- Honest failure modes are reported: bulk imports and `VACUUM` defeat page
  alignment, and whole-page dedup does not transfer across devices.

## What is here

| Path | What it is |
|---|---|
| `fastcdc_cas.py` | FastCDC + SHA-256 content-addressed store (shared library / reference baseline) |
| `measure_page_dedup.py` | Offline measurer: cross-snapshot page-dedup vs FastCDC vs full-copy |
| `experiment_page_alignment.py` | Early page-aligned vs FastCDC experiment on a synthetic DB |
| `shim/page_dedup_vfs.py` | The live page-dedup VFS shim (Python/apsw) |
| `shim/page_dedup_vfs.c` | The same shim in C (host build; NDK cross-compile for on-device) |
| `shim/demo_rollback_journal.py` | Run + cross-validate the shim in rollback-journal mode |
| `shim/demo_wal_mode.py` | Run + cross-validate in WAL mode (parses `-wal` frame headers) |
| `shim/benchmark.py` | Benchmark vs Litestream/rsync/FastCDC/full-copy + XOR-delta |
| `shim/plot_bytes_per_backup.py`, `shim/plot_size_dependence.py` | Regenerate the paper figures |
| `pull_sqlite_snapshot.sh` | Pull a WAL-checkpointed SQLite snapshot over adb |
| `eval_extra/litestream_vs_pagededup.py` | Live litestream daemon vs page-dedup, measured |
| `eval_extra/failure_recovery.py` | Corrupted-object / interrupted-backup / interrupted-restore tests |
| `eval_extra/end_to_end.py` | End-to-end backup+restore over a throttled TCP link |
| `benchmark_results.csv` | Aggregate benchmark numbers (no personal content) |
| `RESULTS.md`, `DESIGN.md`, `IMPLEMENTATION_PLAN.md` | Findings and design docs |

## Running it

```bash
pip install -r requirements.txt        # apsw, matplotlib

# offline measurement on two snapshots (oldest first)
python3 measure_page_dedup.py snap0.db snap1.db

# live VFS shim + cross-validation against the offline tool
python3 shim/demo_rollback_journal.py
python3 shim/demo_wal_mode.py

# C shim (host build); the NDK cross-compile needs the SQLite amalgamation
cc -O2 shim/page_dedup_vfs.c -lsqlite3 -o dedupage && ./dedupage store work/app.db

# benchmark + figures
python3 shim/benchmark.py
python3 shim/plot_bytes_per_backup.py && python3 shim/plot_size_dependence.py
```

## Reproducing on your own device

The raw database snapshots used in the paper are **not** included: they are real
databases pulled from personal devices (podcast subscriptions, flashcard decks,
reading history). To reproduce, collect your own with `pull_db.sh`:

```bash
# make a snapshot, change something in the app, snapshot again, then measure
./pull_sqlite_snapshot.sh /sdcard/Android/data/<pkg>/.../your.db snapshots/snap0.db
./pull_sqlite_snapshot.sh /sdcard/Android/data/<pkg>/.../your.db snapshots/snap1.db
python3 measure_page_dedup.py snapshots/snap0.db snapshots/snap1.db
```

## Honest scope

This is a **measurement-and-systems** contribution. The mechanism (content-addressed
page deduplication, WAL-frame change detection, XOR-delta) combines known
techniques; the contribution is the first real-device measurement of cross-snapshot
page redundancy for mobile SQLite and an honest evaluation of where the approach
wins and where it does not.

## License

MIT. See [`LICENSE`](LICENSE).