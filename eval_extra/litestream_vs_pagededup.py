"""
LIVE Litestream baseline vs our page-dedup+XOR-delta, on identical workloads.

Replaces the modeled Litestream number with a MEASURED one. We run the same
sequence of incremental changes twice:

  Run A (Litestream): a live WAL-mode DB replicated by a real litestream daemon
    to a local file replica. We measure the replica bytes it ships for the
    incremental changes (final replica size minus the post-seed baseline). LTX
    segments are compressed by litestream.

  Run B (ours): the same changes applied to a fresh DB; we checkpoint and copy a
    snapshot each interval, then compute page-dedup and page-dedup+XOR-delta
    (zlib-compressed) cumulatively, exactly as in shim/benchmark.py.

Both runs apply byte-identical logical operations, so the comparison is fair.
Litestream controls its own checkpointing, so we do NOT manually checkpoint
during Run A; Run B checkpoints for consistent file-copy snapshots.
"""
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from fastcdc_cas import sha256
import measure_page_dedup as off

LS = str(HERE / "litestream")
N_ROWS = 20000
N_INTERVALS = 5
PAGE = 4096


def dirsize(p):
    p = Path(p)
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def build_db(path):
    if Path(path).exists():
        Path(path).unlink()
    for ext in ("-wal", "-shm"):
        Path(str(path) + ext).unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA page_size=%d" % PAGE)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE msgs(id INTEGER PRIMARY KEY, ts INT, body TEXT)")
    con.executemany("INSERT INTO msgs VALUES(?,?,?)",
                    [(i, i * 1000, "message %d with padding text to fill the page" % i)
                     for i in range(N_ROWS)])
    con.commit()
    return con


def apply_change(con, i):
    # a realistic small incremental change: ~10 scattered updates + ~10 inserts
    for k in range(0, 10 * 137, 137):
        con.execute("UPDATE msgs SET body=? WHERE id=?",
                    ("edit interval %d row %d distinct text" % (i, k), k))
    m = con.execute("SELECT MAX(id) FROM msgs").fetchone()[0]
    con.executemany("INSERT INTO msgs VALUES(?,?,?)",
                    [(m + 1 + j, (m + 1 + j) * 1000, "appended i%d row %d" % (i, j)) for j in range(10)])
    con.commit()


def run_litestream():
    work = HERE / "ls_run"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    db = str(work / "app.db")
    replica = str(work / "replica")
    con = build_db(db)
    con.close()
    # start the real litestream daemon
    proc = subprocess.Popen([LS, "replicate", db, "file://" + replica],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)                       # let it take the initial (seed) snapshot
    baseline = dirsize(replica)
    con = sqlite3.connect(db)
    for i in range(1, N_INTERVALS + 1):
        apply_change(con, i)
        time.sleep(2.5)                # let litestream sync this interval's WAL
    con.close()
    time.sleep(3)                      # final sync window
    proc.send_signal(signal.SIGINT)    # graceful shutdown sync
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(1)
    final = dirsize(replica)
    return baseline, final, final - baseline


def run_ours():
    work = HERE / "ours_run"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    db = str(work / "app.db")
    con = build_db(db)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    snaps = [work / "s0.db"]
    shutil.copy(db, snaps[0])
    for i in range(1, N_INTERVALS + 1):
        apply_change(con, i)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        fp = work / ("s%d.db" % i)
        shutil.copy(db, fp)
        snaps.append(fp)
    con.close()

    page_store = set()
    prev_pages = None
    pd = pdd = 0
    for k, sp in enumerate(snaps):
        data = Path(sp).read_bytes()
        ps = off.sqlite_page_size(data)
        cur = off.pages(data, ps)
        for i, pg in enumerate(cur):
            h = sha256(pg)
            if h not in page_store:
                page_store.add(h)
                pd += len(zlib.compress(pg, 6))
                if prev_pages is not None and i < len(prev_pages) and len(prev_pages[i]) == ps:
                    xor = bytes(a ^ b for a, b in zip(pg, prev_pages[i]))
                    pdd += min(len(zlib.compress(pg, 6)), len(zlib.compress(xor, 6)))
                else:
                    pdd += len(zlib.compress(pg, 6))
        prev_pages = cur
    # subtract the seed (snapshot 0) so we compare INCREMENTAL bytes, like litestream baseline
    seed = Path(snaps[0]).read_bytes()
    ps = off.sqlite_page_size(seed)
    seed_bytes = sum(len(zlib.compress(pg, 6)) for pg in off.pages(seed, ps))
    return pd - seed_bytes, pdd - seed_bytes


def human(n):
    for u in ("B", "KB", "MB"):
        if abs(n) < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f GB" % n


def main():
    print("Workload: %d-row WAL DB, %d incremental intervals (~10 updates + 10 inserts each)\n"
          % (N_ROWS, N_INTERVALS))
    print("Running LIVE litestream ...")
    b, f, ls_inc = run_litestream()
    print("  litestream replica: baseline(after seed)=%s  final=%s  INCREMENTAL=%s"
          % (human(b), human(f), human(ls_inc)))
    print("Running ours (page-dedup and page-dedup+delta) ...")
    pd_inc, pdd_inc = run_ours()
    print("  page-dedup INCREMENTAL      = %s" % human(pd_inc))
    print("  page-dedup+delta INCREMENTAL = %s" % human(pdd_inc))
    print()
    print("=== MEASURED comparison (incremental bytes over %d intervals) ===" % N_INTERVALS)
    print("  litestream (live)   : %s" % human(ls_inc))
    print("  page-dedup          : %s  (%.2fx vs litestream)" % (human(pd_inc), ls_inc / pd_inc if pd_inc else 0))
    print("  page-dedup+delta    : %s  (%.2fx vs litestream)" % (human(pdd_inc), ls_inc / pdd_inc if pdd_inc else 0))


if __name__ == "__main__":
    main()