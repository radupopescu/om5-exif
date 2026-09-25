# om5-exif-update

Relabels an OM System OM-5 Mark II raw file (`.ORF`) as an original OM-5,
or reverts that relabelling back to Mark II.

The two bodies share the same sensor and ORF container.

## What it changes

Two fields are patched in place, byte for byte:

| Location             | 2to1 (default)        | 1to2 (reverse)        |
| -------------------- | -------------------- | -------------------- |
| `IFD0:Model`         | `OM-5MarkII` -> `OM-5` | `OM-5` -> `OM-5MarkII` |
| `Olympus:CameraType2`| `S0130` -> `S0101`   | `S0101` -> `S0130`   |

In 2to1 the `IFD0:Model` entry count shrinks from 17 to 5; 1to2 writes the
string back into the slot the conversion left behind and sets the count to
the slot size. `S0101` is ExifTool's canonical code for the OM-5.

The patch does not move any offset and does not touch image, preview or
thumbnail data. Output size equals input size; only the changed bytes
differ, and a 2to1 followed by a 1to2 reproduces the original bytes
exactly. Independent verification on a real file: 15 bytes differ, the
embedded 3200x2400 preview and the raw sensor strip are byte-identical,
and `exiftool -validate` output is unchanged.

## Usage

```
./om5-exif-update.py RAW.ORF                  # 2to1 (default) -> ./om5-converted/
./om5-exif-update.py --mode 1to2 RAW.ORF      # 1to2 -> ./om5-restored/
./om5-exif-update.py --in-place RAW.ORF       # edit original, keep RAW.ORF.orig backup
./om5-exif-update.py -n RAW.ORF               # dry run, report changes only
./om5-exif-update.py -o out DIR_OR_FILE       # choose output directory
```

Directories are searched recursively for `.orf`. Output paths mirror the input
tree beneath the output directory, relative to the common root of the supplied
paths. `--in-place` keeps one `<file>.orig` backup per file (never
overwritten) and leaves sidecars untouched; otherwise the original is left
alone and matching `NAME.orf.xmp` / `NAME.xmp` sidecars are copied next to the
output file. Files that do not match the selected mode are skipped, which
makes re-runs safe; read or write failures are counted and cause a non-zero
exit.

Requires Python 3 only; no third-party packages.

## 1to2 safety

1to2 only rewrites bytes that a 2to1 conversion itself wrote. A file must
carry the signature the conversion leaves behind: `IFD0:Model` is `OM-5`
with entry count 5, followed by zero padding from the old 17-byte
`OM-5MarkII` slot, and exactly one `S0101` appears in the MakerNote. Files
that read as `OM-5`/`S0101` without that padding are skipped, so genuine
OM-5 files are left alone in the common case. Caveat: a genuine OM-5 whose
Model string happens to be followed by six or more zero bytes cannot be
distinguished from a converted Mark II without samples of both bodies and
would be reverted too.

In `--in-place` mode, when a `<file>.orig` backup from the original 2to1
run exists, it is compared against the expected result: on a
byte-identical match the tool reports it, and a mismatching backup is
reported and never copied over the file — the file is re-patched instead,
so an unrelated backup cannot corrupt a photo.

## Notes

- Keep an untouched master of every original. `--in-place` writes a `.orig`
  backup, but verify it before relying on it.
- `InternalSerialNumber` is left unchanged. Its leading digits encode the body,
  and the OM-5 Mark I prefix is not publicly documented.
- Cache behaviour after conversion:
  - `exiftool`, macOS ImageIO (Finder, Quick Look) read the file live and show
    `OM-5` immediately (run `mdimport FILE` if Spotlight is stale).
  - Apple Photos stores metadata in its library database at import time and does
    not re-read it. Delete the asset and re-import the converted file to see
    `OM-5` there.
