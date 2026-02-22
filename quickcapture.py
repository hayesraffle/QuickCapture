#!/usr/bin/env python3
"""
QuickCapture — modern tethered capture for Canon EOS 1100D
Single camera thread with command queue (darktable-style architecture)
"""

import customtkinter as ctk
import tkinter as tk
import subprocess, threading, time, io, queue
from pathlib import Path
from PIL import Image, ImageTk, ImageDraw

import gphoto2 as gp

SAVE_DIR  = Path.home() / "Desktop" / "Scans"
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
        """Apply current rotation to a saved image."""
        rot = self._get_rotation()
        if rot:
            img = Image.open(dest_path)
            img = img.rotate(rot, expand=True)
            img.save(dest_path, quality=95)


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

    def run(self):
        self.root.mainloop()

    def _on_quit(self):
        if self._cam:
            self._cam.stop()
        self.root.destroy()

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
                    if rot:
                        img = Image.open(str(dest))
                        img = img.rotate(rot, expand=True)
                        img.save(str(dest), quality=95)
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
            tw   = int(h * 3/2)
            if tw < w:
                left = (w - tw) // 2
                img = img.crop((left, 0, left + tw, h))
            img = img.resize((THUMB_W, THUMB_H), Image.LANCZOS)

            # rounded corners
            mask = Image.new("L", (THUMB_W, THUMB_H), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, THUMB_W-1, THUMB_H-1], radius=10, fill=255)
            img.putalpha(mask)

            photo = ctk.CTkImage(img, size=(THUMB_W, THUMB_H))
            self._thumb_refs.append(photo)

            lbl = ctk.CTkLabel(self._roll_scroll, image=photo, text="",
                               fg_color="transparent")
            lbl.pack(side="left", padx=4, pady=4)
            lbl.bind("<Button-1>", lambda e, p=str(path): subprocess.Popen(["open", p]))
        except Exception as e:
            print(f"Thumb: {e}")


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
