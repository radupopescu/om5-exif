# om5-exif-update

Relabels an OM System OM-5 Mark II raw file (`.ORF`) as an original OM-5.

The two bodies share the same sensor and ORF container. 

## What it changes

Two fields are patched in place, byte for byte:

| Location             | Before       | After     |
| -------------------- | ------------ | --------- |
| `IFD0:Model`         | `OM-5MarkII` | `OM-5`    |
| `Olympus:CameraType2`| `S0130`      | `S0101`   |

`S0101` is ExifTool's canonical code for the OM-5. The `IFD0:Model` entry count
is updated from 17 to 5 to match the shorter string.

The patch does not move any offset and does not touch image, preview or
thumbnail data. Output size equals input size; only the changed bytes differ.
Independent verification on a real file: 15 bytes differ, the embedded 3200x2400
preview and the raw sensor strip are byte-identical, and `exiftool -validate`
output is unchanged.

## Usage

```
./om5-exif-update.py RAW.ORF             # write converted copy to ./om5-converted/
./om5-exif-update.py --in-place RAW.ORF  # edit original, keep RAW.ORF.orig backup
./om5-exif-update.py -n RAW.ORF          # dry run, report changes only
./om5-exif-update.py -o out DIR_OR_FILE  # choose output directory
```

Directories are searched recursively for `.orf`, `.jpg` and `.jpeg`. Files that
are not OM-5 Mark II raw files are skipped, which makes re-runs safe.

Requires Python 3 only; no third-party packages.

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
