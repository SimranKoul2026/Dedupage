"""
Phase 4 benchmark: bytes-uploaded per method, on the REAL captured workloads.

Methods (per interval i, given the remote already holds snapshots 0..i-1):
  full-copy     : re-upload the whole DB               = size(snap[i])
  rsync         : rsync delta batch snap[i-1] -> snap[i]  (real rsync run)
  fastcdc       : FastCDC chunks of snap[i] new to cumulative store
  litestream    : raw changed pages (positional diff vs snap[i-1]) x page_size
                  = what Litestream ships (WAL frames, NO cross-snapshot dedup)
  page-dedup    : pages of snap[i] whose content hash is new to cumulative store
                  = OUR method (changed AND content-new)

Emits a per-workload table + a CSV. page-dedup should be <= litestream <= fastcdc
<= full-copy on the realistic-change intervals.
"""
import csv
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from fastcdc_cas import fastcdc, sha256  # noqa: E402
import measure_page_dedup as off  # noqa: E402


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f TB" % n


def litestream_bytes(cur, prev, ps):
    """Changed pages (positional) = WAL frames Litestream ships, zlib-compressed
    (Litestream supports payload compression), NO cross-snapshot dedup."""
    import zlib
    pc = off.pages(cur, ps)
    pp = off.pages(prev, ps) if prev is not None else []
    b = 0
    for i, pg in enumerate(pc):
        if i >= len(pp) or pg != pp[i]:
            b += len(zlib.compress(pg, 6))
    return b


def rsync_bytes(prev_path, cur_path):
    """Real rsync upload cost: remote already has prev; delta-sync it to cur,
    report 'Total bytes sent' (forces the delta algorithm with --no-whole-file)."""
    import shutil
    import re
    import os
    rsync = "/opt/homebrew/bin/rsync" if os.path.exists("/opt/homebrew/bin/rsync") else "rsync"
    with tempfile.TemporaryDirectory() as d:
        remote = Path(d) / "remote.db"
        shutil.copy(prev_path, remote)          # remote currently holds prev
        r = subprocess.run(   # -z: compressed transfer, fair vs the zlib methods
            [rsync, "--no-whole-file", "-z", "--stats", "-a", cur_path, str(remote)],
            capture_output=True, text=True)
        m = re.search(r"[Tt]otal bytes sent:\s*([\d,]+)", r.stdout)
        return int(m.group(1).replace(",", "")) if m else -1


def bench_workload(name, files):
    import zlib
    rows = []
    page_store = set()
    fcdc_store = set()
    prev = None
    prev_path = None
    prev_pages = None
    for path in files:
        data = Path(path).read_bytes()
        ps = off.sqlite_page_size(data)
        full = len(data)
        cur_pages = off.pages(data, ps)
        # page-dedup (ours) and page-dedup+XOR-delta (Phase 5)
        pd = 0
        pdd = 0
        for i, pg in enumerate(cur_pages):
            h = sha256(pg)
            if h not in page_store:
                page_store.add(h)
                pd += len(zlib.compress(pg, 6))   # content-addressed objects are compressed
                # Phase 5: if a same-position prior page exists, store the
                # zlib(XOR-delta) instead of the full page when it's smaller.
                if prev_pages is not None and i < len(prev_pages) and len(prev_pages[i]) == ps:
                    xor = bytes(a ^ b for a, b in zip(pg, prev_pages[i]))
                    pdd += min(len(zlib.compress(pg, 6)), len(zlib.compress(xor, 6)))
                else:
                    pdd += len(zlib.compress(pg, 6))
        # fastcdc (chunks compressed, as restic/borg store them)
        fc = 0
        for o, l in fastcdc(data):
            chunk = data[o:o + l]
            h = sha256(chunk)
            if h not in fcdc_store:
                fcdc_store.add(h)
                fc += len(zlib.compress(chunk, 6))
        # litestream-model
        ls = full if prev is None else litestream_bytes(data, prev, ps)
        # rsync (real); first interval has no prev -> full copy
        rs = full if prev_path is None else rsync_bytes(prev_path, path)
        rows.append({
            "workload": name, "snapshot": Path(path).stem,
            "full_copy": full, "rsync": rs, "fastcdc": fc,
            "litestream": ls, "page_dedup": pd, "page_dedup_delta": pdd,
        })
        prev = data
        prev_path = path
        prev_pages = cur_pages
    return rows


def gen_recurrence(outdir):
    """Synthetic workload where page CONTENT RECURS: a block of rows toggles
    between two states A/B across snapshots (A,B,A,B,A). Demonstrates the one
    regime where page-dedup beats Litestream: on the return to a prior state,
    those pages' content is already in the store (0 bytes) whereas Litestream
    re-ships every changed page every time. Clearly SYNTHETIC."""
    import sqlite3
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    dbp = outdir / "rec.db"
    if dbp.exists():
        dbp.unlink()
    con = sqlite3.connect(dbp)
    con.execute("PRAGMA page_size=4096")
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t VALUES(?,?)",
                    [(i, "base row %d padding padding padding" % i) for i in range(8000)])
    con.commit()
    files = []
    import shutil
    states = ["A", "B", "A", "B", "A"]
    for k, s in enumerate(states):
        # toggle a fixed block of rows between two contents
        con.executemany("UPDATE t SET v=? WHERE id=?",
                        [("STATE_%s row %d xxxxxxxxxxxxxxxxxxxx" % (s, i), i) for i in range(0, 2000)])
        con.commit()
        fp = outdir / ("r%d.db" % k)
        shutil.copy(dbp, fp)
        files.append(str(fp))
    con.close()
    return files


def main():
    workloads = {
        "AnkiDroid (phone, mixed)": [
            ROOT / "anki_snapshots/snap0.db", ROOT / "anki_snapshots/snap1.db",
            ROOT / "anki_snapshots/snap2.db", ROOT / "anki_snapshots/snap3.db"],
        "AntennaPod (phone)": [
            ROOT / "antennapod_snapshots/ap_snap0.db",
            ROOT / "antennapod_snapshots/ap_snap1.db"],
        "AntennaPod (tablet, Dimensity)": [
            ROOT / "tablet_snapshots/tab_snap0.db",
            ROOT / "tablet_snapshots/tab_snap1.db"],
        "SYNTHETIC content-recurrence (A/B toggle)": gen_recurrence(HERE / "rec_work"),
    }
    allrows = []
    for name, files in workloads.items():
        files = [str(f) for f in files if Path(f).exists()]
        if len(files) < 2:
            continue
        rows = bench_workload(name, files)
        allrows += rows
        print("=" * 78)
        print(name)
        print("-" * 78)
        print("(all payloads zlib-compressed / rsync -z, apples-to-apples)")
        print("%-9s %10s %10s %10s %10s %10s %10s" %
              ("snap", "full-copy", "rsync", "fastcdc", "litestrm", "pg-dedup", "pg-dedup+Δ"))
        keys = ("full_copy", "rsync", "fastcdc", "litestream", "page_dedup", "page_dedup_delta")
        tot = {k: 0 for k in keys}
        for r in rows:
            print("%-9s %10s %10s %10s %10s %10s %10s" % (
                r["snapshot"], human(r["full_copy"]), human(r["rsync"]),
                human(r["fastcdc"]), human(r["litestream"]), human(r["page_dedup"]),
                human(r["page_dedup_delta"])))
            for k in tot:
                tot[k] += r[k] if r[k] >= 0 else 0
        print("-" * 78)
        print("%-9s %10s %10s %10s %10s %10s %10s" % (
            "TOTAL", human(tot["full_copy"]), human(tot["rsync"]),
            human(tot["fastcdc"]), human(tot["litestream"]), human(tot["page_dedup"]),
            human(tot["page_dedup_delta"])))
        inc = {k: sum(r[k] for r in rows[1:] if r[k] >= 0) for k in keys}
        if inc["page_dedup"]:
            base = inc["page_dedup_delta"] or 1
            print("incremental: page-dedup+Δ=%s | page-dedup=%s | rsync=%s | litestream=%s | fastcdc=%s | full-copy=%s" % (
                human(inc["page_dedup_delta"]), human(inc["page_dedup"]), human(inc["rsync"]),
                human(inc["litestream"]), human(inc["fastcdc"]), human(inc["full_copy"])))
            print("           page-dedup+Δ vs rsync=%.2fx  vs litestream=%.1fx  vs fastcdc=%.1fx" % (
                inc["rsync"] / base, inc["litestream"] / base, inc["fastcdc"] / base))
        print()

    csv_path = ROOT / "benchmark_results.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(allrows[0].keys()))
        w.writeheader()
        w.writerows(allrows)
    print("wrote", csv_path)


if __name__ == "__main__":
    main()