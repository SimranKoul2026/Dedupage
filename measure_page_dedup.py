#!/usr/bin/env python3
"""
measure_page_dedup - the measurement harness for the SQLite/mobile paper.

Answers the ONE question the paper hinges on (and that DeltaFS never measured):

  Across a sequence of real backup snapshots of a SQLite database, what fraction
  of 4 KiB pages are byte-identical to pages already seen in earlier snapshots?

That "untouched-page fraction" is the ceiling on what whole-page content-addressed
dedup can save for remote backup. DeltaFS's own data (updated pages differ ~13.8%)
implies changed pages will NOT dedup, so the win lives entirely in untouched pages.

For each snapshot i (compared against the cumulative store of pages from 0..i-1):
  - page_total, page_dedup (hash already stored), page_new
  - untouched_frac = page_dedup / page_total          <- the headline number
  - bytes_stored (page-dedup)   = page_new * page_size
  - bytes_stored (FastCDC)      = FastCDC chunks not already in the FastCDC store
  - bytes_stored (full copy)    = whole snapshot (the naive Litestream-ish upload)

It also measures the DELTA OPPORTUNITY that dedup leaves on the table: among NEW
(changed) pages, how many have a same-index page in the previous snapshot that
differs only a little (candidate for XOR/delta rather than full re-store).

Usage:
  # measure real snapshots (byte-exact SQLite files, oldest first):
  python3 measure_page_dedup.py snap0.db snap1.db snap2.db ...

  # synthetic demo showing both regimes (append-heavy vs update-heavy):
  python3 measure_page_dedup.py --demo
"""
import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fastcdc_cas import fastcdc, sha256


def sqlite_page_size(data):
    if data[:16] != b"SQLite format 3\x00":
        return None
    ps = struct.unpack(">H", data[16:18])[0]
    return 65536 if ps == 1 else ps


def pages(data, ps):
    return [data[i:i + ps] for i in range(0, len(data), ps)]


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024
    return "%.1f TB" % n


def byte_diff_ratio(a, b):
    """Fraction of differing bytes between two equal-length pages (0..1)."""
    if len(a) != len(b):
        return 1.0
    d = sum(1 for x, y in zip(a, b) if x != y)
    return d / len(a)


DELTA_SIMILAR_THRESHOLD = 0.25  # a "new" page within 25% of its old same-index page = delta candidate


def measure(snapshot_bytes_list, labels=None):
    labels = labels or ["snap%d" % i for i in range(len(snapshot_bytes_list))]
    ps = None
    for b in snapshot_bytes_list:
        p = sqlite_page_size(b)
        if p:
            ps = p
            break
    if ps is None:
        print("ERROR: none of the inputs look like SQLite files (bad header).")
        return
    # cumulative stores (simulating a remote store populated over time)
    page_store = set()          # page hashes seen so far
    fastcdc_store = set()       # fastcdc chunk hashes seen so far

    tot_input = tot_page_store = tot_fastcdc_store = tot_fullcopy = 0

    hdr = "%-10s %8s %8s %8s %10s %12s %12s %12s" % (
        "snapshot", "pages", "dedup", "new", "untouch%", "page-store", "fastcdc-store", "full-copy")
    print("page size: %d B" % ps)
    print(hdr)
    print("-" * len(hdr))

    prev_pages = None
    for idx, (data, label) in enumerate(zip(snapshot_bytes_list, labels)):
        pl = pages(data, ps)
        total = len(pl)
        dedup = new = 0
        delta_candidates = 0
        for j, pg in enumerate(pl):
            h = sha256(pg)
            if h in page_store:
                dedup += 1
            else:
                new += 1
                page_store.add(h)
                # delta opportunity: is there a same-index page last time that is close?
                if prev_pages is not None and j < len(prev_pages):
                    if byte_diff_ratio(pg, prev_pages[j]) <= DELTA_SIMILAR_THRESHOLD:
                        delta_candidates += 1
        # fastcdc accounting for the same snapshot
        fc_new_bytes = 0
        for off, length in fastcdc(data):
            h = sha256(data[off:off + length])
            if h not in fastcdc_store:
                fastcdc_store.add(h)
                fc_new_bytes += length

        page_bytes = new * ps
        full = len(data)
        untouch = (dedup / total * 100) if total else 0.0

        tot_input += full
        tot_page_store += page_bytes
        tot_fastcdc_store += fc_new_bytes
        tot_fullcopy += full

        note = ""
        if idx > 0:
            note = "  (%d/%d new pages are near-duplicates of last snapshot -> delta candidates)" % (
                delta_candidates, new) if new else ""
        print("%-10s %8d %8d %8d %9.1f%% %12s %12s %12s" % (
            label, total, dedup, new, untouch,
            human(page_bytes), human(fc_new_bytes), human(full)))
        if note:
            print(note)
        prev_pages = pl

    print("-" * len(hdr))
    print("TOTALS over %d snapshots:" % len(snapshot_bytes_list))
    print("  naive full-copy backup     :", human(tot_fullcopy))
    print("  FastCDC dedup store        :", human(tot_fastcdc_store),
          "(%.1f%% of full-copy)" % (tot_fastcdc_store / tot_fullcopy * 100 if tot_fullcopy else 0))
    print("  page-dedup store (ours)    :", human(tot_page_store),
          "(%.1f%% of full-copy)" % (tot_page_store / tot_fullcopy * 100 if tot_fullcopy else 0))
    if tot_page_store:
        print("  page-dedup vs FastCDC      : %.2fx %s" % (
            (tot_fastcdc_store / tot_page_store) if tot_fastcdc_store >= tot_page_store
            else (tot_page_store / tot_fastcdc_store),
            "smaller" if tot_fastcdc_store >= tot_page_store else "LARGER"))
    print()
    print("Headline = untouch%%: the fraction of pages a backup can skip re-uploading.")
    print("High untouch%% (append-heavy apps) -> paper works. Low -> dedup alone is weak,")
    print("and the delta-candidate note shows how much a dedup+delta hybrid could recover.")


# ---------------------------------------------------------------------------
# Synthetic demo: two honest regimes, no VACUUM (realistic backup cadence).
# ---------------------------------------------------------------------------
def _demo():
    import sqlite3
    import shutil
    d = Path(__file__).parent / "backup_demo"
    d.mkdir(exist_ok=True)

    def fresh(path, n):
        if path.exists():
            path.unlink()
        con = sqlite3.connect(path)
        con.execute("PRAGMA page_size=4096")
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("CREATE TABLE msgs(id INTEGER PRIMARY KEY, ts INTEGER, body TEXT)")
        con.executemany("INSERT INTO msgs VALUES(?,?,?)",
                        [(i, i * 1000, "message %d with padding text to fill the page nicely" % i)
                         for i in range(n)])
        con.commit(); con.close()

    def append_snapshot(src, dst, k):
        shutil.copy(src, dst)
        con = sqlite3.connect(dst)
        m = con.execute("SELECT MAX(id) FROM msgs").fetchone()[0]
        con.executemany("INSERT INTO msgs VALUES(?,?,?)",
                        [(m + 1 + i, (m + 1 + i) * 1000, "appended message %d text padding here" % (m + 1 + i))
                         for i in range(k)])
        con.commit(); con.close()

    def update_snapshot(src, dst, k):
        shutil.copy(src, dst)
        con = sqlite3.connect(dst)
        n = con.execute("SELECT COUNT(*) FROM msgs").fetchone()[0]
        # scatter k updates across the whole table (touches many pages).
        # Vary the text by dst name so each snapshot differs (non-idempotent,
        # like a real update-heavy workload writing fresh values each interval).
        tag = Path(dst).stem
        step = max(1, n // k)
        for i in range(0, n, step):
            con.execute("UPDATE msgs SET body=? WHERE id=?",
                        ("REWRITTEN at %s row %d with fresh distinct text" % (tag, i), i))
        con.commit(); con.close()

    for regime, mutate in (("APPEND-HEAVY (chat/anki-like)", append_snapshot),
                           ("UPDATE-HEAVY (key-value/config-like)", update_snapshot)):
        print("=" * 78)
        print("REGIME:", regime)
        print("=" * 78)
        base = d / "s0.db"
        fresh(base, 20000)
        snaps = [base.read_bytes()]
        labels = ["s0"]
        prev = base
        for i in range(1, 5):
            cur = d / ("s%d.db" % i)
            mutate(prev, cur, 400)   # 400 rows added/updated per backup interval
            snaps.append(cur.read_bytes())
            labels.append("s%d" % i)
            prev = cur
        measure(snaps, labels)
        print()


def main():
    ap = argparse.ArgumentParser(description="cross-snapshot SQLite page-dedup measurer")
    ap.add_argument("snapshots", nargs="*", help="SQLite files, oldest first")
    ap.add_argument("--demo", action="store_true", help="run the synthetic two-regime demo")
    args = ap.parse_args()
    if args.demo:
        _demo()
        return
    if len(args.snapshots) < 2:
        ap.error("give >= 2 snapshot files (oldest first), or use --demo")
    data = [Path(p).read_bytes() for p in args.snapshots]
    measure(data, [Path(p).name for p in args.snapshots])


if __name__ == "__main__":
    main()