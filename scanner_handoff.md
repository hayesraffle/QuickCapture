# Scanner App — Handoff Report

## Goal
A simple macOS scanning app for a **Canon EOS 1100D** connected via USB.
Target user is non-technical. Features needed:
- Live preview (so subject can be centered before capture)
- One-tap capture button
- Autofocus button
- Flash on/off toggle
- Photo roll (thumbnails of captured images)
- Manual shutter button on camera should also work

---

## Environment
- **Mac**: macOS 14.6.1 (Sonoma), Apple Silicon (arm64)
- **Camera**: Canon EOS 1100D with Magic Lantern firmware
- **Language**: Python 3.14 (Homebrew)
- **App file**: `~/Desktop/scanner.py`
- **Output folder**: `~/Desktop/Scans/`

### Dependencies (all installed)
```
brew install gphoto2
pip3 install gphoto2 Pillow --break-system-packages
```
- `gphoto2` CLI: `/opt/homebrew/bin/gphoto2`
- `python-gphoto2`: bindings for libgphoto2 (v2.6.3)
- `Pillow`: image display in tkinter
- `python-tk@3.14`: tkinter support (had to install separately)

---

## Key Technical Findings

### macOS daemon conflict
macOS auto-launches `ptpcamerad` and `mscamerad` which claim the USB device
and block gphoto2. Must kill them before connecting:
```bash
killall ptpcamerad mscamerad PTPCamera
```
This must be done every time before connecting. The app does this at startup.

### Camera config keys confirmed working on 1100D
| Feature | Config key | Value |
|---|---|---|
| Image format | `imageformat` | `"L"` (Large JPEG) |
| Live view on/off | `viewfinder` | `1` / `0` |
| Flash pop-up | `popupflash` | TOGGLE (raises physical flash) |
| Autofocus | `autofocusdrive` | `1` (TOGGLE type) |
| Remote shutter | `eosremoterelease` | `"Press Half"` → `"Press Full"` → `"Release Full"` → `"Release Half"` |

### Live view
- `viewfinder=1` enables live view, mirror stays up
- Preview frames captured via `camera.capture_preview()`
- **Important**: `autofocusdrive` does NOT work while live view is active
- AF requires: disable live view → trigger AF → re-enable live view (Canon "Quick AF" mode)

### Architecture — critical insight
**The entire reason things kept breaking** is that gphoto2 is not thread-safe.
Having two threads (preview loop + event loop) both calling camera methods
simultaneously causes `-110 I/O in progress` errors.

**The fix** (current implementation): single `CameraThread` class with a
`queue.Queue`. All camera operations — preview frames, AF, capture, event
polling — execute sequentially in one thread. The UI submits jobs via
`CameraThread.run(fn)` which returns a `threading.Event` the caller can `.wait()` on.

The main loop in `CameraThread._loop()`:
1. Drain command queue (AF / capture jobs)
2. Capture one preview frame
3. Poll for file-added events (10ms timeout) — detects manual shutter press
4. Repeat

---

## Current State of Each Feature

| Feature | Status | Notes |
|---|---|---|
| Live preview | ✅ Working | Native camera resolution, no upscaling |
| Capture button | ⚠️ Untested with new architecture | Uses `eosremoterelease` sequence |
| Manual shutter | ⚠️ Untested with new architecture | Detected via `GP_EVENT_FILE_ADDED` polling |
| Photo roll | ✅ Working | Rounded thumbnails, auto-scrolls |
| Flash toggle | ✅ UI only | Flash stays popped in use; toggle is visual indicator |
| AF button | ⚠️ Untested with new architecture | Drops live view → phase-detect → restores |
| Disconnect handling | ✅ | Catches `GP_ERROR_IO`, shows message |

---

## What's Been Tried for AF
1. `autofocusdrive=1` via `set_config` — failed (live view blocks it)
2. `eosremoterelease="Press Half"` — popped flash, didn't move lens
3. **Current approach**: disable `viewfinder`, trigger `autofocusdrive=1`, re-enable `viewfinder`
   - This is Canon's "Quick Mode" AF — mirror drops briefly, phase-detect locks, mirror returns
   - Should work but untested with the new single-thread architecture

---

## What the New Architecture Looks Like

```python
class CameraThread:
    def run(self, fn):
        # queues fn(camera) for serial execution
        # returns threading.Event — caller can .wait()

# Usage:
def af_job(cam):
    # disable live view
    cfg = cam.get_config()
    cfg.get_child_by_name("viewfinder").set_value(0)
    cam.set_config(cfg)
    time.sleep(0.3)
    # trigger AF
    cfg = cam.get_config()
    w = cfg.get_child_by_name("autofocusdrive")
    w.set_value(1)
    cam.set_single_config("autofocusdrive", w)
    time.sleep(1.2)
    # restore live view
    cfg = cam.get_config()
    cfg.get_child_by_name("viewfinder").set_value(1)
    cam.set_config(cfg)

done = self._cam.run(af_job)
done.wait()  # optional — block until complete
```

---

## Suggestions for Next Assistant
1. **Test the new single-thread architecture first** — launch `scanner.py`,
   check if preview is live and capture works
2. If capture hangs: the `eosremoterelease` sequence may need timing adjustments
   or try `camera.capture(gp.GP_CAPTURE_IMAGE)` instead
3. If AF still doesn't move the lens: check if the lens is in **MF mode**
   (physical switch on the lens barrel) — this overrides all software AF
4. The `python-gphoto2` library has good examples at:
   https://github.com/jim-easterbrook/python-gphoto2/tree/main/examples
   Specifically `capture-tethered.py` for the file-added event pattern
5. Consider using `gp.check_result(gp.use_python_logging())` at startup
   to get verbose gphoto2 debug output

---

## Running the App
```bash
killall ptpcamerad mscamerad PTPCamera 2>/dev/null
python3 ~/Desktop/scanner.py
```
