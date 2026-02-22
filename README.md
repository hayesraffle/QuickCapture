# QuickCapture

Tethered capture app for Canon EOS cameras on macOS. Live preview, one-click shutter, flash and autofocus controls, image rotation, and a photo roll — all in a minimal dark UI.

Built with Python, [gphoto2](http://gphoto.org/), and [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter).

## Features

- Live viewfinder preview over USB
- Capture via on-screen shutter button or spacebar
- Flash toggle (Green / Flash Off exposure modes)
- Autofocus trigger
- Rotate preview and saved images (90° increments)
- Configurable filename prefix
- Scrollable photo roll with thumbnail previews
- Single-instance lock to prevent duplicate launches

## Requirements

- macOS
- Python 3
- Canon EOS camera connected via USB (tested with EOS 1100D)

### Python dependencies

```
gphoto2
customtkinter
Pillow
```

Install with:

```bash
pip install gphoto2 customtkinter Pillow
```

## Usage

Double-click `QuickCapture.command`, or run from a terminal:

```bash
python3 quickcapture.py
```

Images are saved to `~/Desktop/Scans/`.
