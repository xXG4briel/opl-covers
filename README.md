# OPL Covers

Downloads artwork for **Open PS2 Loader** straight into the `ART` folder of your USB drive.

## Usage

```bash
./run.sh          # opens the GUI (first run creates .venv and installs requirements.txt)
./run.sh --reset  # clears the art index cache
```

1. The app detects the USB drive and asks you to confirm it.
2. Pick what to download: front cover, back cover, disc (icon), spine, logo, background, screenshots.
3. Select the games and click **Download art**.

The game ID is read from the file name (`SLUS_200.62.Name.iso`), from `SYSTEM.CNF` inside the
ISO, or from `ul.cfg`. If it can't be found (e.g. `.zso`), double-click the game and type the ID.

## Art formats: why PNG is the default

**Save everything as PNG** is checked by default. Some OPL builds/themes fail to load `.jpg`
art (the cover simply doesn't show) while `.png` loads fine. OPL accepts both extensions
(`SLUS_216.78_COV.png` works the same as `.jpg`), so PNG is the safe choice. When saving as
PNG, any `.jpg` version of the same art is removed so OPL never has two candidates.

Art files are named `<GAME_ID>_<TYPE>.<ext>`:

| Type | Meaning | Size |
|---|---|---|
| `COV` | Front cover | 140x200 |
| `COV2` | Back cover | 242x344 |
| `ICO` | Disc icon | 64x64 |
| `LAB` | Spine | 18x240 |
| `LGO` | Logo | 300x125 |
| `BG_00` | Background | 640x480 |
| `SCR_00`, `SCR_01` | Screenshots | 250x188 |

## Sources

- OPL Manager art database (backup `OPLM_ART_2023_11` on archive.org); a ~20 MB index of it
  is cached in `cache/` on first run
- Front cover fallback: [xlenore/ps2-covers](https://github.com/xlenore/ps2-covers),
  resized to 140x200

## Requirements

Python 3.9+ with `tkinter` and `venv` (Ubuntu/Mint: `sudo apt install python3-tk python3-venv`).
Dependencies (`requests`, `Pillow`) are installed into `.venv` by `run.sh`.
