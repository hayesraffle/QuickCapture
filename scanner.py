#!/usr/bin/env python3
"""
Scanner — modern tethered capture for Canon EOS 1100D
Single camera thread with command queue (darktable-style architecture)
"""

import customtkinter as ctk
import tkinter as tk
import subprocess, threading, time, io, queue
from pathlib import Path
from PIL import Image, ImageTk, ImageDraw

import gphoto2 as gp

SAVE_DIR  = Path.home() / "Desktop" / "Scans"
THUMB_W   = 120
THUMB_H   = 80

# ── palette ───────────────────────────────────────────────────────────────────
BG         = "#000000"
SURFACE    = "#1c1c1e"
SURFACE2   = "#2c2c2e"
ICON_BG    = "#48484a"
YELLOW     = "#ffd60a"
BLUE       = "#0a84ff"
GREEN      = "#30d158"
TEXT_DIM   = "#98989d"
TEXT_BRIGHT= "#ffffff"
DIVIDER    = "#38383a"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ── high-quality icons (3x supersampled) ──────────────────────────────────────

def _hq(size, fn):
    sc = 3
    big = size * sc
    img = Image.new("RGBA", (big, big), (0,0,0,0))
    d = ImageDraw.Draw(img)
    fn(d, big, sc)
    return img.resize((size, size), Image.LANCZOS)

def flash_icon(on, size=36):
    def draw(d, s, sc):
        d.ellipse([0, 0, s-1, s-1], fill=YELLOW if on else ICON_BG)
        cx, color = s/2, "#000000" if on else "#ffffff"
        d.polygon([
            (cx + 1*sc, 3*sc), (cx - 4*sc, s//2 + 1*sc),
            (cx + 0*sc, s//2 + 1*sc), (cx - 1*sc, s - 3*sc),
            (cx + 4*sc, s//2 - 1*sc), (cx + 0*sc, s//2 - 1*sc),
        ], fill=color)
    return _hq(size, draw)

def af_icon(active, size=36):
    def draw(d, s, sc):
        d.ellipse([0, 0, s-1, s-1], fill=BLUE if active else ICON_BG)
        m, r, t, c = 10*sc, 7*sc, 3*sc, "#ffffff"
        for rect in [
            [m, m, m+r, m+t], [m, m, m+t, m+r],
            [s-m-r, m, s-m, m+t], [s-m-t, m, s-m, m+r],
            [m, s-m-t, m+r, s-m], [m, s-m-r, m+t, s-m],
            [s-m-r, s-m-t, s-m, s-m], [s-m-t, s-m-r, s-m, s-m],
        ]:
            d.rounded_rectangle(rect, radius=sc, fill=c)
        cx = s // 2
        d.ellipse([cx-2*sc, cx-2*sc, cx+2*sc, cx+2*sc], fill=c)
    return _hq(size, draw)

def zoom_icon(zoomed, size=36):
    def draw(d, s, sc):
        d.ellipse([0, 0, s-1, s-1], fill=GREEN if zoomed else ICON_BG)
        c = "#ffffff"
        cx, cy = s // 2, s // 2
        # magnifying glass
        r = 6 * sc
        d.ellipse([cx - r, cy - r - 2*sc, cx + r, cy + r - 2*sc], outline=c, width=2*sc)
        # handle
        d.line([(cx + 4*sc, cy + 4*sc - 2*sc), (cx + 8*sc, cy + 8*sc - 2*sc)],
               fill=c, width=3*sc)
        if zoomed:
            # "+" inside lens
            d.line([(cx - 3*sc, cy - 2*sc), (cx + 3*sc, cy - 2*sc)], fill=c, width=2*sc)
            d.line([(cx, cy - 5*sc), (cx, cy + 1*sc)], fill=c, width=2*sc)
    return _hq(size, draw)

def shutter_ring(size=80, pressed=False):
    def draw(d, s, sc):
        w, gap = 4*sc, 8*sc
        c = "#999999" if pressed else "#ffffff"
        d.ellipse([0, 0, s-1, s-1], outline=c, width=w)
        d.ellipse([gap, gap, s-gap, s-gap], fill=c)
    return _hq(size, draw)


# ── camera thread ─────────────────────────────────────────────────────────────

class CameraThread:
    def __init__(self, on_frame, on_file, on_status, on_disconnect, get_prefix):
        self._on_frame      = on_frame
        self._on_file       = on_file
        self._on_status     = on_status
        self._on_disconnect = on_disconnect
        self._get_prefix    = get_prefix
        self._q             = queue.Queue()
        self._running       = True
        threading.Thread(target=self._loop, daemon=True).start()

    def run(self, fn):
        done = threading.Event()
        self._q.put((fn, done))
        return done

    def stop(self):
        self._running = False

    def _drain_queue(self):
        """Discard all pending jobs so UI threads don't block forever."""
        while not self._q.empty():
            _, done = self._q.get()
            done.set()

    def _connect(self):
        """Try to connect to the camera. Returns camera object or None."""
        self._on_status("Connecting…")
        # SIGKILL — macOS PTP daemons survive regular SIGTERM
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

            self._on_status("Ready")
            return cam
        except Exception:
            return None

    def _loop(self):
        while self._running:
            # ── connect (retry until success) ──
            cam = None
            while self._running and cam is None:
                cam = self._connect()
                if cam is None:
                    self._drain_queue()
                    self._on_status("No camera — waiting…")
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
                            self._on_file(dest)
                    except Exception:
                        pass

                    continue
                # job loop broke (disconnect during job) — fall through to reconnect
                break

            # ── disconnected — clean up and retry ──
            self._drain_queue()
            self._on_disconnect()
            try:
                cam.exit()
            except Exception:
                pass
            time.sleep(1)


# ── app ───────────────────────────────────────────────────────────────────────

class ScannerApp:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("Scanner")
        self.root.configure(fg_color=BG)
        self.root.resizable(True, True)
        self.root.geometry("1024x768")
        self.root.minsize(640, 480)

        self.capture_count = 0
        self.flash_on      = False
        self.zoomed        = False   # False = normal, True = 5x sensor zoom
        self._raw_frame    = None    # latest raw PIL frame from camera
        self._thumb_refs   = []
        self._ui_refs      = {}
        self._cam          = None

        self._build_ui()
        self.root.bind("<space>", lambda e: self._do_capture()
                       if not isinstance(e.widget, ctk.CTkEntry) else None)
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        self._cam = CameraThread(
            on_frame      = self._on_frame,
            on_file       = self._on_file,
            on_status     = self._set_status,
            on_disconnect = self._on_disconnect,
            get_prefix    = self._get_prefix,
        )

    def run(self):
        self.root.mainloop()

    def _get_prefix(self):
        return self._prefix_var.get().strip() or "scan"

    # ── build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── preview (expands to fill available space) ──
        self._preview_frame = tk.Frame(self.root, bg=BG)
        self._preview_frame.pack(fill="both", expand=True)

        self._preview_canvas = tk.Canvas(self._preview_frame, bg=BG,
                                         highlightthickness=0)
        self._preview_canvas.pack(fill="both", expand=True)
        self._preview_canvas.bind("<Configure>", self._on_preview_resize)
        self._preview_canvas.bind("<Button-1>", lambda e: self._toggle_zoom())
        self._preview_w = 0
        self._preview_h = 0

        # ── divider ──
        ctk.CTkFrame(self.root, fg_color=DIVIDER, height=1, corner_radius=0).pack(fill="x")

        # ── control bar (buttons + shutter + status) ──
        controls = ctk.CTkFrame(self.root, fg_color=SURFACE, corner_radius=0, height=80)
        controls.pack(fill="x")
        controls.pack_propagate(False)

        # left side: flash, focus, zoom buttons
        left = ctk.CTkFrame(controls, fg_color="transparent")
        left.pack(side="left", padx=(12, 0))

        self._flash_img = ctk.CTkImage(flash_icon(False), size=(32, 32))
        self._flash_btn = ctk.CTkButton(
            left, image=self._flash_img, text="Flash",
            font=ctk.CTkFont(size=9), text_color=TEXT_DIM,
            fg_color="transparent", hover_color=SURFACE2,
            width=50, height=64, compound="top",
            command=self._toggle_flash,
        )
        self._flash_btn.pack(side="left", padx=2, pady=4)

        self._af_img = ctk.CTkImage(af_icon(False), size=(32, 32))
        self._af_btn = ctk.CTkButton(
            left, image=self._af_img, text="Focus",
            font=ctk.CTkFont(size=9), text_color=TEXT_DIM,
            fg_color="transparent", hover_color=SURFACE2,
            width=50, height=64, compound="top",
            command=self._do_af,
        )
        self._af_btn.pack(side="left", padx=2, pady=4)

        self._zoom_img = ctk.CTkImage(zoom_icon(False), size=(32, 32))
        self._zoom_btn = ctk.CTkButton(
            left, image=self._zoom_img, text="Zoom",
            font=ctk.CTkFont(size=9), text_color=TEXT_DIM,
            fg_color="transparent", hover_color=SURFACE2,
            width=50, height=64, compound="top",
            command=self._toggle_zoom,
        )
        self._zoom_btn.pack(side="left", padx=2, pady=4)

        # center: shutter button
        self._shutter_img = ImageTk.PhotoImage(shutter_ring(64))
        self._ui_refs["shutter"] = self._shutter_img
        self._shutter_cv = tk.Canvas(controls, width=64, height=64,
                                     bg=SURFACE, highlightthickness=0)
        self._shutter_cv.create_image(32, 32, image=self._shutter_img)
        self._shutter_cv.place(relx=0.5, rely=0.5, anchor="center")
        self._shutter_cv.bind("<Button-1>", lambda e: self._do_capture())

        # right side: name field + status, vertically centered and right-aligned
        right = ctk.CTkFrame(controls, fg_color="transparent")
        right.pack(side="right", padx=(0, 16), fill="y")

        # vertical centering wrapper
        spacer_top = ctk.CTkFrame(right, fg_color="transparent")
        spacer_top.pack(expand=True)

        name_row = ctk.CTkFrame(right, fg_color="transparent")
        name_row.pack()

        ctk.CTkLabel(
            name_row, text="Name:",
            font=ctk.CTkFont(size=12), text_color=TEXT_DIM,
        ).pack(side="left", padx=(0, 6))

        self._prefix_var = tk.StringVar(value="scan")
        self._prefix_entry = ctk.CTkEntry(
            name_row, textvariable=self._prefix_var,
            width=180, height=28, font=ctk.CTkFont(size=12),
            fg_color=SURFACE2, border_color=DIVIDER, text_color=TEXT_BRIGHT,
        )
        self._prefix_entry.pack(side="left")

        self._status_label = ctk.CTkLabel(
            right, text="",
            font=ctk.CTkFont(size=10), text_color=TEXT_DIM,
        )
        self._status_label.pack(pady=(2, 0))

        spacer_bot = ctk.CTkFrame(right, fg_color="transparent")
        spacer_bot.pack(expand=True)

        # ── divider ──
        ctk.CTkFrame(self.root, fg_color=DIVIDER, height=1, corner_radius=0).pack(fill="x")

        # ── photo roll ──
        roll_frame = ctk.CTkFrame(self.root, fg_color=SURFACE, corner_radius=0)
        roll_frame.pack(fill="x")

        self._roll_scroll = ctk.CTkScrollableFrame(
            roll_frame, fg_color=SURFACE, height=THUMB_H + 20,
            orientation="horizontal", corner_radius=0,
        )
        self._roll_scroll.pack(fill="x", padx=0)

    # ── preview scaling ──────────────────────────────────────────────────────

    def _on_preview_resize(self, event):
        self._preview_w = event.width
        self._preview_h = event.height
        # re-render current frame at new size
        if self._raw_frame is not None:
            self._render_frame(self._raw_frame)

    def _render_frame(self, img):
        """Scale frame to fit preview area, maintaining aspect ratio."""
        pw, ph = self._preview_w, self._preview_h
        if pw < 10 or ph < 10:
            return

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

    def _set_status(self, msg):
        self.root.after(0, self._status_label.configure, {"text": msg})

    def _on_disconnect(self):
        self._set_status("Disconnected — replug USB")

    # ── zoom toggle (5x sensor zoom via eoszoom) ───────────────────────────

    def _toggle_zoom(self):
        self.zoomed = not self.zoomed
        self._zoom_img = ctk.CTkImage(zoom_icon(self.zoomed), size=(32, 32))
        self._zoom_btn.configure(
            image=self._zoom_img,
            text="5x" if self.zoomed else "Zoom",
            text_color=GREEN if self.zoomed else TEXT_DIM,
        )
        self._set_status("Zooming in…" if self.zoomed else "Zooming out…")

        want_zoom = self.zoomed

        def zoom_job(cam):
            # eoszoom: "1" = normal, "5" = 5x magnified center crop
            # try string first (most common), fall back to int
            cfg = cam.get_config()
            ez = cfg.get_child_by_name("eoszoom")
            val = "5" if want_zoom else "1"
            try:
                ez.set_value(val)
                cam.set_single_config("eoszoom", ez)
            except gp.GPhoto2Error:
                cfg = cam.get_config()
                ez = cfg.get_child_by_name("eoszoom")
                ez.set_value(int(val))
                cam.set_single_config("eoszoom", ez)
            time.sleep(0.3)

        def after():
            self._set_status("5x zoom" if want_zoom else "Ready")

        def run():
            done = self._cam.run(zoom_job)
            done.wait()
            self.root.after(0, after)

        threading.Thread(target=run, daemon=True).start()

    # ── flash ─────────────────────────────────────────────────────────────────

    def _toggle_flash(self):
        self.flash_on = not self.flash_on
        self._flash_img = ctk.CTkImage(flash_icon(self.flash_on), size=(32, 32))
        self._flash_btn.configure(
            image=self._flash_img,
            text_color=YELLOW if self.flash_on else TEXT_DIM,
        )
        self._set_status("Flash on…" if self.flash_on else "Flash off…")

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

    def _do_af(self):
        self._af_img = ctk.CTkImage(af_icon(True), size=(32, 32))
        self._af_btn.configure(image=self._af_img, text_color=BLUE)
        self._set_status("Focusing…")

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
            self._af_img = ctk.CTkImage(af_icon(False), size=(32, 32))
            self._af_btn.configure(image=self._af_img, text_color=TEXT_DIM)
            self._set_status("Ready")

        def run():
            done = self._cam.run(af_job)
            done.wait()
            self.root.after(0, after)

        threading.Thread(target=run, daemon=True).start()

    # ── capture ───────────────────────────────────────────────────────────────

    def _do_capture(self):
        self._animate_shutter()
        self._set_status("Capturing…")

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
            self.root.after(0, self._set_status, "Ready")

        threading.Thread(target=run, daemon=True).start()

    def _animate_shutter(self):
        p = ImageTk.PhotoImage(shutter_ring(64, pressed=True))
        self._ui_refs["sp"] = p
        self._shutter_cv.delete("all")
        self._shutter_cv.create_image(32, 32, image=p)
        self.root.after(120, self._reset_shutter)

    def _reset_shutter(self):
        p = ImageTk.PhotoImage(shutter_ring(64))
        self._ui_refs["shutter"] = p
        self._shutter_cv.delete("all")
        self._shutter_cv.create_image(32, 32, image=p)

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
                               fg_color="transparent", cursor="hand2")
            lbl.pack(side="left", padx=6, pady=6)
            lbl.bind("<Button-1>", lambda e, p=str(path): subprocess.Popen(["open", p]))
        except Exception as e:
            print(f"Thumb: {e}")


if __name__ == "__main__":
    import fcntl, sys
    lockfile = open("/tmp/scanner_app.lock", "w")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Scanner is already running.")
        sys.exit(0)
    app = ScannerApp()
    app.run()
