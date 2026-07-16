"""
Phase 2 driver: WAL-mode workload + WAL-frame-aware dirty tracking.

Real apps (AnkiDroid, KOReader) run SQLite in WAL mode, where page writes land
in the -wal file as frames, not the main DB. This driver demonstrates two things:

  1. The efficient design path: derive the changed-page set straight from the
     WAL frame headers (parse_wal_pages), no full-DB rescan.
  2. Ground-truth agreement: a checkpoint writes exactly those pages into the
     main DB (intercepted by the Phase 1 main-DB xWrite path). The WAL-derived
     set must EQUAL the checkpoint-observed set.

Then the usual cross-validation vs the offline tool, and byte-exact restore.
autocheckpoint is disabled so the WAL only flushes when we say so (clean compare).
"""
import shutil
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import apsw  # noqa: E402
import page_dedup_vfs  # noqa: E402
from fastcdc_cas import sha256  # noqa: E402
import measure_page_dedup as off  # noqa: E402

STORE = HERE.parent / "shim_store_wal"
WORK = HERE / "work_wal"


def main():
    for d in (STORE, WORK):
        if d.exists():
            shutil.rmtree(d)
    WORK.mkdir(parents=True)

    ctrl, vfs = page_dedup_vfs.setup(STORE)
    dbpath = str(WORK / "app.db")
    walpath = dbpath + "-wal"
    con = apsw.Connection(dbpath, vfs="dedup")
    cur = con.cursor()
    cur.execute("PRAGMA page_size=4096")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA wal_autocheckpoint=0")     # only WE checkpoint
    cur.execute("CREATE TABLE msgs(id INTEGER PRIMARY KEY, ts INT, body TEXT)")
    cur.executemany(
        "INSERT INTO msgs VALUES(?,?,?)",
        [(i, i * 1000, "message %d with padding text to fill the page" % i) for i in range(20000)],
    )

    snap_files = []
    agree = True

    def take(label):
        nonlocal agree
        # (1) WAL-derived changed-page set (efficient path), read before checkpoint
        walbytes = Path(walpath).read_bytes() if Path(walpath).exists() else b""
        ps = struct.unpack(">I", walbytes[8:12])[0] if len(walbytes) >= 12 else 4096
        wal_set = page_dedup_vfs.parse_wal_pages(walbytes, ps)
        # (2) checkpoint -> writes those pages into main DB (intercepted), ground truth
        cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        rec = ctrl.snapshot(label)
        main_set = rec["dirty_page_nos"]
        match = (wal_set == main_set)
        agree = agree and match
        print("  %-3s wal_frames_pages=%4d checkpoint_pages=%4d agree=%s new=%d" %
              (label, len(wal_set), len(main_set), match, rec["new_pages"]))
        shutil.copy(dbpath, WORK / (label + ".db"))    # checkpointed -> consistent
        snap_files.append(str(WORK / (label + ".db")))
        return rec

    print("=== WAL-derived vs checkpoint-observed changed pages ===")
    take("s0")
    cur.execute("UPDATE msgs SET body='EDITED a' WHERE id IN (10,500,1000)")
    take("s1")
    cur.executemany("INSERT INTO msgs VALUES(?,?,?)",
                    [(20000 + i, i, "appended row %d" % i) for i in range(50)])
    cur.execute("UPDATE msgs SET body='EDITED b' WHERE id=7")
    take("s2")
    con.close()
    print("=== WAL-frame tracking matches checkpoint ground truth:",
          "PASS" if agree else "FAIL", "===")

    # cross-validate vs offline tool on the checkpointed copies
    seen = set()
    off_new = []
    for fp in snap_files:
        data = Path(fp).read_bytes()
        ps = off.sqlite_page_size(data)
        new = set()
        for pg in off.pages(data, ps):
            h = sha256(pg)
            if h not in seen:
                seen.add(h)
                new.add(h)
        off_new.append(new)
    xok = all(r["new_hashes"] == o for r, o in zip(ctrl.snapshots, off_new))
    print("=== CROSS-VALIDATION vs offline tool:", "PASS" if xok else "FAIL", "===")

    out = page_dedup_vfs.restore(STORE, ["s0", "s1", "s2"], WORK / "restored_s2.db")
    rok = Path(out).read_bytes() == Path(snap_files[-1]).read_bytes()
    print("=== RESTORE s2 byte-exact:", rok, "===")

    return agree and xok and rok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)