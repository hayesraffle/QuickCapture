#!/usr/bin/env python3
"""
process_scans.py  —  Flatbed scan processor.

  python3 process_scans.py [scan_folder]

Opens a browser. If scan_folder is given, auto-processes all unprocessed
JPEGs (newest first). You can also drag more scans into the browser window.
Crops are saved to  scan_folder/processed/  (or output/ if no folder given).
"""

import sys, json, io, threading, webbrowser, email, email.policy, socket, tempfile, os, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from PIL import Image, ImageOps

# ── Config ─────────────────────────────────────────────────────────────────────

SCAN_DIR     = None          # set via sys.argv[1] or left None for drag-drop only
BASE_DIR     = Path(__file__).parent
OUTPUT_DIR   = BASE_DIR / "output"       # default; overridden if SCAN_DIR is set
RESULTS_FILE = OUTPUT_DIR / "results.json"  # derived in main()
PORT         = 8765
THUMB_SIZE   = (400, 400)

# ── Detection + perspective correction (Apple Vision) ─────────────────────────

def find_and_extract_documents(pil_img):
    """Use Apple's VNDetectDocumentSegmentationRequest to find and extract documents."""
    import numpy as np
    import cv2
    import Vision
    import Quartz
    from Foundation import NSURL
    # Write to temp file for Vision framework
    tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
    try:
        pil_img.save(tmp.name, 'JPEG', quality=95)
        url = NSURL.fileURLWithPath_(tmp.name)
        ci_image = Quartz.CIImage.imageWithContentsOfURL_(url)
    finally:
        os.unlink(tmp.name)

    if ci_image is None:
        return []

    handler = Vision.VNImageRequestHandler.alloc().initWithCIImage_options_(ci_image, None)
    request = Vision.VNDetectDocumentSegmentationRequest.alloc().init()
    success, error = handler.performRequests_error_([request], None)
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

# ── State ──────────────────────────────────────────────────────────────────────

class AppState:
    def __init__(self):
        self._lock   = threading.Lock()
        self.results = []

    def load(self):
        if RESULTS_FILE.exists():
            try:
                self.results = json.loads(RESULTS_FILE.read_text())
                # normalise legacy entries that lack 'name'
                for r in self.results:
                    if "name" not in r:
                        r["name"] = Path(r["path"]).name
            except Exception:
                self.results = []
        heal_paths(self.results)
        return self

    def add(self, entries):
        with self._lock:
            self.results.extend(entries)
            self._persist()

    def persist(self):
        with self._lock:
            self._persist()

    def _persist(self):
        OUTPUT_DIR.mkdir(exist_ok=True)
        RESULTS_FILE.write_text(json.dumps(self.results, indent=2))

STATE = AppState()

# ── Processing ─────────────────────────────────────────────────────────────────

def _unique_path(directory, name):
    out = directory / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while out.exists():
        out = directory / f"{stem}_{n}{suffix}"
        n += 1
    return out

def process_upload(filename, file_bytes):
    """Detect documents in image, perspective-correct, and save. Returns list of result dicts."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    img = Image.open(io.BytesIO(file_bytes))
    img.load()
    img = ImageOps.exif_transpose(img)
    documents = find_and_extract_documents(img)
    if not documents:
        return []
    stem  = Path(filename).stem
    multi = len(documents) > 1
    entries = []
    for idx, doc in enumerate(documents):
        label = f"_crop{idx+1}" if multi else ""
        out   = _unique_path(OUTPUT_DIR, f"{stem}{label}.jpg")
        doc.save(out, "JPEG", quality=95)
        entries.append({
            "path":     str(out),
            "name":     out.name,
            "source":   filename,
            "rotation": 0,
            "deleted":  False,
        })
    return entries

def heal_paths(results):
    index = {p.name: p for p in OUTPUT_DIR.rglob("*.jpg")} if OUTPUT_DIR.exists() else {}
    fixed = 0
    for r in results:
        if r.get("deleted"):
            continue
        p = Path(r["path"])
        if not p.exists() and r.get("name", p.name) in index:
            found = index[r.get("name", p.name)]
            r["path"] = str(found)
            r["name"] = found.name
            fixed += 1
    if fixed:
        print(f"Healed {fixed} stale path(s)")

# ── Auto-process folder ───────────────────────────────────────────────────────

def auto_process_folder(scan_dir):
    """Process all unprocessed JPEGs in scan_dir, newest first."""
    scan_dir = Path(scan_dir)
    # Collect all top-level JPEGs (exclude processed/ subdir)
    all_jpgs = sorted(
        [f for f in scan_dir.glob("*.jpg") if f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    # Also check .jpeg
    all_jpgs += sorted(
        [f for f in scan_dir.glob("*.jpeg") if f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    # Find already-processed source filenames
    already = set()
    for r in STATE.results:
        src = r.get("source")
        if src:
            already.add(src)

    unprocessed = [f for f in all_jpgs if f.name not in already]
    if not unprocessed:
        print("No new images to process.")
        return

    print(f"Auto-processing {len(unprocessed)} new image(s)...")
    for img_path in unprocessed:
        print(f"  {img_path.name}")
        file_bytes = img_path.read_bytes()
        entries = process_upload(img_path.name, file_bytes)
        if entries:
            STATE.add(entries)
        else:
            print(f"    (no photos detected)")
    print("Done.")

# ── Port check ─────────────────────────────────────────────────────────────────

def port_in_use(port):
    """Check if a TCP port is already bound on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True

# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Scan Processor</title>
<style>
*, *::before, *::after { box-sizing: border-box; }
body {
  font-family: sans-serif; background: #141414; color: #eee;
  margin: 0; padding: 0; min-height: 100vh;
}

/* ── Drop zone ── */
#dropzone {
  border: 2px dashed #383838; border-radius: 14px;
  margin: 20px; padding: 64px 20px;
  text-align: center; cursor: pointer;
  transition: border-color 0.2s, background 0.2s, padding 0.3s;
}
#dropzone.drag-over  { border-color: #4af; background: rgba(68,170,255,0.06); }
#dropzone.compact    { padding: 16px 20px; }
#dropzone h2         { margin: 0 0 6px; font-size: 22px; color: #666; font-weight: 400; }
#dropzone.compact h2 { font-size: 14px; color: #444; }
#dropzone p          { margin: 0; color: #444; font-size: 13px; }
#dropzone.compact p  { display: none; }

/* ── Grid ── */
#grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 10px; padding: 0 20px 100px;
}

/* ── Cards ── */
.card {
  position: relative; background: #242424; border-radius: 8px;
  padding: 8px; user-select: none; transition: background 0.15s;
}
.card:hover { background: #2e2e2e; }
.img-box {
  width: 100%; aspect-ratio: 1; display: flex;
  align-items: center; justify-content: center;
  background: #111; border-radius: 4px; overflow: hidden; cursor: pointer;
}
.img-box img {
  max-width: 100%; max-height: 100%; object-fit: contain; display: block;
}
.label {
  font-size: 11px; color: #666; margin-top: 5px; text-align: center;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* ── Skeleton ── */
.card.skeleton .img-box {
  animation: shimmer 1.2s ease-in-out infinite;
}
@keyframes shimmer {
  0%,100% { background: #1e1e1e; }
  50%      { background: #282828; }
}

/* ── Rotate ── */
.rot-btn {
  display: none; position: absolute; top: 6px; left: 6px;
  background: rgba(60,60,60,0.9); color: #fff; border: none;
  border-radius: 50%; width: 22px; height: 22px;
  font-size: 13px; cursor: pointer; line-height: 22px; padding: 0; z-index: 2;
}
.card:hover .rot-btn { display: block; }
.rot-btn:hover { background: rgba(90,90,90,0.95); }

/* ── Delete ── */
.del-btn {
  display: none; position: absolute; top: 6px; right: 6px;
  background: rgba(160,30,30,0.9); color: #fff; border: none;
  border-radius: 50%; width: 22px; height: 22px;
  font-size: 12px; cursor: pointer; line-height: 22px; padding: 0; z-index: 2;
}
.card:hover .del-btn { display: block; }
.del-confirm {
  display: none; position: absolute; top: 32px; right: 6px;
  background: #900; color: #fff; border-radius: 6px;
  padding: 5px 10px; font-size: 12px; white-space: nowrap;
  cursor: pointer; z-index: 3; box-shadow: 0 2px 8px rgba(0,0,0,0.6);
}
.del-confirm:hover { background: #c00; }

/* ── Status ── */
#status {
  position: fixed; bottom: 14px; right: 18px;
  background: #2a2a2a; border: 1px solid #444;
  padding: 6px 16px; border-radius: 20px; font-size: 13px;
  transition: background 0.25s;
}
#status.saving { background: #1a5c30; border-color: #2a8a50; }
#status.saved  { background: #164426; border-color: #1d6034; }
#reset-btn {
  display: none; position: fixed; bottom: 14px; left: 18px;
  background: none; border: 1px solid #333; color: #555;
  padding: 6px 14px; border-radius: 20px; font-size: 13px;
  cursor: pointer; transition: border-color 0.2s, color 0.2s;
}
#reset-btn:hover { border-color: #900; color: #c44; }
body.has-photos #reset-btn { display: block; }
#quit-btn {
  position: fixed; bottom: 14px; left: 50%; transform: translateX(-50%);
  background: none; border: 1px solid #333; color: #555;
  padding: 6px 14px; border-radius: 20px; font-size: 13px;
  cursor: pointer; transition: border-color 0.2s, color 0.2s;
}
#quit-btn:hover { border-color: #888; color: #aaa; }

/* ── Lightbox ── */
#lightbox {
  display: none; position: fixed; inset: 0; z-index: 100;
  background: rgba(0,0,0,0.88); align-items: center; justify-content: center;
  cursor: zoom-out;
}
#lightbox.open { display: flex; }
#lightbox img {
  max-width: 92vw; max-height: 92vh; object-fit: contain; border-radius: 6px;
}
</style>
</head>
<body>

<div id="dropzone">
  <h2>Drop scans here</h2>
  <p>JPG &middot; PNG &middot; TIFF &nbsp;&mdash;&nbsp; splits multi-photo scans automatically</p>
</div>
<button id="reset-btn" onclick="resetAll()">Start over</button>
<button id="quit-btn" onclick="doQuit()">Quit</button>

<div id="grid"></div>
<div id="status">ready</div>
<div id="lightbox" onclick="closeLightbox()"><img></div>

<script>
const STATE = /*INJECT_STATE*/null/*END*/;
const results = STATE.results;
const dz = document.getElementById('dropzone');
let saveTimer = null, confirmTimer = null;

// ── Init ────────────────────────────────────────────────────────────────────
results.forEach((r, i) => { if (!r.deleted) addCard(i, r); });
if (results.length > 0) compact();

// ── Drop zone ───────────────────────────────────────────────────────────────

dz.addEventListener('click', () => {
  const inp = document.createElement('input');
  inp.type = 'file'; inp.multiple = true;
  inp.accept = 'image/*';
  inp.onchange = e => handleFiles([...e.target.files]);
  inp.click();
});

dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag-over'); });
dz.addEventListener('dragleave', e => { if (!dz.contains(e.relatedTarget)) dz.classList.remove('drag-over'); });
dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('drag-over'); handleFiles([...e.dataTransfer.files]); });

document.addEventListener('dragover', e => e.preventDefault());
document.addEventListener('drop', e => {
  e.preventDefault();
  const files = [...e.dataTransfer.files].filter(f => /\.(jpe?g|png|tiff?)$/i.test(f.name));
  if (files.length) handleFiles(files);
});

async function handleFiles(files) {
  compact();
  for (const file of files) {
    const skelId = addSkeleton(file.name);
    try {
      const fd = new FormData();
      fd.append('file', file);
      const resp = await fetch('/upload', { method: 'POST', body: fd });
      const crops = await resp.json();
      removeSkeleton(skelId);
      crops.forEach(entry => {
        const i = results.length;
        results.push(entry);
        addCard(i, entry);
      });
      if (crops.length === 0) showNote(`No photos found in ${file.name}`);
    } catch (err) {
      removeSkeleton(skelId);
      showNote(`Error processing ${file.name}`);
    }
  }
}

function compact() {
  dz.classList.add('compact');
  document.body.classList.add('has-photos');
}

function resetAll() {
  if (!confirm('Delete all output photos and start fresh?')) return;
  fetch('/reset', { method: 'POST' }).then(() => {
    results.length = 0;
    document.getElementById('grid').innerHTML = '';
    dz.classList.remove('compact');
    document.body.classList.remove('has-photos');
  });
}

// ── Cards ────────────────────────────────────────────────────────────────────
let skelCounter = 0;
function addSkeleton(name) {
  const id = 'skel-' + (skelCounter++);
  const d = document.createElement('div');
  d.className = 'card skeleton'; d.id = id;
  d.innerHTML = '<div class="img-box"></div><div class="label">' + escHtml(name) + '</div>';
  document.getElementById('grid').appendChild(d);
  return id;
}
function removeSkeleton(id) { const el = document.getElementById(id); if (el) el.remove(); }

function addCard(i, r) {
  const card = document.createElement('div');
  card.className = 'card'; card.id = 'card-' + i;
  card.innerHTML =
    '<div class="img-box" onclick="openLightbox(' + i + ')">' +
      '<img src="/thumb/' + i + '?rot=' + (r.rotation||0) + '" loading="lazy" alt="' + escHtml(r.name) + '">' +
    '</div>' +
    '<div class="label">' + escHtml(r.name) + '</div>' +
    '<button class="rot-btn" onclick="event.stopPropagation();rotate(' + i + ')" title="Rotate">&#x21ba;</button>' +
    '<button class="del-btn" onclick="showDel(event,' + i + ')">&#x2715;</button>' +
    '<div class="del-confirm" id="dc-' + i + '" onclick="doDelete(' + i + ')">Delete?</div>';
  document.getElementById('grid').appendChild(card);
}

function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); }

// ── Lightbox ─────────────────────────────────────────────────────────────────
function openLightbox(i) {
  const lb = document.getElementById('lightbox');
  const img = lb.querySelector('img');
  img.src = '/thumb/' + i + '?rot=' + (results[i].rotation||0) + '&full=1&t=' + Date.now();
  lb.classList.add('open');
}
function closeLightbox() {
  document.getElementById('lightbox').classList.remove('open');
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeLightbox();
});

// ── Rotate ───────────────────────────────────────────────────────────────────
function rotate(i) {
  results[i].rotation = ((results[i].rotation - 90) % 360 + 360) % 360;
  const img = document.querySelector('#card-' + i + ' img');
  if (img) img.style.transform = 'rotate(' + results[i].rotation + 'deg)';
  setStatus('unsaved\u2026', '');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveAll, 1500);
}

function refreshThumb(i) {
  const img = document.querySelector('#card-' + i + ' img');
  if (!img) return;
  const fresh = new Image();
  fresh.onload = () => { img.src = fresh.src; img.style.transform = ''; };
  fresh.src = '/thumb/' + i + '?rot=0&t=' + Date.now();
}

// ── Save ─────────────────────────────────────────────────────────────────────
function saveAll() {
  const dirty = {};
  results.forEach((r, i) => { if ((r.rotation||0) % 360 !== 0) dirty[i] = r.rotation; });
  if (!Object.keys(dirty).length) return;
  setStatus('Saving\u2026', 'saving');
  fetch('/save', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(dirty)
  }).then(r => r.text()).then(() => {
    setStatus('Saved', 'saved');
    setTimeout(() => setStatus('ready', ''), 2000);
    Object.keys(dirty).forEach(i => { results[i].rotation = 0; refreshThumb(+i); });
  });
}

// ── Delete ───────────────────────────────────────────────────────────────────
function showDel(e, i) {
  e.stopPropagation();
  document.querySelectorAll('.del-confirm').forEach(el => el.style.display = 'none');
  const el = document.getElementById('dc-' + i);
  el.style.display = 'block';
  clearTimeout(confirmTimer);
  confirmTimer = setTimeout(() => el.style.display = 'none', 3000);
}
function doDelete(i) {
  document.getElementById('dc-' + i).style.display = 'none';
  fetch('/delete/' + i, { method: 'POST' }).then(() => {
    results[i].deleted = true;
    const card = document.getElementById('card-' + i);
    if (card) { card.style.opacity = '0.12'; card.style.pointerEvents = 'none'; }
  });
}
document.addEventListener('click', () =>
  document.querySelectorAll('.del-confirm').forEach(el => el.style.display = 'none')
);

// ── Util ─────────────────────────────────────────────────────────────────────
function setStatus(t, c) { const s = document.getElementById('status'); s.textContent = t; s.className = c; }
function showNote(msg) { setStatus(msg, ''); setTimeout(() => setStatus('ready', ''), 4000); }
new EventSource('/heartbeat');
function doQuit() {
  fetch('/quit', { method: 'POST' }).catch(() => {});
  document.body.innerHTML = '<p style="text-align:center;margin-top:40vh;color:#666">Server stopped.</p>';
}
</script>
</body>
</html>"""

# ── Multipart parser (no cgi module needed) ────────────────────────────────────

def parse_multipart(headers, body):
    """Returns list of (filename, bytes) for uploaded files."""
    ct = headers.get("Content-Type", "")
    raw = ("Content-Type: " + ct + "\r\n\r\n").encode() + body
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    results = []
    for part in msg.get_payload():
        cd = part.get("Content-Disposition", "")
        if "filename" not in cd:
            continue
        fname = part.get_filename("")
        data  = part.get_payload(decode=True)
        if fname and data:
            results.append((fname, data))
    return results

# ── HTTP handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            page_state = {
                "results": [
                    {"rotation": r.get("rotation", 0),
                     "name":     r.get("name", Path(r["path"]).name),
                     "deleted":  r.get("deleted", False)}
                    for r in STATE.results
                ],
            }
            html  = HTML.replace("/*INJECT_STATE*/null/*END*/", json.dumps(page_state))
            data  = html.encode()
            self._respond(200, "text/html; charset=utf-8", data)

        elif parsed.path == "/heartbeat":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.server.sse_clients.add(self)
            try:
                while True:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    time.sleep(15)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.server.sse_clients.discard(self)

        elif parsed.path.startswith("/thumb/"):
            idx = int(parsed.path.split("/")[-1])
            if idx >= len(STATE.results):
                self.send_error(404); return
            r = STATE.results[idx]
            if r.get("deleted") or not Path(r["path"]).exists():
                self.send_error(404); return
            qs   = parse_qs(parsed.query)
            rot  = int(qs.get("rot", ["0"])[0])
            full = qs.get("full", ["0"])[0] == "1"
            img  = Image.open(r["path"])
            if rot:
                img = img.rotate(-rot, expand=True)
            if not full:
                img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            else:
                img.thumbnail((1800, 1800), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85 if full else 82)
            self._respond(200, "image/jpeg", buf.getvalue())

        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        if self.path == "/upload":
            parts = parse_multipart(self.headers, body)
            all_entries = []
            for fname, data in parts:
                entries = process_upload(fname, data)
                STATE.add(entries)
                # Return index alongside each entry so JS can reference it
                for e in entries:
                    idx = STATE.results.index(e)
                    all_entries.append({
                        "idx":      idx,
                        "name":     e["name"],
                        "rotation": 0,
                    })
            resp = json.dumps(all_entries).encode()
            self._respond(200, "application/json", resp)

        elif self.path == "/save":
            dirty = json.loads(body)  # {str(idx): degrees}
            for idx_str, deg in dirty.items():
                i   = int(idx_str)
                deg = int(deg)
                if deg % 360 == 0 or i >= len(STATE.results):
                    continue
                r = STATE.results[i]
                img = Image.open(r["path"])
                img = img.rotate(-deg, expand=True)
                img.save(r["path"], "JPEG", quality=95)
                r["rotation"] = 0
            STATE.persist()
            self._respond(200, "text/plain", b"ok")

        elif self.path == "/reset":
            with STATE._lock:
                for r in STATE.results:
                    Path(r["path"]).unlink(missing_ok=True)
                STATE.results.clear()
                STATE._persist()
            self._respond(200, "text/plain", b"ok")

        elif self.path.startswith("/delete/"):
            idx = int(self.path.split("/")[-1])
            if idx < len(STATE.results):
                r = STATE.results[idx]
                Path(r["path"]).unlink(missing_ok=True)
                r["deleted"] = True
                STATE.persist()
            self._respond(200, "text/plain", b"ok")

        elif self.path == "/quit":
            self._respond(200, "text/plain", b"bye")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        else:
            self.send_error(404)

    def _respond(self, code, content_type, data):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ── Entry point ────────────────────────────────────────────────────────────────

def main(scan_dir=None):
    global SCAN_DIR, OUTPUT_DIR, RESULTS_FILE

    if scan_dir:
        SCAN_DIR = Path(scan_dir)
        OUTPUT_DIR = SCAN_DIR / "processed"
        RESULTS_FILE = OUTPUT_DIR / "results.json"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE.load()

    # If port is already in use, just open browser to existing instance
    if port_in_use(PORT):
        print(f"Port {PORT} already in use — opening browser to existing instance.")
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return

    # Auto-process folder if given
    if SCAN_DIR:
        auto_process_folder(SCAN_DIR)

    print(f"Scan processor  →  http://127.0.0.1:{PORT}/")
    print(f"Output folder   →  {OUTPUT_DIR}")
    print("Ctrl-C to quit\n")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.sse_clients = set()

    def watchdog():
        # Give the browser time to connect initially
        time.sleep(15)
        while True:
            time.sleep(5)
            if not server.sse_clients:
                print("\nNo browser connected — shutting down.")
                server.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("Stopped.")

if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else None
    main(folder)
