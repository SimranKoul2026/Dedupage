"""
Phase 1 driver + cross-validation.

Creates a DB fresh THROUGH the dedup VFS, applies a workload with snapshots, and
checks the live shim against the already-validated offline tool
(measure_page_dedup.py) on file copies taken at the same points:

  - CROSS-VALIDATION: per-snapshot set of NEW page hashes (live) == (offline).
  - RESTORE: reconstruct the final snapshot from store+manifests, byte-exact.
"""
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import apsw  # noqa: E402
import page_dedup_vfs  # noqa: E402
from fastcdc_cas import sha256  # noqa: E402
import measure_page_dedup as off  # noqa: E402

STORE = HERE.parent / "shim_store"
WORK = HERE / "work"


def main():
    for d in (STORE, WORK):
        if d.exists():
            shutil.rmtree(d)
    WORK.mkdir(parents=True)

    ctrl, vfs = page_dedup_vfs.setup(STORE)
    dbpath = str(WORK / "app.db")
    con = apsw.Connection(dbpath, vfs="dedup")
    cur = con.cursor()
    cur.execute("PRAGMA page_size=4096")
    cur.execute("PRAGMA journal_mode=DELETE")
    cur.execute("CREATE TABLE msgs(id INTEGER PRIMARY KEY, ts INT, body TEXT)")
    cur.executemany(
        "INSERT INTO msgs VALUES(?,?,?)",
        [(i, i * 1000, "message %d with padding text to fill the page" % i) for i in range(20000)],
    )

    snap_files = []

    def take(label):
        rec = ctrl.snapshot(label)                 # live snapshot
        fp = WORK / (label + ".db")
        shutil.copy(dbpath, fp)                     # file copy for offline check
        snap_files.append(str(fp))
        return rec

    take("s0")                                     # fresh DB (all pages new)
    cur.execute("UPDATE msgs SET body='EDITED a' WHERE id IN (10,500,1000)")
    take("s1")                                      # small scattered update
    cur.executemany("INSERT INTO msgs VALUES(?,?,?)",
                    [(20000 + i, i, "appended row %d" % i) for i in range(50)])
    cur.execute("UPDATE msgs SET body='EDITED b' WHERE id=7")
    take("s2")                                      # append + update
    con.close()

    print("=== LIVE shim (per snapshot) ===")
    for r in ctrl.snapshots:
        print("  %-3s dirty=%4d new=%4d bytes=%d" %
              (r["label"], r["dirty_pages"], r["new_pages"], r["bytes_uploaded"]))
    print("  cumulative bytes uploaded:", ctrl.bytes_uploaded)

    # OFFLINE cross-check on the file copies
    print("=== OFFLINE tool (per snapshot, on file copies) ===")
    seen = set()
    off_new_per = []
    for fp in snap_files:
        data = Path(fp).read_bytes()
        ps = off.sqlite_page_size(data)
        new = set()
        for pg in off.pages(data, ps):
            h = sha256(pg)
            if h not in seen:
                seen.add(h)
                new.add(h)
        off_new_per.append(new)
        print("  %-3s new=%4d" % (Path(fp).stem, len(new)))

    ok = True
    for r, offnew in zip(ctrl.snapshots, off_new_per):
        if r["new_hashes"] != offnew:
            ok = False
            print("  MISMATCH %s: live=%d offline=%d (sym-diff=%d)" %
                  (r["label"], len(r["new_hashes"]), len(offnew),
                   len(r["new_hashes"] ^ offnew)))
    print("=== CROSS-VALIDATION:", "PASS" if ok else "FAIL", "===")

    # RESTORE the final snapshot from store+manifests, byte-exact check
    out = page_dedup_vfs.restore(STORE, ["s0", "s1", "s2"], WORK / "restored_s2.db")
    recon = Path(out).read_bytes()
    orig = Path(snap_files[-1]).read_bytes()
    print("=== RESTORE s2 byte-exact:", recon == orig,
          "(%d vs %d bytes) ===" % (len(recon), len(orig)))

    return ok and recon == orig


if __name__ == "__main__":
    sys.exit(0 if main() else 1)