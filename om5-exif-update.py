#!/usr/bin/env python3
"""
om5-exif-update.py

Relabel an OM System OM-5 Mark II raw file (ORF) as a plain OM-5 so that
imaging software predating the Mark II will accept it, or revert that
change with 1to2 mode.

The Mark II and the original OM-5 share the same sensor and ORF container.
Capture One and similar tools choose a camera profile from the recorded
camera identity, so changing that identity is usually enough.

The two identity fields are rewritten in place, byte for byte, without
moving anything else in the file. 2to1 (the default) applies:

    IFD0:Model            "OM-5MarkII"  ->  "OM-5"     (IFD entry count 17 -> 5)
    Olympus:CameraType2   "S0130"       ->  "S0101"    (same length)

1to2 applies the exact reverse. No offsets change and no image or preview
data is touched. The output is exactly the same size as the input, and a
2to1 followed by a 1to2 reproduces the original bytes.

Usage:
    ./om5-exif-update.py FILE_OR_DIR [FILE_OR_DIR ...]
    ./om5-exif-update.py --mode 1to2 FILE_OR_DIR ...
    ./om5-exif-update.py --in-place FILE_OR_DIR ...
    ./om5-exif-update.py --dry-run FILE_OR_DIR ...

Options:
    --mode {2to1,1to2}
                  2to1 relabel Mark II files as OM-5 (default); 1to2 revert
                  2to1-converted files back to Mark II
    -o DIR        write results into DIR (default: ./om5-converted,
                  or ./om5-restored in 1to2 mode)
    --in-place    edit the originals, keeping a <file>.orig backup
    -n, --dry-run report what would change, write nothing

Conventions:
    Only .orf files are collected. When a directory is given it is searched
    recursively; output paths mirror the input tree, relative to the common
    root of the supplied paths, beneath the output directory.

    --in-place edits each original and leaves a single <file>.orig backup
    (never overwritten once it exists). XMP sidecars stay next to the
    original and are not copied.

    Without --in-place the original is left untouched and written to the
    output directory. Any matching XMP sidecar (NAME.orf.xmp or NAME.xmp) is
    copied alongside the output file. Sidecars are not detected or copied in
    the other direction, and unrelated sidecars are ignored.

    1to2 only touches files that carry the signature a 2to1 conversion
    leaves behind: Model "OM-5" with entry count 5, followed by zero
    padding from the old 17-byte "OM-5MarkII" slot, and exactly one "S0101"
    in the MakerNote. Anything else that reads as OM-5/S0101 is skipped,
    which keeps genuine OM-5 files safe in the common case; a genuine OM-5
    whose Model string happens to be followed by six or more zero bytes
    cannot be told apart from a converted file without samples of both
    bodies and would be reverted too.

    In 1to2 --in-place mode, when a <file>.orig backup from the original
    2to1 run exists it is compared against the expected result: on a
    byte-identical match the written file equals that backup, and a
    mismatching backup is reported and never copied over the file.
"""

import argparse
import os
import shutil
import struct
import sys

MODES = {
    "2to1": {"src_model": "OM-5MarkII", "dst_model": "OM-5",
             "src_camtype": "S0130", "dst_camtype": "S0101"},
    "1to2": {"src_model": "OM-5", "dst_model": "OM-5MarkII",
             "src_camtype": "S0101", "dst_camtype": "S0130"},
}

FORWARD_MODEL_SLOT = 17  # bytes Mark II files allocate to IFD0:Model

MODEL_TAG = 0x0110
EXIF_IFD_TAG = 0x8769
MAKERNOTE_TAG = 0x927C

ORF_MAGIC = 0x4F52  # "RO" little-endian, ORF files use this instead of 42


def parse_ifd(data, base, endian):
    """Return (tag, typ, count, value_field_pos, entry_pos) for each entry."""
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
    """Locate the Model IFD0 entry and the MakerNote region.

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
    info = {"endian": endian}

    for tag, typ, count, vpos, epos in parse_ifd(data, ifd0, endian):
        if tag == MODEL_TAG:
            off, size = tag_value(data, typ, count, vpos, endian)
            info["model"] = (off, size, epos, count)
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


def patch(data, info, mode):
    """Apply the relabel to a mutable bytearray. Returns a list of changes."""
    spec = MODES[mode]
    changes = []

    moff, msize, mepos, mcount = info["model"]
    src = spec["src_model"].encode()
    dst = spec["dst_model"].encode()

    if data[moff:moff + len(src)] != src:
        got = data[moff:moff + msize].split(b"\x00", 1)[0].decode("ascii", "replace")
        hint = " (already converted?)" if mode == "2to1" else ""
        raise ValueError(
            "Model is %r, not %r%s" % (got, spec["src_model"], hint)
        )

    if mode == "2to1":
        # Model: write DST + NUL and update the IFD entry count; clear the old slot.
        data[moff:moff + msize] = b"\x00" * msize
        new = dst + b"\x00"
        data[moff:moff + len(new)] = new
        struct.pack_into(info["endian"] + "I", data, mepos + 4, len(new))
    else:
        # The forward patch left "OM-5\0" followed by zero padding from the
        # old "OM-5MarkII" slot. Rewrite only bytes inside that padded slot;
        # require enough padding for "MarkII\0" so that a genuine OM-5 whose
        # Model value is immediately followed by live data is left alone.
        if mcount != len(src) + 1:
            raise ValueError(
                "Model entry count is %d, not %d (not a converted file?)"
                % (mcount, len(src) + 1)
            )
        cap = FORWARD_MODEL_SLOT - (len(src) + 1)
        run = 0
        while (run < cap and moff + len(src) + 1 + run < len(data)
               and data[moff + len(src) + 1 + run] == 0):
            run += 1
        if run < len(dst) - len(src):
            raise ValueError(
                "Model is %r but is not followed by zero padding "
                "(genuine OM-5, not a converted file?)" % spec["src_model"]
            )
        slot = len(src) + 1 + run
        data[moff:moff + slot] = dst + b"\x00" * (slot - len(dst))
        struct.pack_into(info["endian"] + "I", data, mepos + 4, slot)
    changes.append("Model  %r -> %r" % (spec["src_model"], spec["dst_model"]))

    # CameraType2 inside the MakerNote: unique there, same length.
    moff, msize = info["makernote"]
    region = bytes(data[moff:moff + msize])
    hits = []
    start = 0
    needle = spec["src_camtype"].encode()
    while True:
        i = region.find(needle, start)
        if i < 0:
            break
        hits.append(moff + i)
        start = i + 1
    if len(hits) != 1:
        raise ValueError(
            "expected exactly one %r in MakerNote, found %d"
            % (spec["src_camtype"], len(hits))
        )
    data[hits[0]:hits[0] + len(needle)] = spec["dst_camtype"].encode()
    changes.append("CameraType2 %r -> %r" % (spec["src_camtype"], spec["dst_camtype"]))

    return changes


def sidecars(path):
    """Existing XMP sidecars for a raw file: NAME.orf.xmp and NAME.xmp."""
    candidates = [path + ".xmp", os.path.splitext(path)[0] + ".xmp"]
    return [c for c in candidates if os.path.isfile(c)]


def dest_sidecar(path, dest, sc):
    """Map a source sidecar path to its counterpart next to dest."""
    if sc == path + ".xmp":
        return dest + ".xmp"
    return os.path.splitext(dest)[0] + ".xmp"


def process(path, dest, in_place, dry_run, mode):
    """Relabel one file. Returns True if a change was (or would be) made.

    Raises ValueError for files that do not match the mode's expectations
    (not a Mark II ORF for 2to1, not a converted ORF for 1to2) and OSError
    for read or write failures.
    """
    with open(path, "rb") as fh:
        original = fh.read()

    info = find_identity(original)
    buf = bytearray(original)
    changes = patch(buf, info, mode)

    if bytes(buf) == original:
        print("unchanged %s" % path)
        return False

    print("rewrite %s" % path)
    for c in changes:
        print("        %s" % c)

    if dry_run:
        return True

    if in_place:
        backup = path + ".orig"
        if mode == "1to2" and os.path.exists(backup):
            with open(backup, "rb") as fh:
                backup_bytes = fh.read()
            if backup_bytes == bytes(buf):
                print("        matches existing %s" % backup)
            else:
                print("note    existing %s does not match this file's "
                      "original Mark II state; re-patching anyway" % backup)
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
        with open(path, "wb") as fh:
            fh.write(buf)
    else:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copy2(path, dest)
        with open(dest, "wb") as fh:
            fh.write(buf)
        for sc in sidecars(path):
            shutil.copy2(sc, dest_sidecar(path, dest, sc))
    return True


def collect(paths):
    """Expand directories into their .orf files; pass other paths through."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for name in files:
                    if name.lower().endswith(".orf"):
                        out.append(os.path.join(root, name))
        else:
            out.append(p)
    return out


def output_base(paths):
    """Common root of the supplied paths, for mirroring the input tree."""
    absolute = [os.path.abspath(p) for p in paths]
    try:
        base = os.path.commonpath(absolute)
    except ValueError:
        base = os.path.dirname(absolute[0])
    if os.path.isfile(base):
        base = os.path.dirname(base)
    return base


def main():
    ap = argparse.ArgumentParser(
        description="Relabel OM-5 Mark II ORF files as OM-5, "
                    "or revert converted files back to Mark II.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paths", nargs="+", help="files or directories")
    ap.add_argument("--mode", choices=("2to1", "1to2"), default="2to1",
                    help="2to1 relabel Mark II files as OM-5 (default); "
                         "1to2 revert 2to1-converted files back to Mark II")
    ap.add_argument("-o", "--out", default=None,
                    help="output directory (default: om5-converted for 2to1, "
                         "om5-restored for 1to2)")
    ap.add_argument("--in-place", action="store_true",
                    help="edit originals, keeping .orig backups")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="report changes without writing")
    args = ap.parse_args()

    out_dir = args.out or ("om5-converted" if args.mode == "2to1" else "om5-restored")

    files = collect(args.paths)
    if not files:
        print("no files found", file=sys.stderr)
        return 1

    base = output_base(args.paths)

    written = unchanged = skipped = failed = 0
    for path in files:
        try:
            with open(path, "rb") as fh:
                head = fh.read(4)
        except OSError as exc:
            print("fail    %s (%s)" % (path, exc))
            failed += 1
            continue

        if len(head) < 4 or head[:2] not in (b"II", b"MM"):
            print("skip    %s (not TIFF/ORF)" % path)
            skipped += 1
            continue

        dest = path if args.in_place else os.path.join(
            out_dir, os.path.relpath(os.path.abspath(path), base)
        )
        try:
            if process(path, dest, args.in_place, args.dry_run, args.mode):
                written += 1
            else:
                unchanged += 1
        except ValueError as exc:
            print("skip    %s (%s)" % (path, exc))
            skipped += 1
        except OSError as exc:
            print("fail    %s (%s)" % (path, exc))
            failed += 1

    verb = "would rewrite" if args.dry_run else ("rewrote" if args.in_place else "wrote")
    print("\n%s %d, unchanged %d, skipped %d, failed %d"
          % (verb, written, unchanged, skipped, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
