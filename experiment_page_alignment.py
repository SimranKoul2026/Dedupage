#!/usr/bin/env python3
"""
Core-hypothesis test for the "SQLite page-aware chunker" paper idea.

Claim under test:
  When a mobile SQLite database changes a LITTLE (a few rows updated/inserted),
  a PAGE-ALIGNED chunker must re-store far fewer bytes than FastCDC, because
  SQLite writes are page-local and page-aligned, while FastCDC boundaries are
  not aware of page structure and merge several pages into one chunk (so one
  changed page dirties a multi-page chunk).

We measure, for v1 -> v2 (v1 already in the store):
  bytes_to_store(v2) = sum(len(chunk) for chunk in v2 if chunk.hash not in v1)

Lower is better. We compare three chunkers:
  - fastcdc      : content-defined (restic/borg style), avg ~16KiB
  - fixed_page   : fixed blocks == SQLite page size (naive baseline)
  - page_aware   : parse SQLite header, one chunk per real page (our proposal)

No hardware/energy claims here. Pure, reproducible byte accounting.
"""
import hashlib
import os
import sqlite3
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fastcdc_cas import fastcdc, sha256  # reuse the same FastCDC + hashing


# --- chunkers ---------------------------------------------------------------
def chunks_fastcdc(data):
    for off, length in fastcdc(data):
        yield data[off:off + length]


def sqlite_page_size(data):
    # SQLite header: bytes 16-17 = page size (big-endian). Value 1 => 65536.
    if data[:16] != b"SQLite format 3\x00":
        return None
    ps = struct.unpack(">H", data[16:18])[0]
    return 65536 if ps == 1 else ps


def chunks_fixed(data, block):
    for i in range(0, len(data), block):
        yield data[i:i + block]


def chunks_page_aware(data):
    ps = sqlite_page_size(data)
    if ps is None:
        # not a sqlite file: fall back to fastcdc
        yield from chunks_fastcdc(data)
        return
    # The database file is an exact array of `ps`-byte pages.
    yield from chunks_fixed(data, ps)


# --- accounting -------------------------------------------------------------
def hashset(chunks):
    hs = set()
    total = 0
    for c in chunks:
        hs.add(sha256(c))
        total += len(c)
    return hs, total


def bytes_to_store(v1_chunks_fn, v2_chunks_fn, v1, v2):
    v1_hashes = {sha256(c) for c in v1_chunks_fn(v1)}
    stored = 0
    n_new = 0
    n_total = 0
    for c in v2_chunks_fn(v2):
        n_total += 1
        if sha256(c) not in v1_hashes:
            stored += len(c)
            n_new += 1
    return stored, n_new, n_total


# --- build the databases ----------------------------------------------------
def build_db(path, n_rows, seed_text="row"):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("PRAGMA page_size=4096")
    con.execute("PRAGMA journal_mode=DELETE")  # keep it single-file, no WAL
    con.execute("CREATE TABLE msgs(id INTEGER PRIMARY KEY, ts INTEGER, body TEXT)")
    con.executemany(
        "INSERT INTO msgs(id, ts, body) VALUES(?,?,?)",
        [(i, i * 1000, "%s message number %d with some padding text here" % (seed_text, i))
         for i in range(n_rows)],
    )
    con.commit()
    con.close()


def small_change(path, n_update=10, n_insert=10, vacuum=False):
    con = sqlite3.connect(path)
    # update a handful of existing rows
    for i in range(0, n_update * 137, 137):  # spread across the table
        con.execute("UPDATE msgs SET body=? WHERE id=?", ("EDITED body for row %d" % i, i))
    # insert a few new rows at the end
    m = con.execute("SELECT MAX(id) FROM msgs").fetchone()[0]
    con.executemany(
        "INSERT INTO msgs(id, ts, body) VALUES(?,?,?)",
        [(m + 1 + k, (m + 1 + k) * 1000, "appended row %d" % (m + 1 + k)) for k in range(n_insert)],
    )
    con.commit()
    if vacuum:
        con.execute("VACUUM")  # full file rebuild - the dedup-killer case
    con.close()


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024
    return "%.1f TB" % n


def run_scenario(label, vacuum):
    d = Path(__file__).parent / "sqlite_demo"
    d.mkdir(exist_ok=True)
    v1p = d / "app_v1.db"
    v2p = d / "app_v2.db"

    N = 50000
    build_db(v1p, N)
    import shutil
    shutil.copy(v1p, v2p)
    small_change(v2p, n_update=10, n_insert=10, vacuum=vacuum)

    v1 = v1p.read_bytes()
    v2 = v2p.read_bytes()
    ps = sqlite_page_size(v1)

    print("=" * 60)
    print("SCENARIO:", label)
    print("=" * 60)
    print("DB size v1 / v2      : %s / %s   page size %d B"
          % (human(len(v1)), human(len(v2)), ps))
    print("Change               : 10 rows updated + 10 inserted%s"
          % (" + VACUUM" if vacuum else " (no VACUUM)"))
    print()
    print("%-14s %14s %10s %12s" % ("chunker", "bytes-to-store", "new-chunks", "total-chunks"))
    print("-" * 54)

    configs = [
        ("fastcdc", chunks_fastcdc, chunks_fastcdc),
        ("fixed_page", lambda x: chunks_fixed(x, ps), lambda x: chunks_fixed(x, ps)),
        ("page_aware", chunks_page_aware, chunks_page_aware),
    ]
    results = {}
    for name, f1, f2 in configs:
        stored, n_new, n_total = bytes_to_store(f1, f2, v1, v2)
        results[name] = stored
        print("%-14s %14s %10d %12d" % (name, human(stored), n_new, n_total))

    print()
    fc, pa = results["fastcdc"], results["page_aware"]
    if pa > 0:
        print("page_aware vs fastcdc: %.1fx %s bytes to store"
              % (fc / pa if fc >= pa else pa / fc, "fewer" if fc >= pa else "MORE"))
    print("(v2 total size %s; ideal delta is only ~tens of KB.)" % human(len(v2)))
    print()


def main():
    run_scenario("realistic incremental write (what apps do most of the time)", vacuum=False)
    run_scenario("after VACUUM / compaction (the dedup-killer)", vacuum=True)
    print("Takeaway: page-alignment helps ONLY when writes stay page-local.")
    print("Compaction (VACUUM) rewrites the whole file and defeats every chunker.")
    print("That tension is itself a finding the paper must address.")


if __name__ == "__main__":
    main()