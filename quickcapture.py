#!/usr/bin/env python3
"""
QuickCapture — modern tethered capture for Canon EOS 1100D
Single camera thread with command queue (darktable-style architecture)
"""

import customtkinter as ctk
import tkinter as tk
import subprocess, threading, time, io, queue, sys
from pathlib import Path
from PIL import Image, ImageTk, ImageDraw
from PIL.Image import Exif

import gphoto2 as gp

SAVE_DIR  = Path.home() / "Pictures" / "QuickCapture"
THUMB_W   = 100
THUMB_H   = 74

# ── palette ───────────────────────────────────────────────────────────────────
BG         = "#000000"
SURFACE    = "#1c1c1e"
SURFACE2   = "#2c2c2e"
ICON_BG    = "#3a3a3c"
YELLOW     = "#ffd60a"
BLUE       = "#0a84ff"
RED        = "#ff453a"
GREEN      = "#30d158"

TEXT_DIM   = "#8e8e93"
TEXT_BRIGHT= "#ffffff"
DIVIDER    = "#38383a"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ── high-quality icons (4x supersampled) ──────────────────────────────────────

def _hq(size, fn):
    sc = 4
    big = size * sc
    img = Image.new("RGBA", (big, big), (0,0,0,0))
    d = ImageDraw.Draw(img)
    fn(d, big, sc)
    return img.resize((size, size), Image.LANCZOS)

def flash_icon(on, size=28):
    def draw(d, s, sc):
        # no background — _make_round_btn provides the circle
        cx, color = s/2, "#000000" if on else "#ffffff"
        d.polygon([
            (cx + 1*sc, 4*sc), (cx - 4*sc, s//2 + 1*sc),
            (cx + 0*sc, s//2 + 1*sc), (cx - 1*sc, s - 4*sc),
            (cx + 4*sc, s//2 - 1*sc), (cx + 0*sc, s//2 - 1*sc),
        ], fill=color)
    return _hq(size, draw)

def af_icon(active, size=32):
    def draw(d, s, sc):
        # no background — _make_round_btn provides the circle
        c = "#ffffff"
        cx, cy = s // 2, s // 2
        # letter A
        ax = cx - 6*sc
        d.line([(ax, cy+5*sc), (ax+4*sc, cy-5*sc)], fill=c, width=2*sc)
        d.line([(ax+4*sc, cy-5*sc), (ax+8*sc, cy+5*sc)], fill=c, width=2*sc)
        d.line([(ax+2*sc, cy+1*sc), (ax+6*sc, cy+1*sc)], fill=c, width=2*sc)
        # letter F
        fx = cx + 2*sc
        d.line([(fx, cy+5*sc), (fx, cy-5*sc)], fill=c, width=2*sc)
        d.line([(fx, cy-5*sc), (fx+6*sc, cy-5*sc)], fill=c, width=2*sc)
        d.line([(fx, cy), (fx+5*sc, cy)], fill=c, width=2*sc)
    return _hq(size, draw)


def rotate_icon(size=28):
    def draw(d, s, sc):
        # no background — _make_round_btn provides the circle
        c = "#ffffff"
        cx, cy = s//2, s//2
        # circular arrow (arc + arrowhead)
        r = 5*sc
        d.arc([cx-r, cy-r, cx+r, cy+r], start=220, end=80, fill=c, width=2*sc)
        # arrowhead at ~80 degrees (top-right)
        import math
        angle = math.radians(80)
        ax = cx + r * math.cos(angle)
        ay = cy - r * math.sin(angle)
        d.polygon([
            (ax, ay),
            (ax - 3*sc, ay - 1*sc),
            (ax - 1*sc, ay + 3*sc),
        ], fill=c)
    return _hq(size, draw)

def _make_round_btn(icon_img, size, bg=None):
    """Composite an icon image centered on a filled circle."""
    sc = 3
    big = size * sc
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, big-1, big-1], fill=bg or ICON_BG)
    # paste icon centered
    iw, ih = icon_img.size
    # scale icon up to match
    icon_big = icon_img.resize((iw * sc, ih * sc), Image.LANCZOS)
    ix = (big - icon_big.width) // 2
    iy = (big - icon_big.height) // 2
    img.paste(icon_big, (ix, iy), icon_big)
    return img.resize((size, size), Image.LANCZOS)

def shutter_ring(size=72, pressed=False):
    def draw(d, s, sc):
        w, gap = 3*sc, 6*sc
        outer = "#999999" if pressed else "#ffffff"
        inner = "#b0b0b0" if pressed else "#ffffff"
        d.ellipse([0, 0, s-1, s-1], outline=outer, width=w)
        d.ellipse([gap, gap, s-gap, s-gap], fill=inner)
    return _hq(size, draw)


# ── document crop (Apple Vision + OpenCV) ────────────────────────────────────

def _prewarm_crop():
    """Pre-import heavy crop libs at startup so first detection is instant."""
    try:
        import numpy, cv2, Vision, Quartz
        from Foundation import NSURL
    except Exception:
        pass

def _crop_document(pil_img):
    """Detect and perspective-correct a document. Returns list of PIL Images."""
    import numpy as np, cv2, Vision, Quartz, tempfile, os
    from Foundation import NSURL

    tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
    try:
        pil_img.save(tmp.name, 'JPEG', quality=95)
        url      = NSURL.fileURLWithPath_(tmp.name)
        ci_image = Quartz.CIImage.imageWithContentsOfURL_(url)
    finally:
        os.unlink(tmp.name)

    if ci_image is None:
        return []

    handler = Vision.VNImageRequestHandler.alloc().initWithCIImage_options_(ci_image, None)
    request = Vision.VNDetectDocumentSegmentationRequest.alloc().init()
    success, _ = handler.performRequests_error_([request], None)
    if not success:
        return []

    results = request.results()
    if not results or len(results) == 0:
        return []

    w, h   = pil_img.size
    cv_img = np.array(pil_img)
    crops  = []

    for obs in results:
        if obs.confidence() < 0.5:
            continue

        tl, tr = obs.topLeft(), obs.topRight()
        br, bl = obs.bottomRight(), obs.bottomLeft()
        corners = np.array([
            [tl.x * w, (1 - tl.y) * h],
            [tr.x * w, (1 - tr.y) * h],
            [br.x * w, (1 - br.y) * h],
            [bl.x * w, (1 - bl.y) * h],
        ], dtype=np.float32)

        width  = int(max(np.linalg.norm(corners[1] - corners[0]),
                         np.linalg.norm(corners[2] - corners[3])))
        height = int(max(np.linalg.norm(corners[3] - corners[0]),
                         np.linalg.norm(corners[2] - corners[1])))
        if width < 50 or height < 50:
            continue

        dst    = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)
        M      = cv2.getPerspectiveTransform(corners, dst)
        warped = cv2.warpPerspective(cv_img, M, (width, height))
        crops.append(Image.fromarray(warped))

    return crops


# ── camera thread ─────────────────────────────────────────────────────────────

class CameraThread:
    def __init__(self, on_frame, on_file, on_status, on_disconnect, get_prefix, get_rotation):
        self._on_frame      = on_frame
        self._on_file       = on_file
        self._on_status     = on_status
        self._on_disconnect = on_disconnect
        self._get_prefix    = get_prefix
        self._get_rotation  = get_rotation
        self._q             = queue.Queue()
        self._running       = True
        self._cam_ref       = None  # live camera reference for shutdown
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def run(self, fn):
        done = threading.Event()
        self._q.put((fn, done))
        return done

    def _save_rotated(self, src_path, dest_path):
        """Apply current rotation to a saved image and set EXIF orientation."""
        rot = self._get_rotation()
        img = Image.open(dest_path)
        if rot:
            img = img.rotate(rot, expand=True)
        exif = Exif()
        exif[0x0112] = 1  # Orientation: pixels are correct, no viewer rotation
        img.save(dest_path, quality=95, exif=exif.tobytes())


    def stop(self):
        self._running = False
        # release camera immediately from main thread if thread is stuck
        cam = self._cam_ref
        if cam is not None:
            try:
                cam.exit()
            except Exception:
                pass
            self._cam_ref = None
        self._thread.join(timeout=2)

    def _drain_queue(self):
        """Discard all pending jobs so UI threads don't block forever."""
        while not self._q.empty():
            _, done = self._q.get()
            done.set()

    def _connect(self):
        """Try to connect to the camera. Returns camera object or None."""
        self._on_status("Connecting...", True)
        subprocess.run(["killall", "-9", "ptpcamerad", "mscamerad", "PTPCamera"],
                       capture_output=True)
        time.sleep(1.5)

        try:
            cam = gp.Camera()
            cam.init()

            cfg = cam.get_config()
            cfg.get_child_by_name("imageformat").set_value("L")
            cam.set_config(cfg)

            cfg = cam.get_config()
            cfg.get_child_by_name("viewfinder").set_value(1)
            cam.set_config(cfg)

            self._cam_ref = cam
            self._on_status("Ready")
            return cam
        except Exception:
            return None

    def _loop(self):
        while self._running:
            cam = None
            while self._running and cam is None:
                cam = self._connect()
                if cam is None:
                    self._drain_queue()
                    self._on_status("No camera -- waiting...", True)
                    time.sleep(2)

            if not self._running:
                return

            # ── main loop ──
            while self._running:
                # drain queued jobs
                while not self._q.empty():
                    fn, done = self._q.get()
                    try:
                        fn(cam)
                    except gp.GPhoto2Error as e:
                        if e.code == gp.GP_ERROR_IO:
                            done.set()
                            break  # disconnect — will reconnect
                        self._on_status(f"⚠ {e}")
                    except Exception as e:
                        self._on_status(f"⚠ {e}")
                    finally:
                        done.set()
                else:
                    # no disconnect during job processing — continue normally
                    # grab a preview frame
                    try:
                        cf   = cam.capture_preview()
                        data = cf.get_data_and_size()
                        img  = Image.open(io.BytesIO(data))
                        self._on_frame(img)
                    except gp.GPhoto2Error as e:
                        if e.code == gp.GP_ERROR_IO:
                            break  # disconnect — will reconnect
                        time.sleep(0.2)
                    except Exception:
                        time.sleep(0.1)

                    # quick non-blocking event poll (manual shutter detection)
                    try:
                        et, ed = cam.wait_for_event(10)
                        if et == gp.GP_EVENT_FILE_ADDED:
                            cf   = cam.file_get(ed.folder, ed.name,
                                                  gp.GP_FILE_TYPE_NORMAL)
                            ts   = time.strftime("%Y-%m-%d_%H-%M-%S")
                            ext  = Path(ed.name).suffix
                            pfx  = self._get_prefix()
                            dest = SAVE_DIR / f"{pfx}_{ts}{ext}"
                            cf.save(str(dest))
                            self._save_rotated(str(dest), str(dest))
                            self._on_file(dest)
                    except Exception:
                        pass

                    continue
                # job loop broke (disconnect during job) — fall through to reconnect
                break

            # ── disconnected or shutting down — clean up ──
            self._drain_queue()
            if not self._running:
                # quitting — release camera cleanly
                try:
                    cam.exit()
                except Exception:
                    pass
                return
            self._cam_ref = None
            self._on_disconnect()
            try:
                cam.exit()
            except Exception:
                pass
            time.sleep(1)


# ── app ───────────────────────────────────────────────────────────────────────

class QuickCaptureApp:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("QuickCapture")
        self.root.configure(fg_color=BG)
        self.root.resizable(True, True)
        self.root.geometry("1100x820")
        self.root.minsize(700, 520)

        self.capture_count = 0
        self.flash_on      = False
        self._rotation     = 0  # 0, 90, 180, 270
        self._raw_frame    = None
        self._thumb_refs   = []
        self._ui_refs      = {}
        self._cam          = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_quit)
        self.root.createcommand("::tk::mac::Quit", self._on_quit)
        self.root.bind("<space>", lambda e: self._do_capture()
                       if not isinstance(e.widget, ctk.CTkEntry) else None)
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        self._cam = CameraThread(
            on_frame      = self._on_frame,
            on_file       = self._on_file,
            on_status     = self._set_status,
            on_disconnect = self._on_disconnect,
            get_prefix    = self._get_prefix,
            get_rotation  = lambda: self._rotation,
        )
        threading.Thread(target=_prewarm_crop, daemon=True).start()

    def run(self):
        self.root.mainloop()

    def _on_quit(self):
        if self._cam:
            self._cam.stop()
        self.root.destroy()

    def _on_done(self):
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        if getattr(sys, 'frozen', False):
            script = Path(sys._MEIPASS) / "process_scans.py"
            python = "python3"
        else:
            script = Path(__file__).parent / "process_scans.py"
            python = sys.executable
        subprocess.Popen(
            [python, str(script), str(SAVE_DIR)],
            start_new_session=True,
        )

    def _get_prefix(self):
        return self._prefix_var.get().strip() or "scan"

    # ── build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── preview area (expands to fill) ──
        self._preview_frame = tk.Frame(self.root, bg=BG)
        self._preview_frame.pack(fill="both", expand=True)

        self._preview_canvas = tk.Canvas(self._preview_frame, bg=BG,
                                         highlightthickness=0)
        self._preview_canvas.pack(fill="both", expand=True)
        self._preview_canvas.bind("<Configure>", self._on_preview_resize)

        self._status_id = None  # canvas text item
        self._status_clear_id = None  # pending after() id

        self._preview_w = 0
        self._preview_h = 0

        # ── bottom bar container ──
        bottom = ctk.CTkFrame(self.root, fg_color=SURFACE, corner_radius=0)
        bottom.pack(fill="x")

        # ── control bar (fixed height) ──
        controls = ctk.CTkFrame(bottom, fg_color="transparent", corner_radius=0, height=90)
        controls.pack(fill="x", padx=20, pady=(12, 0))
        controls.pack_propagate(False)

        # left side: flash + focus buttons
        left = ctk.CTkFrame(controls, fg_color="transparent")
        left.place(relx=0.0, rely=0.5, anchor="w")

        self._flash_pil = _make_round_btn(flash_icon(False), 44)
        self._flash_photo = ImageTk.PhotoImage(self._flash_pil)
        self._flash_cv = tk.Canvas(left, width=44, height=44, bg=SURFACE,
                                   highlightthickness=0)
        self._flash_cv.create_image(22, 22, image=self._flash_photo)
        self._flash_cv.pack(side="left", padx=(0, 10))
        self._flash_cv.bind("<Button-1>", lambda e: self._toggle_flash())

        self._af_pil = _make_round_btn(af_icon(False), 44)
        self._af_photo = ImageTk.PhotoImage(self._af_pil)
        self._af_cv = tk.Canvas(left, width=44, height=44, bg=SURFACE,
                                highlightthickness=0)
        self._af_cv.create_image(22, 22, image=self._af_photo)
        self._af_cv.pack(side="left", padx=(0, 10))
        self._af_cv.bind("<Button-1>", lambda e: self._do_af())

        self._rot_pil = _make_round_btn(rotate_icon(), 44)
        self._rot_photo = ImageTk.PhotoImage(self._rot_pil)
        self._rot_cv = tk.Canvas(left, width=44, height=44, bg=SURFACE,
                                 highlightthickness=0)
        self._rot_cv.create_image(22, 22, image=self._rot_photo)
        self._rot_cv.pack(side="left", padx=(0, 10))
        self._rot_cv.bind("<Button-1>", lambda e: self._do_rotate())

        # center: shutter button
        self._shutter_img = ImageTk.PhotoImage(shutter_ring(72))
        self._ui_refs["shutter"] = self._shutter_img
        self._shutter_cv = tk.Canvas(controls, width=72, height=72,
                                     bg=SURFACE, highlightthickness=0)
        self._shutter_cv.create_image(36, 36, image=self._shutter_img)
        self._shutter_cv.place(relx=0.5, rely=0.5, anchor="center")
        self._shutter_cv.bind("<Button-1>", lambda e: self._do_capture())

        # right side: name field
        right = ctk.CTkFrame(controls, fg_color="transparent")
        right.place(relx=1.0, rely=0.5, anchor="e")

        ctk.CTkLabel(
            right, text="Name",
            font=ctk.CTkFont(size=11), text_color=TEXT_DIM,
        ).pack(side="left", padx=(0, 8))

        self._prefix_var = tk.StringVar(value="scan")
        self._prefix_entry = ctk.CTkEntry(
            right, textvariable=self._prefix_var,
            width=140, height=36, font=ctk.CTkFont(size=13),
            fg_color=SURFACE2, border_color=DIVIDER, text_color=TEXT_BRIGHT,
            corner_radius=10,
        )
        self._prefix_entry.pack(side="left")

        self._done_btn = ctk.CTkButton(
            right, text="Review Crops", width=110, height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=GREEN, hover_color="#2ab84c",
            text_color="#000000", corner_radius=10,
            command=self._on_done,
        )
        self._done_btn.pack(side="left", padx=(12, 0))

        # ── thin separator ──
        ctk.CTkFrame(bottom, fg_color=DIVIDER, height=1, corner_radius=0).pack(fill="x", padx=16, pady=(10, 0))

        # ── photo roll ──
        self._roll_scroll = ctk.CTkScrollableFrame(
            bottom, fg_color=SURFACE, height=THUMB_H + 16,
            orientation="horizontal", corner_radius=0,
        )
        self._roll_scroll.pack(fill="x", padx=8, pady=(4, 8))

    # ── preview scaling ──────────────────────────────────────────────────────

    def _on_preview_resize(self, event):
        self._preview_w = event.width
        self._preview_h = event.height
        if self._raw_frame is not None:
            self._render_frame(self._raw_frame)

    def _render_frame(self, img):
        """Scale frame to fit preview area, maintaining aspect ratio."""
        pw, ph = self._preview_w, self._preview_h
        if pw < 10 or ph < 10:
            return

        if self._rotation:
            img = img.rotate(self._rotation, expand=True)

        iw, ih = img.size
        scale = min(pw / iw, ph / ih)
        nw = int(iw * scale)
        nh = int(ih * scale)
        display = img.resize((nw, nh), Image.LANCZOS)

        photo = ImageTk.PhotoImage(display)
        self._ui_refs["preview"] = photo
        self._preview_canvas.delete("all")
        self._preview_canvas.create_image(pw // 2, ph // 2, image=photo, anchor="center")

    # ── camera callbacks ──────────────────────────────────────────────────────

    def _on_frame(self, img):
        self.root.after(0, self._show_frame, img)

    def _show_frame(self, img):
        self._raw_frame = img
        self._render_frame(img)

    def _on_file(self, path):
        self.root.after(0, self._file_received, path)

    def _file_received(self, path):
        self.capture_count += 1
        self._add_thumb(path)

    def _set_status(self, msg, persist=False):
        def _update():
            # cancel pending clear
            if self._status_clear_id is not None:
                self.root.after_cancel(self._status_clear_id)
                self._status_clear_id = None
            # remove old text
            if self._status_id is not None:
                self._preview_canvas.delete(self._status_id)
                self._status_id = None
            if msg:
                pw = self._preview_w or 400
                self._status_id = self._preview_canvas.create_text(
                    pw // 2, 24, text=msg, fill=TEXT_DIM,
                    font=("Helvetica", 13))
            if not persist and msg:
                self._status_clear_id = self.root.after(
                    2500, lambda: self._clear_status())
        self.root.after(0, _update)

    def _clear_status(self):
        if self._status_id is not None:
            self._preview_canvas.delete(self._status_id)
            self._status_id = None
        self._status_clear_id = None

    def _on_disconnect(self):
        self._set_status("Disconnected -- replug USB", True)

    def _do_rotate(self):
        self._rotation = (self._rotation + 90) % 360
        if self._raw_frame is not None:
            self._render_frame(self._raw_frame)


    # ── flash ─────────────────────────────────────────────────────────────────

    def _toggle_flash(self):
        self.flash_on = not self.flash_on
        self._flash_pil = _make_round_btn(flash_icon(self.flash_on), 44,
                                          bg=YELLOW if self.flash_on else None)
        self._flash_photo = ImageTk.PhotoImage(self._flash_pil)
        self._flash_cv.delete("all")
        self._flash_cv.create_image(22, 22, image=self._flash_photo)
        self._set_status("Flash on" if self.flash_on else "Flash off")

        want_on = self.flash_on

        def flash_job(cam):
            # switch between Green (auto flash) and Flash Off exposure mode
            cfg = cam.get_config()
            mode = cfg.get_child_by_name("autoexposuremode")
            mode.set_value("Green" if want_on else "Flash Off")
            cam.set_single_config("autoexposuremode", mode)
            time.sleep(0.3)

        def after():
            self._set_status("Flash on" if want_on else "Flash off")

        def run():
            done = self._cam.run(flash_job)
            done.wait()
            self.root.after(0, after)

        threading.Thread(target=run, daemon=True).start()

    # ── autofocus ─────────────────────────────────────────────────────────────

    def _update_af_btn(self, active):
        self._af_pil = _make_round_btn(af_icon(active), 44,
                                       bg=BLUE if active else None)
        self._af_photo = ImageTk.PhotoImage(self._af_pil)
        self._af_cv.delete("all")
        self._af_cv.create_image(22, 22, image=self._af_photo)

    def _do_af(self):
        self._update_af_btn(True)
        self._set_status("Focusing...", True)

        def af_job(cam):
            # autofocusdrive is a TOGGLE — must reset to 0 then set to 1
            # otherwise gphoto2 caches value=1 and repeat presses are no-ops
            cfg = cam.get_config()
            af = cfg.get_child_by_name("autofocusdrive")
            af.set_value(0)
            cam.set_single_config("autofocusdrive", af)
            time.sleep(0.1)
            cfg = cam.get_config()
            af = cfg.get_child_by_name("autofocusdrive")
            af.set_value(1)
            cam.set_single_config("autofocusdrive", af)
            time.sleep(2.0)  # give lens time to seek and lock

        def after():
            self._update_af_btn(False)
            self._set_status("Ready")

        def run():
            done = self._cam.run(af_job)
            done.wait()
            self.root.after(0, after)

        threading.Thread(target=run, daemon=True).start()

    # ── capture ───────────────────────────────────────────────────────────────

    def _do_capture(self):
        self._animate_shutter()
        self._set_status("Capturing...", True)
        rot = self._rotation  # capture current rotation at time of click

        def capture_job(cam):
            for val in ("Press Half","Press Full","Release Full","Release Half"):
                cfg = cam.get_config()
                r = cfg.get_child_by_name("eosremoterelease")
                r.set_value(val)
                cam.set_single_config("eosremoterelease", r)
                time.sleep(0.25)

            deadline = time.time() + 8
            while time.time() < deadline:
                et, ed = cam.wait_for_event(300)
                if et == gp.GP_EVENT_FILE_ADDED:
                    cf = cam.file_get(ed.folder, ed.name, gp.GP_FILE_TYPE_NORMAL)
                    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
                    ext = Path(ed.name).suffix
                    pfx = self._get_prefix()
                    dest = SAVE_DIR / f"{pfx}_{ts}{ext}"
                    cf.save(str(dest))
                    img = Image.open(str(dest))
                    if rot:
                        img = img.rotate(rot, expand=True)
                    exif = Exif()
                    exif[0x0112] = 1  # Orientation: pixels are correct
                    img.save(str(dest), quality=95, exif=exif.tobytes())
                    self._on_file(dest)

                    time.sleep(0.8)
                    cfg = cam.get_config()
                    cfg.get_child_by_name("viewfinder").set_value(1)
                    cam.set_config(cfg)
                    time.sleep(0.5)
                    return

            raise RuntimeError("Timed out — no image received")

        def run():
            done = self._cam.run(capture_job)
            done.wait()
            self.root.after(0, lambda: self._set_status("Ready"))

        threading.Thread(target=run, daemon=True).start()

    def _animate_shutter(self):
        p = ImageTk.PhotoImage(shutter_ring(72, pressed=True))
        self._ui_refs["sp"] = p
        self._shutter_cv.delete("all")
        self._shutter_cv.create_image(36, 36, image=p)
        self.root.after(120, self._reset_shutter)

    def _reset_shutter(self):
        p = ImageTk.PhotoImage(shutter_ring(72))
        self._ui_refs["shutter"] = p
        self._shutter_cv.delete("all")
        self._shutter_cv.create_image(36, 36, image=p)

    # ── photo roll ────────────────────────────────────────────────────────────

    def _add_thumb(self, path):
        try:
            img  = Image.open(path)
            w, h = img.size
            thumb_w = max(1, int(THUMB_H * w / h))
            img = img.resize((thumb_w, THUMB_H), Image.LANCZOS)

            mask = Image.new("L", (thumb_w, THUMB_H), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, thumb_w-1, THUMB_H-1], radius=10, fill=255)
            img.putalpha(mask)

            photo = ctk.CTkImage(img, size=(thumb_w, THUMB_H))
            self._thumb_refs.append(photo)

            # pair container groups original + crop side by side
            pair = ctk.CTkFrame(self._roll_scroll, fg_color="transparent")
            pair.pack(side="left", padx=2, pady=4)

            lbl = ctk.CTkLabel(pair, image=photo, text="", fg_color="transparent")
            lbl.pack(side="left", padx=(4, 2))
            lbl.bind("<Button-1>", lambda e, p=str(path): subprocess.Popen(["open", p]))

            # placeholder shown while crop is processing
            crop_lbl = ctk.CTkLabel(
                pair, text="···", fg_color=SURFACE2,
                width=THUMB_H, height=THUMB_H, corner_radius=10,
                text_color=TEXT_DIM, font=ctk.CTkFont(size=14),
            )
            crop_lbl.pack(side="left", padx=(0, 4))

            threading.Thread(
                target=self._do_crop, args=(path, pair, crop_lbl), daemon=True
            ).start()
        except Exception as e:
            print(f"Thumb: {e}")

    def _do_crop(self, path, pair, crop_lbl):
        """Background thread: detect documents, update placeholders on main thread."""
        try:
            crops = _crop_document(Image.open(path))
            if crops:
                processed_dir = SAVE_DIR / "processed"
                processed_dir.mkdir(exist_ok=True)
                stem = Path(path).stem
                for i, crop_img in enumerate(crops):
                    suffix    = f"_crop{i+1}" if len(crops) > 1 else "_crop"
                    crop_path = processed_dir / f"{stem}{suffix}.jpg"
                    crop_img.save(crop_path, "JPEG", quality=95)
                    if i == 0:
                        self.root.after(0, self._show_crop_success, crop_lbl, crop_img, crop_path)
                    else:
                        self.root.after(0, self._add_crop_thumb, pair, crop_img, crop_path)
            else:
                self.root.after(0, self._show_crop_failed, crop_lbl)
        except Exception:
            self.root.after(0, self._show_crop_failed, crop_lbl)

    def _show_crop_success(self, crop_lbl, crop_img, crop_path):
        w, h    = crop_img.size
        thumb_w = max(1, int(THUMB_H * w / h))
        thumb   = crop_img.resize((thumb_w, THUMB_H), Image.LANCZOS)
        mask = Image.new("L", (thumb_w, THUMB_H), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, thumb_w-1, THUMB_H-1], radius=10, fill=255)
        thumb.putalpha(mask)
        photo = ctk.CTkImage(thumb, size=(thumb_w, THUMB_H))
        self._thumb_refs.append(photo)
        crop_lbl.configure(image=photo, text="", fg_color="transparent",
                           width=thumb_w, height=THUMB_H)
        crop_lbl.bind("<Button-1>", lambda e, img=crop_img: self._open_image_popup(img))

    def _show_crop_failed(self, crop_lbl):
        crop_lbl.configure(fg_color="#3a1010", text="✗",
                           text_color=RED, font=ctk.CTkFont(size=18))

    def _add_crop_thumb(self, pair, crop_img, crop_path):
        """Add an extra crop thumbnail to an existing pair frame (2nd, 3rd… crops)."""
        new_lbl = ctk.CTkLabel(pair, text="", fg_color="transparent")
        new_lbl.pack(side="left", padx=(0, 4))
        self._show_crop_success(new_lbl, crop_img, crop_path)

    def _open_image_popup(self, img):
        """Show a crop image in a floating window. Click or Escape to close."""
        popup = ctk.CTkToplevel(self.root)
        popup.title("Crop Preview")
        popup.configure(fg_color=BG)
        popup.attributes("-topmost", True)

        # fit image to 80% of screen
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        max_w, max_h = int(sw * 0.8), int(sh * 0.8)
        iw, ih = img.size
        scale  = min(max_w / iw, max_h / ih, 1.0)
        dw, dh = max(1, int(iw * scale)), max(1, int(ih * scale))

        display = img.resize((dw, dh), Image.LANCZOS)
        photo   = ctk.CTkImage(display, size=(dw, dh))
        self._thumb_refs.append(photo)

        lbl = ctk.CTkLabel(popup, image=photo, text="", fg_color=BG)
        lbl.pack(padx=16, pady=16)

        close = lambda e=None: popup.destroy()
        popup.bind("<Escape>", close)
        lbl.bind("<Button-1>", close)
        popup.geometry(f"{dw + 32}x{dh + 32}")


if __name__ == "__main__":
    import fcntl, sys, atexit, signal
    lockfile = open("/tmp/quickcapture.lock", "w")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("QuickCapture is already running.")
        sys.exit(0)
    app = QuickCaptureApp()

    def _cleanup(*_):
        if app._cam:
            app._cam.stop()
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(0)))

    app.run()
