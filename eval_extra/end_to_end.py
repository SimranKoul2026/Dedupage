"""
End-to-end backup + restore over a real (loopback TCP) network with enforced
bandwidth, addressing "no end-to-end remote backup transport evaluation."

We move the actual backup bundle over a real socket to a store server, with a
token-bucket throttle simulating cellular-class links, then download it back and
reconstruct the database, verifying byte-exact. We compare full-copy against
page-dedup+XOR-delta on a real incremental change.

Bandwidths: 1 Mbps (congested cellular) and 10 Mbps (typical 4G). Numbers are
measured wall-clock over the throttled socket, not modeled.
"""
import socket
import struct
import threading
import time
import sqlite3
import shutil
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from fastcdc_cas import sha256
import measure_page_dedup as off

PAGE = 4096


def get_snapshots():
    """Use the real tablet AntennaPod snapshots if present, else synthesize a pair."""
    a = ROOT / "tablet_snapshots/tab_snap0.db"
    b = ROOT / "tablet_snapshots/tab_snap1.db"
    if a.exists() and b.exists():
        return a.read_bytes(), b.read_bytes(), "AntennaPod tablet (real)"
    d = HERE / "e2e_synth"; d.mkdir(exist_ok=True)
    db = d / "app.db"
    con = sqlite3.connect(db); con.execute("PRAGMA page_size=%d" % PAGE)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t VALUES(?,?)", [(i, "row %d padding" % i) for i in range(8000)])
    con.commit(); s0 = db.read_bytes()
    for i in range(0, 20 * 61, 61):
        con.execute("UPDATE t SET v=? WHERE id=?", ("edited %d" % i, i))
    con.commit(); con.close(); s1 = db.read_bytes()
    return s0, s1, "synthetic pair"


def build_bundles(s0, s1):
    """full-copy bundle = compressed whole new DB.
       ours bundle = compressed XOR-delta (or full) for each positionally-changed page."""
    ps = off.sqlite_page_size(s1)
    p0 = off.pages(s0, ps); p1 = off.pages(s1, ps)
    full = zlib.compress(s1, 6)
    entries = []
    for i, pg in enumerate(p1):
        if i < len(p0) and pg == p0[i]:
            continue                                   # unchanged; base already on remote
        if i < len(p0) and len(p0[i]) == ps:
            xor = bytes(a ^ b for a, b in zip(pg, p0[i]))
            cz = zlib.compress(pg, 6); cx = zlib.compress(xor, 6)
            entries.append((i, b"x", cx) if len(cx) < len(cz) else (i, b"f", cz))
        else:
            entries.append((i, b"f", zlib.compress(pg, 6)))
    # serialize ours bundle
    out = bytearray(struct.pack(">I", len(entries)))
    for pno, kind, blob in entries:
        out += struct.pack(">IcI", pno, kind, len(blob)) + blob
    return full, bytes(out), ps


def restore_ours(s0, bundle, ps):
    p0 = off.pages(s0, ps)
    pages = list(p0)
    off_ = 0
    (n,) = struct.unpack(">I", bundle[off_:off_+4]); off_ += 4
    for _ in range(n):
        pno, kind, blen = struct.unpack(">IcI", bundle[off_:off_+9]); off_ += 9
        blob = bundle[off_:off_+blen]; off_ += blen
        raw = zlib.decompress(blob)
        page = raw if kind == b"f" else bytes(a ^ b for a, b in zip(raw, p0[pno]))
        if pno < len(pages):
            pages[pno] = page
        else:
            pages.append(page)
    return b"".join(pages)


# --- tiny throttled TCP store server ---
class Store:
    def __init__(self):
        self.data = {}


def serve(store, ready):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(8)
    ready.append(srv.getsockname()[1]); ready.append(srv)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(store, c), daemon=True).start()


def recvall(c, n):
    b = b""
    while len(b) < n:
        d = c.recv(min(65536, n - len(b)))
        if not d:
            break
        b += d
    return b


def handle(store, c):
    try:
        op = recvall(c, 1)
        (nl,) = struct.unpack(">I", recvall(c, 4)); name = recvall(c, nl).decode()
        if op == b"P":
            (dl,) = struct.unpack(">Q", recvall(c, 8)); store.data[name] = recvall(c, dl)
            c.sendall(b"\x01")
        elif op == b"G":
            d = store.data.get(name, b""); c.sendall(struct.pack(">Q", len(d))); c.sendall(d)
    finally:
        c.close()


def throttled_send(sock, data, bw_bytes):
    chunk = 8192; sent = 0; t0 = time.time()
    while sent < len(data):
        sock.sendall(data[sent:sent+chunk]); sent += chunk
        due = sent / bw_bytes
        el = time.time() - t0
        if due > el:
            time.sleep(due - el)


def put(port, name, data, bw):
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(b"P" + struct.pack(">I", len(name)) + name.encode() + struct.pack(">Q", len(data)))
    t0 = time.time(); throttled_send(s, data, bw); recvall(s, 1); dt = time.time() - t0
    s.close(); return dt


def get(port, name, bw):
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(b"G" + struct.pack(">I", len(name)) + name.encode())
    (dl,) = struct.unpack(">Q", recvall(s, 8))
    # throttled receive
    chunk = 8192; buf = b""; t0 = time.time()
    while len(buf) < dl:
        d = s.recv(min(chunk, dl - len(buf)))
        if not d:
            break
        buf += d
        due = len(buf) / bw; el = time.time() - t0
        if due > el:
            time.sleep(due - el)
    dt = time.time() - t0; s.close(); return buf, dt


def human(n):
    for u in ("B", "KB", "MB"):
        if abs(n) < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f GB" % n


def main():
    s0, s1, label = get_snapshots()
    full, ours, ps = build_bundles(s0, s1)
    print("Workload: %s. DB=%s, one realistic incremental change." % (label, human(len(s1))))
    print("Backup bundle sizes:  full-copy=%s   page-dedup+delta=%s\n" % (human(len(full)), human(len(ours))))

    ready = []
    threading.Thread(target=serve, args=(Store(), ready), daemon=True).start()
    while not ready:
        time.sleep(0.05)
    port = ready[0]

    for mbps in (1, 10):
        bw = mbps * 1_000_000 / 8
        print("=== link: %d Mbps (%s/s) ===" % (mbps, human(bw)))
        tf = put(port, "full", full, bw)
        to = put(port, "ours", ours, bw)
        print("  backup upload time:  full-copy=%.2fs   page-dedup+delta=%.2fs   (%.1fx faster)"
              % (tf, to, tf / to if to else 0))
        back, tdl = get(port, "ours", bw)
        recon = restore_ours(s0, back, ps)
        ok = recon == s1
        print("  restore: downloaded %s in %.2fs, reconstruction byte-exact=%s" % (human(len(back)), tdl, ok))
        print()


if __name__ == "__main__":
    main()