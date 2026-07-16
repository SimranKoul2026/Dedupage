"""
page_dedup_vfs - a live page-dedup SQLite VFS shim (Phase 1, apsw prototype).

Wraps the default SQLite VFS. Every write to the MAIN DB file is recorded as a
dirty byte-range. On snapshot(), the dirty pages are read, SHA-256'd, and any
page whose content is new is written to a content-addressed store (same layout
as fastcdc_cas.py: store/objects/<hash>, zlib-compressed) plus a per-snapshot
manifest (page_no -> hash). Unchanged/duplicate pages cost nothing.

This does live, in-process, what measure_page_dedup.py does offline on file
copies. The driver cross-validates the two: the set of NEW page hashes per
snapshot must match exactly.

Phase 1 uses journal_mode=DELETE so main-DB writes are page-aligned and land in
the main file directly (simple, exact page_no = offset/page_size). Phase 2 adds
WAL-frame parsing.
"""
import json
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastcdc_cas import sha256  # noqa: E402

import apsw  # noqa: E402

SQLITE_OPEN_MAIN_DB = 0x00000100


class DedupController:
    """Holds the store, tracks stats, drives snapshot()."""

    def __init__(self, store_dir):
        self.store = Path(store_dir)
        self.objects = self.store / "objects"
        self.manifests = self.store / "manifests"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
        self.seen = set()          # content hashes already in the store
        self.mainfile = None       # the open main-DB VFSFile
        self.bytes_uploaded = 0    # cumulative, uncompressed page bytes of NEW pages
        self.snapshots = []        # per-snapshot stat records

    def register_main(self, f):
        self.mainfile = f

    def snapshot(self, label):
        f = self.mainfile
        if f is None:
            raise RuntimeError("no main DB file is open")
        ps = f.page_size()
        dirty = f.take_dirty_pages(ps)
        pairs = []          # (page_no, hash) for every dirty page (the manifest)
        new_hashes = set()
        up = 0
        for pno in sorted(dirty):
            page = f.read_page(pno, ps)
            h = sha256(page)
            pairs.append((pno, h))
            if h not in self.seen:
                self.seen.add(h)
                (self.objects / h).write_bytes(zlib.compress(page, 6))
                up += ps                 # uncompressed metric (matches offline tool)
                new_hashes.add(h)
        manifest = {"label": label, "page_size": ps, "pages": pairs}
        (self.manifests / (label + ".json")).write_text(json.dumps(manifest))
        self.bytes_uploaded += up
        rec = {
            "label": label,
            "dirty_pages": len(dirty),
            "dirty_page_nos": set(dirty),
            "new_pages": len(new_hashes),
            "bytes_uploaded": up,
            "new_hashes": new_hashes,
        }
        self.snapshots.append(rec)
        return rec


# module-level controller so VFSFile instances can register themselves
CONTROLLER = None


class DedupVFSFile(apsw.VFSFile):
    def __init__(self, basevfs, name, flags):
        super().__init__(basevfs, name, flags)
        self.is_main = bool(flags[0] & SQLITE_OPEN_MAIN_DB)
        self._writes = []          # list of (offset, length) since last snapshot
        if self.is_main and CONTROLLER is not None:
            CONTROLLER.register_main(self)

    def xWrite(self, data, offset):
        if self.is_main:
            self._writes.append((offset, len(data)))
        super().xWrite(data, offset)

    # --- helpers used by the controller at snapshot time ---
    def page_size(self):
        # SQLite header: bytes 16-17 big-endian; value 1 means 65536.
        hdr = self.xRead(2, 16)
        ps = struct.unpack(">H", hdr)[0]
        return 65536 if ps == 1 else ps

    def take_dirty_pages(self, ps):
        pages = set()
        for off, ln in self._writes:
            first = off // ps
            last = (off + ln - 1) // ps
            pages.update(range(first, last + 1))
        self._writes = []
        return pages

    def read_page(self, pno, ps):
        return self.xRead(ps, pno * ps)


class DedupVFS(apsw.VFS):
    def __init__(self, name="dedup", base=""):
        self.basevfs = base
        super().__init__(name, base)

    def xOpen(self, name, flags):
        return DedupVFSFile(self.basevfs, name, flags)


def setup(store_dir):
    """Register the dedup VFS and a fresh controller. Returns (controller, vfs).
    Keep the returned vfs referenced for the connection's lifetime."""
    global CONTROLLER
    CONTROLLER = DedupController(store_dir)
    vfs = DedupVFS()
    return CONTROLLER, vfs


def parse_wal_pages(walbytes, ps):
    """Return the set of 0-indexed page_no's that have frames in a WAL file.

    WAL format: 32-byte header, then frames of (24-byte frame header + ps-byte
    page). Frame-header bytes 0..3 = big-endian SQLite page number (1-indexed).
    This is the design's efficient path: derive the changed-page set from the
    WAL directly, no full-DB rescan. Returns 0-indexed page_no (SQLite page - 1)
    to match the main-DB offset//ps convention.
    """
    pages = set()
    if len(walbytes) < 32:
        return pages
    frame = 24 + ps
    off = 32
    while off + 24 <= len(walbytes):
        pno = struct.unpack(">I", walbytes[off:off + 4])[0]
        if pno == 0:
            break                      # partial/invalid frame -> stop
        pages.add(pno - 1)             # 1-indexed SQLite page -> 0-indexed page_no
        off += frame
    return pages


def restore(store_dir, manifest_labels, out_path):
    """Reconstruct the DB state after applying manifests in order (later page
    hashes override earlier ones). Byte-exact to the live DB at that snapshot."""
    store = Path(store_dir)
    pagemap = {}
    ps = None
    for label in manifest_labels:
        m = json.loads((store / "manifests" / (label + ".json")).read_text())
        ps = m["page_size"]
        for pno, h in m["pages"]:
            pagemap[pno] = h
    maxp = max(pagemap)
    out = bytearray((maxp + 1) * ps)
    for pno, h in pagemap.items():
        page = zlib.decompress((store / "objects" / h).read_bytes())
        out[pno * ps:(pno + 1) * ps] = page
    Path(out_path).write_bytes(out)
    return out_path