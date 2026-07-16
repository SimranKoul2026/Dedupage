#!/usr/bin/env python3
"""
fastcdc_cas - content-addressed deduplicating packer.

This does NOT magically shrink incompressible data. What it does:

  1. Split input into content-defined chunks (FastCDC).
  2. Name each unique chunk by its SHA-256 (content addressing).
  3. Store each unique chunk exactly once in a shared store.
  4. Emit a tiny MANIFEST: an ordered list of chunk hashes + file metadata.

The manifest is the "compressed" artifact. It can be a few MB even for a
huge input, BUT reconstruction requires a store that already holds the
referenced chunks. The bytes did not vanish; they are shared.

You get dramatic "21GB -> 5MB" numbers exactly when the referenced chunks
already exist in the store (a shared corpus, a prior backup, or heavy
internal/near-duplicate repetition). Pack genuinely-unique random data and
the store grows by ~21GB, honestly.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import zlib
from pathlib import Path

# ---------------------------------------------------------------------------
# FastCDC content-defined chunking
# ---------------------------------------------------------------------------
# A "gear" hash table: 256 deterministic 64-bit values. Deterministic so that
# packing the same bytes always yields the same chunk boundaries (required for
# dedup to work across runs and machines).
def _build_gear(seed=0x1FE35A7B):
    gear = []
    x = seed & 0xFFFFFFFFFFFFFFFF
    for _ in range(256):
        # xorshift64* - a small deterministic PRNG, no external deps
        x ^= (x >> 12) & 0xFFFFFFFFFFFFFFFF
        x ^= (x << 25) & 0xFFFFFFFFFFFFFFFF
        x ^= (x >> 27) & 0xFFFFFFFFFFFFFFFF
        gear.append((x * 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF)
    return gear

_GEAR = _build_gear()

MIN_CHUNK = 2 * 1024      # 2 KiB
AVG_CHUNK = 16 * 1024     # 16 KiB target average
MAX_CHUNK = 64 * 1024     # 64 KiB

# Two masks for FastCDC "normalized chunking": a stricter mask before the
# average size (harder to cut, pushes chunks toward the average) and a looser
# mask after (easier to cut, avoids oversized chunks).
_BITS = AVG_CHUNK.bit_length() - 1
_MASK_S = (1 << (_BITS + 1)) - 1   # more 1-bits -> boundary rarer
_MASK_L = (1 << (_BITS - 1)) - 1   # fewer 1-bits -> boundary common


def fastcdc(data):
    """Yield (offset, length) chunk boundaries for a bytes buffer."""
    n = len(data)
    start = 0
    while start < n:
        # last chunk
        if n - start <= MIN_CHUNK:
            yield start, n - start
            return
        fp = 0
        i = start + MIN_CHUNK
        limit_norm = min(start + AVG_CHUNK, n)
        limit_max = min(start + MAX_CHUNK, n)
        cut = 0
        # stricter mask region [MIN, AVG)
        while i < limit_norm:
            fp = ((fp << 1) + _GEAR[data[i]]) & 0xFFFFFFFFFFFFFFFF
            if not (fp & _MASK_S):
                cut = i
                break
            i += 1
        # looser mask region [AVG, MAX)
        if not cut:
            while i < limit_max:
                fp = ((fp << 1) + _GEAR[data[i]]) & 0xFFFFFFFFFFFFFFFF
                if not (fp & _MASK_L):
                    cut = i
                    break
                i += 1
        if not cut:
            cut = limit_max
        yield start, cut - start
        start = cut


# ---------------------------------------------------------------------------
# Store layout
# ---------------------------------------------------------------------------
# store/
#   objects/aa/bbbbbb...   one file per unique chunk, zlib-compressed
# A chunk hash is sha256 hex; it is sharded by its first byte to keep dirs sane.
class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)

    def _path(self, digest):
        return self.objects / digest[:2] / digest[2:]

    def has(self, digest):
        return self._path(digest).exists()

    def put(self, digest, raw):
        """Store a chunk if absent. Returns bytes newly written to disk."""
        p = self._path(digest)
        if p.exists():
            return 0
        p.parent.mkdir(parents=True, exist_ok=True)
        blob = zlib.compress(raw, 6)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, p)
        return len(blob)

    def get(self, digest):
        return zlib.decompress(self._path(digest).read_bytes())


def sha256(b):
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------------------
# Pack / unpack
# ---------------------------------------------------------------------------
def iter_files(target):
    target = Path(target)
    if target.is_file():
        yield target, target.name
        return
    for p in sorted(target.rglob("*")):
        if p.is_file():
            yield p, str(p.relative_to(target))


def pack(target, store_root, manifest_path, read_size=4 * 1024 * 1024):
    store = Store(store_root)
    files = []
    total_in = 0
    unique_bytes = 0        # bytes newly written to the store (post-compress)
    unique_chunks = 0
    dup_chunks = 0
    seen_this_pack = set()

    for path, rel in iter_files(target):
        size = path.stat().st_size
        total_in += size
        chunk_refs = []
        with path.open("rb") as fh:
            # Read in large windows; re-chunk within each window. (For a real
            # tool you would stream a rolling window across reads; a 4 MiB read
            # with 64 KiB max-chunk keeps boundary artifacts negligible.)
            buf = fh.read()
        for off, length in fastcdc(buf):
            chunk = buf[off:off + length]
            digest = sha256(chunk)
            chunk_refs.append([digest, length])
            if store.has(digest) or digest in seen_this_pack:
                dup_chunks += 1
            else:
                written = store.put(digest, chunk)
                unique_bytes += written
                unique_chunks += 1
                seen_this_pack.add(digest)
        files.append({"path": rel, "size": size, "chunks": chunk_refs})

    manifest = {
        "version": 1,
        "created": int(time.time()),
        "chunker": {"algo": "fastcdc", "min": MIN_CHUNK, "avg": AVG_CHUNK, "max": MAX_CHUNK},
        "files": files,
    }
    raw_manifest = json.dumps(manifest, separators=(",", ":")).encode()
    packed_manifest = zlib.compress(raw_manifest, 9)
    Path(manifest_path).write_bytes(packed_manifest)

    manifest_size = len(packed_manifest)
    report = {
        "input_bytes": total_in,
        "unique_chunks": unique_chunks,
        "dup_chunks": dup_chunks,
        "store_growth_bytes": unique_bytes,
        "manifest_bytes": manifest_size,
    }
    return report


def unpack(manifest_path, store_root, out_dir):
    store = Store(store_root)
    manifest = json.loads(zlib.decompress(Path(manifest_path).read_bytes()))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for f in manifest["files"]:
        dest = out / f["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for digest, length in f["chunks"]:
                chunk = store.get(digest)
                if len(chunk) != length:
                    raise ValueError("length mismatch for %s" % digest)
                fh.write(chunk)
    return len(manifest["files"])


# ---------------------------------------------------------------------------
def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return "%.2f %s" % (n, unit)
        n /= 1024
    return "%.2f PB" % n


def main(argv=None):
    ap = argparse.ArgumentParser(description="content-addressed deduplicating packer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pack", help="pack a file/dir into store + manifest")
    p.add_argument("target")
    p.add_argument("--store", required=True)
    p.add_argument("-o", "--manifest", required=True)

    u = sub.add_parser("unpack", help="reconstruct from manifest + store")
    u.add_argument("manifest")
    u.add_argument("--store", required=True)
    u.add_argument("-o", "--out", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "pack":
        r = pack(args.target, args.store, args.manifest)
        ratio = (r["manifest_bytes"] / r["input_bytes"]) if r["input_bytes"] else 0
        print("input               :", human(r["input_bytes"]))
        print("unique chunks        :", r["unique_chunks"])
        print("duplicate chunks     :", r["dup_chunks"])
        print("store grew by        :", human(r["store_growth_bytes"]), "(actual new bytes on disk)")
        print("manifest (shippable) :", human(r["manifest_bytes"]))
        print("manifest / input     : %.4f%%" % (ratio * 100))
        print()
        print("The manifest is what you 'send'. It reconstructs ONLY against a")
        print("store that holds the referenced chunks.")
    elif args.cmd == "unpack":
        n = unpack(args.manifest, args.store, args.out)
        print("reconstructed %d file(s) into %s" % (n, args.out))


if __name__ == "__main__":
    main()