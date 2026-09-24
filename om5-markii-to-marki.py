#!/usr/bin/env python3
"""
om5-markii-to-marki.py

Relabel an OM System OM-5 Mark II raw file (ORF) as a plain OM-5 so that
imaging software predating the Mark II will accept it.

The Mark II and the original OM-5 share the same sensor and ORF container.
Capture One and similar tools choose a camera profile from the recorded
camera identity, so changing that identity is usually enough.

The two identity fields are rewritten in place, byte for byte, without
moving anything else in the file:

    IFD0:Model            "OM-5MarkII"  ->  "OM-5"     (IFD entry count 17 -> 5)
    Olympus:CameraType2   "S0130"       ->  "S0101"    (same length)

No offsets change and no image or preview data is touched. The output is
exactly the same size as the input.

Usage:
    ./om5-markii-to-marki.py FILE_OR_DIR [FILE_OR_DIR ...]
    ./om5-markii-to-marki.py --in-place FILE_OR_DIR ...
    ./om5-markii-to-marki.py --dry-run FILE_OR_DIR ...

Options:
    -o DIR        write results into DIR (default: ./om5-converted)
    --in-place    edit the originals, keeping a <file>.orig backup
    -n, --dry-run report what would change, write nothing
"""

import argparse
import os
import shutil
import struct
import sys

SRC_MODEL = "OM-5MarkII"
SRC_CAMTYPE = "S0130"
DST_MODEL = "OM-5"
DST_CAMTYPE = "S0101"
DST_MAKE = "OM Digital Solutions"

MODEL_TAG = 0x0110
MAKE_TAG = 0x010F
EXIF_IFD_TAG = 0x8769
MAKERNOTE_TAG = 0x927C

ASCII = 2
ORF_MAGIC = 0x4F52  # "RO" little-endian, ORF files use this instead of 42


def parse_ifd(data, base, endian):
    """Yield (tag, typ, count, value_field_pos, entry_pos) for each entry."""
    if base + 2 > len(data):
        return []
    (n,) = struct.unpack_from(endian + "H", data, base)
    entries = []
    for i in range(n):
        e = base + 2 + 12 * i
        if e + 12 > len(data):
            break
        tag, typ, count = struct.unpack_from(endian + "HHI", data, e)
        entries.append((tag, typ, count, e + 8, e))
    return entries


def tag_value(data, typ, count, value_pos, endian):
    """Return (offset, size) of a tag's value, following the offset if needed."""
    size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}.get(typ, 1) * count
    if size <= 4:
        return value_pos, size  # stored inline in the entry
    (off,) = struct.unpack_from(endian + "I", data, value_pos)
    return off, size


def find_identity(data):
    """Locate the Model/ Make IFD0 entries and the MakerNote region.

    Returns a dict with the byte offsets needed to patch, or raises ValueError.
    """
    if data[:2] == b"II":
        endian = "<"
    elif data[:2] == b"MM":
        endian = ">"
    else:
        raise ValueError("not a TIFF/ORF file")

    (magic,) = struct.unpack_from(endian + "H", data, 2)
    if magic not in (42, ORF_MAGIC):
        raise ValueError("unexpected TIFF magic 0x%04x" % magic)

    (ifd0,) = struct.unpack_from(endian + "I", data, 4)
    ifd0_entries = parse_ifd(data, ifd0, endian)

    info = {"endian": endian}

    for tag, typ, count, vpos, epos in ifd0_entries:
        off, size = tag_value(data, typ, count, vpos, endian)
        if tag == MODEL_TAG:
            info["model"] = (off, size, count, epos)
        elif tag == MAKE_TAG:
            info["make"] = (off, size, count, epos)
        elif tag == EXIF_IFD_TAG:
            (sub,) = struct.unpack_from(endian + "I", data, vpos)
            for t, ty, c, vp, ep in parse_ifd(data, sub, endian):
                if t == MAKERNOTE_TAG:
                    moff, msize = tag_value(data, ty, c, vp, endian)
                    info["makernote"] = (moff, msize)

    if "model" not in info:
        raise ValueError("no IFD0 Model tag found")
    if "makernote" not in info:
        raise ValueError("no MakerNote found")
    return info


def patch(data, info):
    """Apply the relabel to a mutable bytearray. Returns a list of changes."""
    changes = []

    moff, msize, mcount, mepos = info["model"]
    current = data[moff:moff + len(SRC_MODEL)]
    if current != SRC_MODEL.encode():
        got = data[moff:moff + msize].split(b"\x00", 1)[0].decode("ascii", "replace")
        raise ValueError(
            "Model is %r, not %r (already converted?)" % (got, SRC_MODEL)
        )

    if "make" in info:
        aoff, asize, acount, aepos = info["make"]
        make = data[aoff:aoff + asize].rstrip(b"\x00 ").decode("ascii", "replace")
        if make and make != DST_MAKE:
            changes.append("Make   %r -> %r" % (make, DST_MAKE))

    # Model: write DST + NUL and update the IFD entry count; clear the old slot.
    data[moff:moff + msize] = b"\x00" * msize
    new = DST_MODEL.encode() + b"\x00"
    data[moff:moff + len(new)] = new
    struct.pack_into(info["endian"] + "I", data, mepos + 4, len(new))
    changes.append("Model  %r -> %r" % (SRC_MODEL, DST_MODEL))

    # CameraType2 inside the MakerNote: unique there, same length.
    moff, msize = info["makernote"]
    region = bytes(data[moff:moff + msize])
    hits = []
    start = 0
    while True:
        i = region.find(SRC_CAMTYPE.encode(), start)
        if i < 0:
            break
        hits.append(moff + i)
        start = i + 1
    if len(hits) != 1:
        raise ValueError(
            "expected exactly one %r in MakerNote, found %d" % (SRC_CAMTYPE, len(hits))
        )
    data[hits[0]:hits[0] + len(SRC_CAMTYPE)] = DST_CAMTYPE.encode()
    changes.append("CameraType2 %r -> %r" % (SRC_CAMTYPE, DST_CAMTYPE))

    return changes


def process(path, dest, in_place, dry_run):
    with open(path, "rb") as fh:
        original = fh.read()

    info = find_identity(original)
    buf = bytearray(original)
    changes = patch(buf, info)

    if bytes(buf) == original:
        print("unchanged %s" % path)
        return 0

    print("rewrite %s" % path)
    for c in changes:
        print("        %s" % c)

    if dry_run:
        return 1

    if in_place:
        backup = path + ".orig"
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
        with open(path, "wb") as fh:
            fh.write(buf)
    else:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copy2(path, dest)
        with open(dest, "wb") as fh:
            fh.write(buf)
    return 1


def collect(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for name in files:
                    if name.lower().endswith((".orf", ".jpg", ".jpeg")):
                        out.append(os.path.join(root, name))
        else:
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Relabel OM-5 Mark II ORF files as OM-5.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paths", nargs="+", help="files or directories")
    ap.add_argument("-o", "--out", default="om5-converted",
                    help="output directory (default: om5-converted)")
    ap.add_argument("--in-place", action="store_true",
                    help="edit originals, keeping .orig backups")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="report changes without writing")
    args = ap.parse_args()

    files = collect(args.paths)
    if not files:
        print("no files found", file=sys.stderr)
        return 1

    done = skipped = failed = 0
    for path in files:
        try:
            with open(path, "rb") as fh:
                head = fh.read(4)
            if len(head) < 4 or head[:2] not in (b"II", b"MM"):
                print("skip    %s (not TIFF/ORF)" % path)
                skipped += 1
                continue
        except OSError as exc:
            print("skip    %s (%s)" % (path, exc))
            skipped += 1
            continue

        dest = path if args.in_place else os.path.join(
            args.out, os.path.relpath(path)
        )
        try:
            done += process(path, dest, args.in_place, args.dry_run)
        except ValueError as exc:
            print("skip    %s (%s)" % (path, exc))
            skipped += 1

    verb = "would rewrite" if args.dry_run else ("rewrote" if args.in_place else "wrote")
    print("\n%s %d file(s), %d skipped, %d failed" % (verb, done, skipped, failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
