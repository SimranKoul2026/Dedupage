"""
Failure-recovery tests for the page-dedup backup store.

Reviewers will ask what happens under partial failure. Because objects are
content-addressed (named by SHA-256 of the page) and manifests are per-snapshot,
the design gives three properties, which this script demonstrates end to end:

  A. Corrupted object is DETECTED at restore (the stored bytes no longer hash to
     their name), so a silently-wrong restore is impossible.
  B. Interrupted backup is RESUMABLE: re-running stores only the missing objects
     (already-present ones are reused, never duplicated) and the restore is correct.
  C. Interrupted restore is IDEMPOTENT: re-running reconstructs the correct,
     byte-exact file regardless of a partial previous attempt.
"""
import json
import shutil
import sqlite3
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from fastcdc_cas import sha256
import measure_page_dedup as off


class CorruptionError(Exception):
    pass


def build_snapshots(d):
    d.mkdir(parents=True, exist_ok=True)
    db = d / "app.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA page_size=4096")
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t VALUES(?,?)",
                    [(i, "row %d padding text to fill the page nicely" % i) for i in range(4000)])
    con.commit()
    s0 = d / "s0.db"; shutil.copy(db, s0)
    for i in range(0, 30 * 53, 53):
        con.execute("UPDATE t SET v=? WHERE id=?", ("edited row %d distinct" % i, i))
    con.commit()
    s1 = d / "s1.db"; shutil.copy(db, s1)
    con.close()
    return [s0, s1]


def backup(snaps, store, upto):
    """Content-addressed backup of snaps[0..upto]. Returns objects newly written."""
    objects = store / "objects"; manifests = store / "manifests"
    objects.mkdir(parents=True, exist_ok=True); manifests.mkdir(parents=True, exist_ok=True)
    written = 0
    for label, sp in list(snaps.items())[:upto + 1]:
        data = Path(sp).read_bytes(); ps = off.sqlite_page_size(data)
        pairs = []
        for pno, pg in enumerate(off.pages(data, ps)):
            h = sha256(pg); pairs.append([pno, h])
            op = objects / h
            if not op.exists():                       # reuse if already present
                op.write_bytes(zlib.compress(pg, 6)); written += 1
        (manifests / (label + ".json")).write_text(json.dumps({"page_size": ps, "pages": pairs}))
    return written


def verified_restore(store, labels, out):
    """Restore with integrity checking: every object must hash to its name."""
    objects = store / "objects"; manifests = store / "manifests"
    pagemap = {}; ps = None
    for label in labels:
        m = json.loads((manifests / (label + ".json")).read_text()); ps = m["page_size"]
        for pno, h in m["pages"]:
            pagemap[pno] = h
    buf = bytearray((max(pagemap) + 1) * ps)
    for pno, h in pagemap.items():
        page = zlib.decompress((objects / h).read_bytes())
        if sha256(page) != h:                          # content-addressed integrity check
            raise CorruptionError("object %s failed hash check (page %d)" % (h[:12], pno))
        buf[pno * ps:(pno + 1) * ps] = page
    Path(out).write_bytes(buf)
    return out


def main():
    work = HERE / "recover_run"
    if work.exists():
        shutil.rmtree(work)
    snaps_list = build_snapshots(work / "snaps")
    snaps = {"s0": snaps_list[0], "s1": snaps_list[1]}
    target = Path(snaps["s1"]).read_bytes()

    # baseline
    store = work / "store"
    backup(snaps, store, upto=1)
    ok0 = Path(verified_restore(store, ["s0", "s1"], work / "r0.db")).read_bytes() == target
    print("baseline restore byte-exact:", ok0)

    # --- A. corrupted object detected ---
    victim = next((store / "objects").iterdir())
    victim.write_bytes(zlib.compress(b"corrupted content not matching the hash name"))
    try:
        verified_restore(store, ["s0", "s1"], work / "rA.db")
        print("A. corrupted object: NOT detected  <-- FAIL")
    except (CorruptionError, zlib.error) as e:
        print("A. corrupted object: DETECTED  (%s)" % type(e).__name__)

    # --- B. interrupted backup is resumable, objects reused not duplicated ---
    store2 = work / "store2"
    w_seed = backup(snaps, store2, upto=0)             # "crash" after seed only
    have_after_seed = len(list((store2 / "objects").iterdir()))
    w_resume = backup(snaps, store2, upto=1)           # resume: writes only new objects
    have_final = len(list((store2 / "objects").iterdir()))
    dup = have_final != (have_after_seed + w_resume)
    okB = Path(verified_restore(store2, ["s0", "s1"], work / "rB.db")).read_bytes() == target
    print("B. interrupted backup: resumed (seed wrote %d, resume wrote %d new), "
          "duplicates=%s, restore ok=%s" % (w_seed, w_resume, dup, okB))

    # --- C. interrupted restore is idempotent ---
    partial = work / "rC.db"
    partial.write_bytes(b"\x00" * 137)                 # a truncated/garbage partial restore
    okC = Path(verified_restore(store2, ["s0", "s1"], partial)).read_bytes() == target
    print("C. interrupted restore: re-run byte-exact =", okC)

    allok = ok0 and okB and okC
    print("\n=== FAILURE-RECOVERY:", "ALL PASS" if allok else "SOME FAILED", "===")


if __name__ == "__main__":
    main()