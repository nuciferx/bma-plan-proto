"""
server.py — PDF Scale Backend v4
รัน: python server.py
เปิด: http://localhost:8000
"""
import io, math, re, json, tempfile, os, time
from collections import defaultdict
from pathlib import Path
from typing import Optional
from uuid import uuid4

import ctypes

import fitz
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles

try:
    import xlsxwriter
    HAS_XLSX = True
except ImportError:
    HAS_XLSX = False

from export.semantic_metadata import (
    AREA_SEMANTIC_TAGS,
    SEMANTIC_PROFILE_MAP, SEMANTIC_CATEGORY_MAP, SEMANTIC_REPORT_TARGET_MAP,
    SEMANTIC_LAW_BASIS_MAP, SEMANTIC_COUNTING_RULE_MAP,
    _derive_measurement_meta, _get_meta,
)
from export.xlsx_helpers import (
    _hex_to_rgb, _poly_area_pt2, _line_points, _line_length_pt,
    _nearest_on_segment, _object_points_for_ref_report, _distance_to_ref, _m2_to_rwu,
)

app = FastAPI()

_BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _BASE_DIR / "static"
print(f"[static] serving from: {_STATIC_DIR}")
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

CASES: dict = {}
PT_PER_MM = 72 / 25.4
SCALE_RE  = re.compile(r'1\s*[:/]\s*(\d{2,5})', re.IGNORECASE)
SNAP_GRID = 8   # PDF pt — fine dedupe for CAD-like snap points
MAX_SNAP_POINTS = 12000
MAX_SNAP_LINES = 12000
MAX_UPLOAD_BYTES = 80 * 1024 * 1024
CASE_TTL_SECONDS = 2 * 60 * 60
MAX_CASES = 12
MIN_RENDER_SCALE = 0.1
MAX_RENDER_SCALE = 3.0
MAX_IMAGE_CACHE_ENTRIES = 24
MAX_IMAGE_CACHE_BYTES = 128 * 1024 * 1024
MAX_ANALYSE_CACHE_ENTRIES = 24
MAX_ANALYSE_CACHE_BYTES = 64 * 1024 * 1024


# ══════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════
def detect_scale(page: fitz.Page) -> Optional[dict]:
    spans = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0: continue
        for line in b["lines"]:
            for sp in line["spans"]:
                m = SCALE_RE.search(sp["text"])
                if m:
                    spans.append({"N": int(m.group(1)), "size": sp["size"]})
    if not spans: return None
    best = max(spans, key=lambda s: s["size"])
    N = best["N"]
    return {"N": N, "label": f"1:{N}",
            "pts_per_m": round((1000 / N) * PT_PER_MM, 4),
            "source": "auto", "verified": False}


def _cleanup_file(path: Optional[str]):
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _make_case(doc: fitz.Document, path: str):
    _prune_cases()
    case_id = uuid4().hex
    CASES[case_id] = {
        "doc": doc,
        "path": path,
        "page_cache": {},
        "image_cache": {},
        "page_tags": {},
        "project_info": {},
        "created_at": time.time(),
        "last_access": time.time(),
    }
    return case_id


def _close_case(case: dict):
    doc = case.get("doc")
    if doc:
        try:
            doc.close()
        except Exception:
            pass
    _cleanup_file(case.get("path"))


def _prune_cases():
    now = time.time()
    stale = [cid for cid, case in CASES.items() if now - case.get("last_access", case.get("created_at", now)) > CASE_TTL_SECONDS]
    for cid in stale:
        _close_case(CASES.pop(cid, {}))
    if len(CASES) < MAX_CASES:
        return
    ordered = sorted(CASES.items(), key=lambda kv: kv[1].get("last_access", 0))
    for cid, case in ordered[: max(0, len(CASES) - MAX_CASES + 1)]:
        _close_case(case)
        CASES.pop(cid, None)


def _get_case(case_id: str):
    if not case_id:
        return None
    _prune_cases()
    case = CASES.get(case_id)
    if case:
        case["last_access"] = time.time()
    return case


def _require_page(doc: fitz.Document, n: int):
    if n < 1 or n > len(doc):
        return None
    return doc[n - 1]


def _normalize_render_scale(scale: float) -> Optional[float]:
    if not math.isfinite(scale):
        return None
    if scale < MIN_RENDER_SCALE or scale > MAX_RENDER_SCALE:
        return None
    return round(scale, 3)


def _cache_size_bytes(cache: dict) -> int:
    return sum(len(v) for v in cache.values() if isinstance(v, (bytes, bytearray)))


def _cache_get(cache: dict, key):
    if key not in cache:
        return None
    value = cache.pop(key)
    cache[key] = value
    return value


def _cache_put(cache: dict, key, value: bytes, max_entries: int, max_bytes: int):
    if key in cache:
        cache.pop(key)
    cache[key] = value
    while cache and (len(cache) > max_entries or _cache_size_bytes(cache) > max_bytes):
        oldest = next(iter(cache))
        cache.pop(oldest, None)
    return value


def _pt_key(x: float, y: float, grid: int = SNAP_GRID):
    return (int(x / grid), int(y / grid))


def _line_key(x0: float, y0: float, x1: float, y1: float):
    a = (round(x0, 1), round(y0, 1))
    b = (round(x1, 1), round(y1, 1))
    return tuple(sorted((a, b)))


def _add_point(grid: dict, x: float, y: float):
    k = _pt_key(x, y)
    if k not in grid:
        grid[k] = (round(x, 1), round(y, 1))


def _add_line(lines_out: list, line_seen: set, x0: float, y0: float, x1: float, y1: float, max_lines: int):
    if len(lines_out) >= max_lines:
        return False
    if math.hypot(x1 - x0, y1 - y0) < 0.5:
        return False
    k = _line_key(x0, y0, x1, y1)
    if k in line_seen:
        return False
    line_seen.add(k)
    lines_out.append([round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)])
    return True


def _pack_snap_result(ep_grid, mp_grid, ct_grid, lines_out, max_snaps, max_lines, meta):
    ep = list(ep_grid.values())
    mp = list(mp_grid.values())
    ct = list(ct_grid.values())
    snaps  = [{"x": v[0], "y": v[1], "t": "ep"} for v in ep[: max_snaps // 2]]
    snaps += [{"x": v[0], "y": v[1], "t": "mp"} for v in mp[: max_snaps // 4]]
    snaps += [{"x": v[0], "y": v[1], "t": "ct"} for v in ct[: max_snaps // 4]]
    meta.update({
        "raw_ep": len(ep),
        "raw_mp": len(mp),
        "raw_ct": len(ct),
        "raw_lines": len(lines_out),
        "returned_snaps": len(snaps),
        "returned_lines": len(lines_out[:max_lines]),
        "snap_grid_pt": SNAP_GRID,
        "max_snaps": max_snaps,
        "max_lines": max_lines,
    })
    return snaps, lines_out[:max_lines], meta


def extract_snaps_typed(drawings: list, max_snaps=MAX_SNAP_POINTS, max_lines=MAX_SNAP_LINES):
    """Returns (snaps, lines).
    snaps: [{x,y,t}]  t = ep|mp|ct
    lines: [[x0,y0,x1,y1], ...]  for NL/IX client-side snap
    """
    ep_grid, mp_grid, ct_grid = {}, {}, {}
    lines_out = []
    line_seen = set()
    meta = {"path_objects": len(drawings), "segments": 0, "engine": "pymupdf"}

    for d in drawings:
        for item in d["items"]:
            op = item[0]
            meta["segments"] += 1
            if op == "l":
                p1, p2 = item[1], item[2]
                # EP – endpoints
                for pt in [p1, p2]:
                    _add_point(ep_grid, pt.x, pt.y)
                # MP – midpoint
                mx, my = (p1.x + p2.x) / 2, (p1.y + p2.y) / 2
                _add_point(mp_grid, mx, my)
                # lines for NL/IX
                _add_line(lines_out, line_seen, p1.x, p1.y, p2.x, p2.y, max_lines)
            elif op == "re":
                r = item[1]
                # EP – corners
                for pt in [(r.x0,r.y0),(r.x1,r.y0),(r.x1,r.y1),(r.x0,r.y1)]:
                    _add_point(ep_grid, pt[0], pt[1])
                # CT – rectangle center
                cx2, cy2 = (r.x0+r.x1)/2, (r.y0+r.y1)/2
                _add_point(ct_grid, cx2, cy2)
                # edges as lines
                for seg in [(r.x0,r.y0,r.x1,r.y0),(r.x1,r.y0,r.x1,r.y1),
                            (r.x1,r.y1,r.x0,r.y1),(r.x0,r.y1,r.x0,r.y0)]:
                    _add_line(lines_out, line_seen, *seg, max_lines)

    return _pack_snap_result(ep_grid, mp_grid, ct_grid, lines_out, max_snaps, max_lines, meta)


def extract_snaps_pdfium(pdf_path: str, page_index: int, max_snaps=MAX_SNAP_POINTS, max_lines=MAX_SNAP_LINES):
    """
    Extract snap points และ lines จาก PDF โดยตรงผ่าน PDFium engine
    เร็วกว่า PyMuPDF 2.6x และแม่นกว่าเพราะอ่าน path object โดยตรง

    Returns:
        snaps: list of {x, y, t}  t = "ep" | "mp" | "ct"
        lines: list of [x0, y0, x1, y1]
    """
    doc = pdfium.PdfDocument(pdf_path)
    page = doc[page_index]
    page_h = page.get_height()
    rp = page.raw

    ep_grid = {}
    mp_grid = {}
    ct_grid = {}
    lines_out = []
    line_seen = set()
    meta = {"engine": "pdfium", "path_objects": 0, "segments": 0, "moveto": 0, "lineto": 0, "bezierto": 0}

    n_obj = pdfium_c.FPDFPage_CountObjects(rp)

    for i in range(n_obj):
        obj = pdfium_c.FPDFPage_GetObject(rp, i)
        obj_type = pdfium_c.FPDFPageObj_GetType(obj)

        if obj_type != pdfium_c.FPDF_PAGEOBJ_PATH:
            continue
        meta["path_objects"] += 1

        n_seg = pdfium_c.FPDFPath_CountSegments(obj)
        pts = []
        prev = None
        start = None

        for j in range(n_seg):
            seg = pdfium_c.FPDFPath_GetPathSegment(obj, j)
            seg_type = pdfium_c.FPDFPathSegment_GetType(seg)
            x = ctypes.c_float()
            y = ctypes.c_float()
            pdfium_c.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y))
            cx = round(x.value, 1)
            cy = round(page_h - y.value, 1)
            pt = (cx, cy)
            pts.append(pt)
            meta["segments"] += 1
            if seg_type == pdfium_c.FPDF_SEGMENT_MOVETO:
                meta["moveto"] += 1
                prev = pt
                start = pt
                _add_point(ep_grid, cx, cy)
                continue
            if seg_type == pdfium_c.FPDF_SEGMENT_LINETO:
                meta["lineto"] += 1
            elif seg_type == pdfium_c.FPDF_SEGMENT_BEZIERTO:
                meta["bezierto"] += 1
            _add_point(ep_grid, cx, cy)
            if prev is not None and seg_type in (pdfium_c.FPDF_SEGMENT_LINETO, pdfium_c.FPDF_SEGMENT_BEZIERTO):
                mx, my = (prev[0] + cx) / 2, (prev[1] + cy) / 2
                _add_point(mp_grid, mx, my)
                _add_line(lines_out, line_seen, prev[0], prev[1], cx, cy, max_lines)
            is_closed = bool(pdfium_c.FPDFPathSegment_GetClose(seg))
            if is_closed and start is not None:
                _add_line(lines_out, line_seen, cx, cy, start[0], start[1], max_lines)
            prev = start if is_closed else pt

        # CT — center of bounding box ของแต่ละ path object
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            cx2 = (min(xs) + max(xs)) / 2
            cy2 = (min(ys) + max(ys)) / 2
            _add_point(ct_grid, cx2, cy2)

    return _pack_snap_result(ep_grid, mp_grid, ct_grid, lines_out, max_snaps, max_lines, meta)


def _rotate_snaps(snaps, rot, W, H):
    out = []
    for s in snaps:
        x, y = s["x"], s["y"]
        if rot == 90:    x, y = y, W - x
        elif rot == 180: x, y = W - x, H - y
        elif rot == 270: x, y = H - y, x
        out.append({"x": round(x,1), "y": round(y,1), "t": s["t"]})
    return out


def _rotate_lines(lines, rot, W, H):
    out = []
    for l in lines:
        x0,y0,x1,y1 = l
        if rot == 90:
            x0,y0 = y0,W-x0;  x1,y1 = y1,W-x1
        elif rot == 180:
            x0,y0 = W-x0,H-y0; x1,y1 = W-x1,H-y1
        elif rot == 270:
            x0,y0 = H-y0,x0;  x1,y1 = H-y1,x1
        out.append([round(x0,1),round(y0,1),round(x1,1),round(y1,1)])
    return out


# ══════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    total = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise ValueError("file too large")
            tmp.write(chunk)
        tmp.close()
        if total == 0:
            raise ValueError("empty upload")
        try:
            doc = fitz.open(tmp.name)
        except Exception:
            raise ValueError("invalid pdf")
        if doc.is_encrypted:
            doc.close()
            raise ValueError("encrypted pdf")
        case_id = _make_case(doc, tmp.name)
        return {"pages": len(doc), "name": file.filename, "case_id": case_id,
                "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024)}
    except ValueError as exc:
        tmp.close()
        _cleanup_file(tmp.name)
        msg = str(exc)
        if msg == "file too large":
            return JSONResponse({"error": "file too large"}, 413)
        if msg == "empty upload":
            return JSONResponse({"error": "empty file"}, 400)
        if msg == "encrypted pdf":
            return JSONResponse({"error": "encrypted pdf not supported"}, 400)
        return JSONResponse({"error": "invalid pdf"}, 400)
    except Exception:
        tmp.close()
        _cleanup_file(tmp.name)
        return JSONResponse({"error": "upload failed"}, 500)


@app.get("/sample-pdf")
def sample_pdf():
    path = os.path.join(os.path.dirname(__file__), "test_plan_A1.pdf")
    if not os.path.exists(path):
        return JSONResponse({"error": "sample pdf not found"}, 404)
    return FileResponse(path, media_type="application/pdf", filename="test_plan_A1.pdf")


@app.get("/page/{n}")
def get_page(n: int, case_id: str, scale: float = 1.5, rot: int = 0):
    _t0 = time.perf_counter()
    case = _get_case(case_id)
    _t1 = time.perf_counter()
    if not case: return JSONResponse({"error":"invalid case"}, 400)
    render_scale = _normalize_render_scale(scale)
    if render_scale is None:
        return JSONResponse({
            "error": f"scale must be between {MIN_RENDER_SCALE} and {MAX_RENDER_SCALE}"
        }, 400)
    doc = case.get("doc")
    page = _require_page(doc, n)
    if page is None: return JSONResponse({"error":"page out of range"}, 404)
    img_cache = case.setdefault("image_cache", {})
    _jpg_quality = 88
    key = ("page", n, render_scale, rot, "jpeg", _jpg_quality)
    cached = _cache_get(img_cache, key)
    _t2 = time.perf_counter()
    if cached is None:
        mat = fitz.Matrix(render_scale, render_scale).prerotate(rot)
        _t3 = time.perf_counter()
        pix = page.get_pixmap(matrix=mat)
        _t4 = time.perf_counter()
        cached = _cache_put(
            img_cache, key, pix.tobytes("jpeg", jpg_quality=_jpg_quality),
            MAX_IMAGE_CACHE_ENTRIES, MAX_IMAGE_CACHE_BYTES
        )
        _t5 = time.perf_counter()
        print(f"[BMA_PAGE_RENDER_PERF] page={n} scale={render_scale} rot={rot} "
              f"session={(_t1-_t0)*1000:.1f}ms cache={(_t2-_t1)*1000:.1f}ms "
              f"get_pixmap={(_t4-_t3)*1000:.1f}ms encode={(_t5-_t4)*1000:.1f}ms "
              f"bytes={len(cached)} total={(_t5-_t0)*1000:.1f}ms MISS", flush=True)
    else:
        _tf = time.perf_counter()
        print(f"[BMA_PAGE_RENDER_PERF] page={n} scale={render_scale} rot={rot} "
              f"session={(_t1-_t0)*1000:.1f}ms total={(_tf-_t0)*1000:.1f}ms "
              f"bytes={len(cached)} HIT", flush=True)
    return Response(cached, media_type="image/jpeg")


@app.get("/thumb/{n}")
def get_thumb(n: int, case_id: str, rot: int = 0):
    _t0 = time.perf_counter()
    case = _get_case(case_id)
    _t1 = time.perf_counter()
    if not case: return JSONResponse({"error":"invalid case"}, 400)
    doc = case.get("doc")
    page = _require_page(doc, n)
    if page is None: return JSONResponse({"error":"page out of range"}, 404)
    img_cache = case.setdefault("image_cache", {})
    _jpg_quality = 70
    key = ("thumb", n, rot, "jpeg", _jpg_quality)
    cached = _cache_get(img_cache, key)
    _t2 = time.perf_counter()
    if cached is None:
        mat = fitz.Matrix(0.18, 0.18).prerotate(rot)
        _t3 = time.perf_counter()
        pix = page.get_pixmap(matrix=mat)
        _t4 = time.perf_counter()
        cached = _cache_put(
            img_cache, key, pix.tobytes("jpeg", jpg_quality=_jpg_quality),
            MAX_IMAGE_CACHE_ENTRIES, MAX_IMAGE_CACHE_BYTES
        )
        _t5 = time.perf_counter()
        print(f"[BMA_THUMB_RENDER_PERF] thumb={n} rot={rot} "
              f"session={(_t1-_t0)*1000:.1f}ms cache={(_t2-_t1)*1000:.1f}ms "
              f"get_pixmap={(_t4-_t3)*1000:.1f}ms encode={(_t5-_t4)*1000:.1f}ms "
              f"bytes={len(cached)} total={(_t5-_t0)*1000:.1f}ms MISS", flush=True)
    else:
        _tf = time.perf_counter()
        print(f"[BMA_THUMB_RENDER_PERF] thumb={n} rot={rot} "
              f"session={(_t1-_t0)*1000:.1f}ms total={(_tf-_t0)*1000:.1f}ms "
              f"bytes={len(cached)} HIT", flush=True)
    return StreamingResponse(io.BytesIO(cached), media_type="image/jpeg")

@app.get("/thumb-md/{n}")
def get_thumb_md(n: int, case_id: str, rot: int = 0):
    """Medium thumbnail for setup grid — 0.4x scale, quality 82"""
    _t0 = time.perf_counter()
    case = _get_case(case_id)
    _t1 = time.perf_counter()
    if not case: return JSONResponse({"error":"invalid case"}, 400)
    doc = case.get("doc")
    page = _require_page(doc, n)
    if page is None: return JSONResponse({"error":"page out of range"}, 404)
    img_cache = case.setdefault("image_cache", {})
    _jpg_quality = 82
    key = ("thumb-md", n, rot, "jpeg", _jpg_quality)
    cached = _cache_get(img_cache, key)
    _t2 = time.perf_counter()
    if cached is None:
        mat = fitz.Matrix(0.4, 0.4).prerotate(rot)
        _t3 = time.perf_counter()
        pix = page.get_pixmap(matrix=mat)
        _t4 = time.perf_counter()
        cached = _cache_put(
            img_cache, key, pix.tobytes("jpeg", jpg_quality=_jpg_quality),
            MAX_IMAGE_CACHE_ENTRIES, MAX_IMAGE_CACHE_BYTES
        )
        _t5 = time.perf_counter()
        print(f"[BMA_THUMB_RENDER_PERF] thumb-md={n} rot={rot} "
              f"session={(_t1-_t0)*1000:.1f}ms cache={(_t2-_t1)*1000:.1f}ms "
              f"get_pixmap={(_t4-_t3)*1000:.1f}ms encode={(_t5-_t4)*1000:.1f}ms "
              f"bytes={len(cached)} total={(_t5-_t0)*1000:.1f}ms MISS", flush=True)
    else:
        _tf = time.perf_counter()
        print(f"[BMA_THUMB_RENDER_PERF] thumb-md={n} rot={rot} "
              f"session={(_t1-_t0)*1000:.1f}ms total={(_tf-_t0)*1000:.1f}ms "
              f"bytes={len(cached)} HIT", flush=True)
    return StreamingResponse(io.BytesIO(cached), media_type="image/jpeg")


@app.get("/analyse/{n}")
def analyse(n: int, case_id: str, rot: int = 0):
    case = _get_case(case_id)
    if not case: return JSONResponse({"error":"invalid case"}, 400)
    doc = case.get("doc")
    page = _require_page(doc, n)
    if page is None: return JSONResponse({"error":"page out of range"}, 404)
    cache = case.setdefault("page_cache", {})
    key   = (n, rot)
    cached = _cache_get(cache, key)
    if cached is not None:
        return Response(cached, media_type="application/json")

    orig_W = page.rect.width
    orig_H = page.rect.height

    scale             = detect_scale(page)
    snaps, lines, snap_meta = extract_snaps_pdfium(case["path"], n - 1)
    # fallback to PyMuPDF for scanned/raster PDFs that have no vector objects
    if not snaps:
        drawings = page.get_drawings()
        snaps, lines, snap_meta = extract_snaps_typed(drawings)
        snap_engine = "pymupdf" if (snaps or lines) else "none"
    else:
        snap_engine = "pdfium"
    snap_meta["engine"] = snap_engine
    degraded_mode = "raster" if not snaps and not lines else "vector"
    warning = None
    if degraded_mode == "raster":
        warning = "ไม่พบ vector geometry บนหน้านี้: วัดได้แบบ manual เท่านั้น ควรตรวจทานเพิ่ม"

    W, H = orig_W, orig_H
    if rot in (90, 180, 270):
        snaps = _rotate_snaps(snaps, rot, W, H)
        lines = _rotate_lines(lines, rot, W, H)
        if rot in (90, 270): W, H = H, W

    size = {
        "w_pt": round(W,1),  "h_pt": round(H,1),
        "w_mm": round(W/PT_PER_MM,1), "h_mm": round(H/PT_PER_MM,1),
        "orig_w_pt": round(orig_W,1), "orig_h_pt": round(orig_H,1),
    }
    result = {"page":n, "size":size, "scale":scale,
              "snaps":snaps, "lines":lines, "render_scale":1.5,
              "snap_engine":snap_engine, "degraded_mode": degraded_mode,
              "snap_meta": snap_meta,
              "warning": warning}
    # cache pre-serialized JSON bytes — avoids re-serializing on every request
    result_bytes = json.dumps(result, separators=(",",":")).encode()
    _cache_put(cache, key, result_bytes, MAX_ANALYSE_CACHE_ENTRIES, MAX_ANALYSE_CACHE_BYTES)
    return Response(result_bytes, media_type="application/json")


@app.post("/export-pdf")
async def export_pdf(body: dict):
    case = _get_case(body.get("case_id", ""))
    if not case: return JSONResponse({"error":"invalid case"}, 400)
    doc = case.get("doc")
    pages   = body.get("pages", [])
    annots  = body.get("annotations", {})
    rotations = body.get("rotations", {})
    pdf_name  = body.get("pdfName", "export")

    def _rot_pt(x, y, rot, W, H):
        if rot == 0:   return x, y
        if rot == 90:  return y, W - x
        if rot == 180: return W - x, H - y
        return H - y, x

    new_doc = fitz.open()
    for pg_num in pages:
        idx = pg_num - 1
        if idx < 0 or idx >= len(doc): continue
        orig_page = doc[idx]
        orig_W, orig_H = orig_page.rect.width, orig_page.rect.height
        new_doc.insert_pdf(doc, from_page=idx, to_page=idx)
        page = new_doc[len(new_doc)-1]
        rot = int(rotations.get(str(pg_num), rotations.get(pg_num, 0)) or 0)
        if rot in (90, 180, 270):
            page.set_rotation(rot)
        pg_annots = annots.get(str(pg_num), {})

        for ln in pg_annots.get("lines", []):
            try:
                col = _hex_to_rgb(ln.get("color","#ffd60a"))
                raw_pts = _line_points(ln)
                if len(raw_pts) < 2:
                    continue
                pts = [fitz.Point(*_rot_pt(p["x"], p["y"], rot, orig_W, orig_H)) for p in raw_pts]
                page.draw_polyline(pts, color=col, width=1.5, dashes="[6 3]")
                pt_dist = ln.get("ptDist") or _line_length_pt(raw_pts)
                lbl = f"{ln['dist']:.2f} m" if ln.get("dist") else f"{pt_dist:.1f} pt"
                mid = raw_pts[len(raw_pts) // 2]
                mx, my = _rot_pt(mid["x"], mid["y"] - 5, rot, orig_W, orig_H)
                page.insert_text(fitz.Point(mx, my), lbl, fontsize=7, color=col)
            except Exception: pass

        for ref in pg_annots.get("refs", []):
            try:
                col = _hex_to_rgb(ref.get("color", "#5ac8fa"))
                raw_pts = _line_points(ref)
                if len(raw_pts) < 2:
                    continue
                pts = [fitz.Point(*_rot_pt(p["x"], p["y"], rot, orig_W, orig_H)) for p in raw_pts]
                page.draw_polyline(pts, color=col, width=1.2, dashes="[8 4]")
                mid = raw_pts[len(raw_pts) // 2]
                label = ref.get("name") or ref.get("refType") or "reference"
                mx, my = _rot_pt(mid["x"], mid["y"] - 5, rot, orig_W, orig_H)
                page.insert_text(fitz.Point(mx, my), label, fontsize=7, color=col)
            except Exception: pass

        for park in pg_annots.get("parking", []):
            try:
                col = _hex_to_rgb(park.get("color", "#ffcc00"))
                x, y = _rot_pt(park["x"], park["y"], rot, orig_W, orig_H)
                rect = fitz.Rect(x - 3, y - 3, x + 3, y + 3)
                page.draw_rect(rect, color=col, fill=col, width=0.8)
                page.insert_text(fitz.Point(x + 4, y + 3), "P", fontsize=7, color=col)
            except Exception: pass

        for poly in pg_annots.get("polys", []):
            if not poly.get("closed"): continue
            try:
                col  = _hex_to_rgb(poly.get("color","#30d158"))
                rpts = [_rot_pt(p["x"], p["y"], rot, orig_W, orig_H) for p in poly["pts"]]
                pts  = [fitz.Point(x, y) for x, y in rpts]
                page.draw_polyline(
                    pts,
                    color=col,
                    fill=col,
                    fill_opacity=0.08,
                    width=1.5,
                    closePath=True,
                )
                cx   = sum(x for x, _ in rpts) / len(rpts)
                cy   = sum(y for _, y in rpts) / len(rpts)
                rows = []
                if poly.get("name"):  rows.append(poly["name"])
                area = poly.get("area")
                if area is None and poly.get("pts"):
                    area_pt2 = _poly_area_pt2(poly["pts"])
                    area = area_pt2 / (poly.get("scale_pts_per_m", 1) ** 2) if poly.get("scale_pts_per_m") else None
                if area: rows.append(f"{area:.2f} sq.m")
                for i, t in enumerate(rows):
                    page.insert_text(fitz.Point(cx, cy+i*9-4), t, fontsize=7, color=col)
            except Exception: pass

    buf = io.BytesIO()
    new_doc.save(buf)
    new_doc.close()
    safe = pdf_name.replace("/","_").replace("\\","_").replace(".pdf","")
    return Response(buf.getvalue(), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{safe}_export.pdf"'})



# ══════════════════════════════════════════════════════
# Frontend
# ══════════════════════════════════════════════════════
@app.post("/project")
async def save_project(body: dict):
    case = _get_case(body.get("case_id", ""))
    if not case: return JSONResponse({"error":"invalid case"}, 400)
    case["page_tags"] = body.get("page_tags", {})
    case["project_info"] = body.get("project_info", {})
    return {"ok": True}

@app.get("/project")
def get_project(case_id: str):
    case = _get_case(case_id)
    if not case: return JSONResponse({"error":"invalid case"}, 400)
    return {
        "page_tags": case.get("page_tags", {}),
        "project_info": case.get("project_info", {})
    }


TAG_LABELS = {
    "site": "ผังบริเวณ",
    "plan": "ชั้น",
    "elev": "รูปด้าน",
    "section": "รูปตัด",
    "detail": "รายละเอียด",
    "schedule": "ตาราง",
    "other": "อื่น ๆ",
}

def _scale_state_py(sc: dict) -> str:
    if not sc: return "missing"
    if sc.get("calibrated") or sc.get("source") == "manual": return "manual"
    if sc.get("verified"): return "ok"
    return "warn"

def _semantic_tag(kind: str, obj: dict | None = None) -> str:
    obj = obj or {}
    if kind == "poly":
        area_type = obj.get("areaType") or "room"
        if area_type == "land":
            return "site_boundary"
        if area_type in ("building", "gfa"):
            return "gross_floor_area"
        if area_type == "non_gfa":
            return "floor_area"
        return "use_area"
    if kind == "opening":
        return "deduction_opening"
    if kind == "ref":
        return "reference_line"
    if kind == "line":
        return "scale_line" if obj.get("semanticRole") == "scale" else "dimension_line"
    if kind == "north":
        return "north_arrow"
    return "review_note"

def _use_category(obj: dict | None, semantic_tag: str) -> str | None:
    if semantic_tag not in AREA_SEMANTIC_TAGS:
        return None
    value = (obj or {}).get("useCategory")
    return value or None

@app.post("/export-xlsx")
async def export_xlsx(body: dict):
    if not HAS_XLSX:
        return JSONResponse({"error": "xlsxwriter not installed"}, 501)
    case = _get_case(body.get("case_id", ""))
    if not case: return JSONResponse({"error":"invalid case"}, 400)

    page_store   = body.get("pageStore", {})
    page_tags    = body.get("pageTags", {})
    page_names   = body.get("pageNames", {})
    project_info = body.get("projectInfo", {}) if isinstance(body.get("projectInfo", {}), dict) else {}
    site_orientation = body.get("siteOrientation", {}) if isinstance(body.get("siteOrientation", {}), dict) else {}
    page_scales  = body.get("pageScales", {})
    pdf_name     = body.get("pdfName", "export")
    page_count   = body.get("pageCount") or len(page_store)
    warnings     = body.get("warnings", [])
    audit_meta   = body.get("auditMeta", {}) if isinstance(body.get("auditMeta", {}), dict) else {}
    try:
        page_count = int(page_count)
    except (TypeError, ValueError):
        page_count = len(page_store)
    store_pages = []
    for key in page_store.keys():
        try:
            store_pages.append(int(key))
        except (TypeError, ValueError):
            pass
    if store_pages:
        page_count = max(page_count, max(store_pages))

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})

    fmt_hdr = wb.add_format({"bold": True, "bg_color": "#1F4E79", "font_color": "white", "border": 1, "align": "center", "valign": "vcenter"})
    fmt_sub = wb.add_format({"bg_color": "#D6E4F0", "bold": True, "border": 1, "align": "center"})
    fmt_cell = wb.add_format({"border": 1, "valign": "top"})
    fmt_num  = wb.add_format({"border": 1, "num_format": '#,##0.00'})
    fmt_total = wb.add_format({"bold": True, "bg_color": "#E2EFDA", "border": 1, "num_format": '#,##0.00'})
    fmt_deduct = wb.add_format({"bold": True, "bg_color": "#FCE4EC", "border": 1, "num_format": '#,##0.00', "font_color": "#C62828"})
    fmt_net  = wb.add_format({"bold": True, "bg_color": "#C6EFCE", "border": 1, "num_format": '#,##0.00', "font_color": "#006100"})
    fmt_note = wb.add_format({"font_color": "#666666", "italic": True, "border": 1})
    fmt_tag  = wb.add_format({"border": 1, "align": "center", "valign": "vcenter"})

    generated_at = audit_meta.get("generatedAt") or time.strftime("%Y-%m-%dT%H:%M:%S%z")

    ws_cover = wb.add_worksheet("Cover")
    ws_cover.set_column(0, 0, 24); ws_cover.set_column(1, 1, 44)
    ws_cover.write(0, 0, "BMA-Plan Phase 1 Export", fmt_hdr)
    cover_rows = [
        ("PDF", pdf_name),
        ("Generated At", generated_at),
        ("Page Count", page_count),
        ("Project No", project_info.get("reqNo", "")),
        ("Building Type", project_info.get("buildingType", "")),
        ("Work Type", project_info.get("workType", "")),
        ("Floors", project_info.get("floors", "")),
        ("GFA", project_info.get("gfa", "")),
        ("Units", project_info.get("units", "")),
        ("Area Objects", audit_meta.get("areaCount", "")),
        ("Opening Objects", audit_meta.get("openingCount", "")),
        ("Line Objects", audit_meta.get("lineCount", "")),
        ("Reference Objects", audit_meta.get("refCount", "")),
        ("Parking Markers", audit_meta.get("parkingCount", "")),
        ("Gross Area", audit_meta.get("grossArea", "")),
        ("Opening Area", audit_meta.get("openingArea", "")),
        ("Net Area", audit_meta.get("netArea", "")),
        ("Warning Count", len(warnings)),
    ]
    for r, (k, v) in enumerate(cover_rows, start=2):
        ws_cover.write(r, 0, k, fmt_cell); ws_cover.write(r, 1, v, fmt_cell)

    ws_warn = wb.add_worksheet("Warnings")
    ws_warn.set_column(0, 0, 10); ws_warn.set_column(1, 1, 12); ws_warn.set_column(2, 2, 12)
    ws_warn.set_column(3, 3, 20); ws_warn.set_column(4, 5, 44)
    for c, h in enumerate(["id", "severity", "page_index", "object_id", "message", "suggested_action"]):
        ws_warn.write(0, c, h, fmt_hdr)
    for r, w in enumerate(warnings, start=1):
        ws_warn.write(r, 0, w.get("id", ""), fmt_cell)
        ws_warn.write(r, 1, w.get("severity", ""), fmt_cell)
        ws_warn.write(r, 2, w.get("page_index", ""), fmt_cell)
        ws_warn.write(r, 3, w.get("object_id", ""), fmt_cell)
        ws_warn.write(r, 4, w.get("message", ""), fmt_cell)
        ws_warn.write(r, 5, w.get("suggested_action", ""), fmt_cell)

    ws_scales = wb.add_worksheet("Page Scales")
    ws_scales.set_column(0, 0, 10); ws_scales.set_column(1, 1, 24); ws_scales.set_column(2, 6, 18)
    ws_scales.set_column(7, 9, 16)
    for c, h in enumerate(["page", "page_name", "label", "pts_per_m", "source", "verified", "status",
                            "scale_state", "object_count", "needs_attention"]):
        ws_scales.write(0, c, h, fmt_hdr)
    scale_row = 1
    for pg in range(1, max(page_count, 0) + 1):
        pg_str = str(pg)
        sc = page_scales.get(pg_str, {}) if isinstance(page_scales, dict) else {}
        if sc is None: sc = {}
        pg_data = page_store.get(pg_str, {})
        obj_count = (
            len([p for p in pg_data.get("polys", []) if p.get("closed")]) +
            len([o for o in pg_data.get("openings", []) if o.get("closed")]) +
            len(pg_data.get("lines", [])) +
            len(pg_data.get("refs", [])) +
            len(pg_data.get("parking", []))
        )
        state = _scale_state_py(sc)
        attention = obj_count > 0 and state not in ("ok", "manual")
        ws_scales.write(scale_row, 0, pg, fmt_cell)
        ws_scales.write(scale_row, 1, page_names.get(pg_str, f"หน้า {pg_str}"), fmt_cell)
        ws_scales.write(scale_row, 2, sc.get("label", ""), fmt_cell)
        ws_scales.write(scale_row, 3, sc.get("pts_per_m", ""), fmt_num)
        ws_scales.write(scale_row, 4, sc.get("source", ""), fmt_cell)
        ws_scales.write(scale_row, 5, bool(sc.get("verified", False)), fmt_cell)
        ws_scales.write(scale_row, 6, sc.get("status", ""), fmt_cell)
        ws_scales.write(scale_row, 7, state, fmt_cell)
        ws_scales.write(scale_row, 8, obj_count, fmt_cell)
        ws_scales.write(scale_row, 9, attention, fmt_cell)
        scale_row += 1

    ws_facts = wb.add_worksheet("Site Facts")
    ws_facts.set_column(0, 0, 10); ws_facts.set_column(1, 1, 24); ws_facts.set_column(2, 2, 18)
    ws_facts.set_column(3, 3, 22); ws_facts.set_column(4, 4, 16); ws_facts.set_column(5, 5, 18)
    ws_facts.set_column(6, 7, 34); ws_facts.set_column(8, 9, 18)
    for c, h in enumerate(["page", "page_name", "fact_type", "object_id", "side_index", "label", "role_or_angle", "note", "semanticTag", "useCategory"]):
        ws_facts.write(0, c, h, fmt_hdr)
    fact_row = 1
    for pg in range(1, max(page_count, 0) + 1):
        pg_str = str(pg)
        pg_name = page_names.get(pg_str, f"หน้า {pg_str}")
        orient = site_orientation.get(pg_str, {}) if isinstance(site_orientation, dict) else {}
        north = orient.get("north", {}) if isinstance(orient, dict) else {}
        if isinstance(north, dict) and north:
            ws_facts.write(fact_row, 0, pg, fmt_cell)
            ws_facts.write(fact_row, 1, pg_name, fmt_cell)
            ws_facts.write(fact_row, 2, "north", fmt_cell)
            ws_facts.write(fact_row, 3, "", fmt_cell)
            ws_facts.write(fact_row, 4, "", fmt_cell)
            ws_facts.write(fact_row, 5, "N / ทิศเหนือ", fmt_cell)
            ws_facts.write(fact_row, 6, north.get("angleDeg", ""), fmt_cell)
            ws_facts.write(fact_row, 7, f"{north.get('source', '')} · {north.get('status', '')}", fmt_note)
            ws_facts.write(fact_row, 8, north.get("semanticTag") or _semantic_tag("north", north), fmt_cell)
            ws_facts.write(fact_row, 9, "", fmt_cell)
            fact_row += 1
        pg_data = page_store.get(pg_str, {}) if isinstance(page_store, dict) else {}
        for poly in pg_data.get("polys", []):
            if not poly.get("closed") or poly.get("areaType") != "land":
                continue
            for idx, tag in enumerate(poly.get("edgeTags", []) or []):
                if not isinstance(tag, dict):
                    continue
                ws_facts.write(fact_row, 0, pg, fmt_cell)
                ws_facts.write(fact_row, 1, pg_name, fmt_cell)
                ws_facts.write(fact_row, 2, "parcel_side", fmt_cell)
                ws_facts.write(fact_row, 3, poly.get("id", ""), fmt_cell)
                ws_facts.write(fact_row, 4, idx + 1, fmt_cell)
                ws_facts.write(fact_row, 5, tag.get("label", f"ด้าน {idx+1}"), fmt_cell)
                ws_facts.write(fact_row, 6, tag.get("role") or tag.get("type", ""), fmt_cell)
                ws_facts.write(fact_row, 7, tag.get("note", ""), fmt_note)
                ws_facts.write(fact_row, 8, "parcel_side", fmt_cell)
                ws_facts.write(fact_row, 9, "", fmt_cell)
                fact_row += 1

    ws_audit = wb.add_worksheet("Audit Log")
    ws_audit.set_column(0, 0, 24); ws_audit.set_column(1, 1, 64)
    audit_rows = [
        ("generated_at", generated_at),
        ("pdf_name", pdf_name),
        ("source", "UI pageStore"),
        ("area_count", audit_meta.get("areaCount", "")),
        ("opening_count", audit_meta.get("openingCount", "")),
        ("line_count", audit_meta.get("lineCount", "")),
        ("reference_count", audit_meta.get("refCount", "")),
        ("parking_count", audit_meta.get("parkingCount", "")),
        ("warning_count", len(warnings)),
    ]
    for r, (k, v) in enumerate(audit_rows):
        ws_audit.write(r, 0, k, fmt_hdr if r == 0 else fmt_cell)
        ws_audit.write(r, 1, v, fmt_cell)

    # ── Sheet 1: สรุปพื้นที่ (with Tag column) ──
    ws = wb.add_worksheet("สรุปพื้นที่")
    ws.set_column(0, 0, 18); ws.set_column(1, 1, 12); ws.set_column(2, 2, 28)
    ws.set_column(3, 3, 14); ws.set_column(4, 4, 14); ws.set_column(5, 5, 14)
    ws.set_column(6, 6, 14); ws.set_column(7, 7, 40); ws.set_column(8, 9, 18)
    ws.set_column(10, 14, 20)
    row = 0
    ws.merge_range(row, 0, row, 14, f"สรุปพื้นที่ — {pdf_name}", wb.add_format({"bold": True, "font_size": 14, "bg_color": "#1F4E79", "font_color": "white", "align": "center"}))
    row += 2
    headers = ["หน้า / ชั้น", "Tag", "ชื่อพื้นที่", "พื้นที่ (ตร.ม.)", "ไร่", "งาน", "ตารางวา", "หมายเหตุ", "semanticTag", "useCategory", "measurementProfile", "objectCategory", "reportTarget", "lawBasis", "countingRule"]
    for c, h in enumerate(headers): ws.write(row, c, h, fmt_hdr)
    row += 1
    grand_total = 0.0; grand_openings = 0.0
    for pg_str in sorted(page_store.keys(), key=lambda x: int(x)):
        pg = int(pg_str); pg_data = page_store[pg_str]
        tag = page_tags.get(pg_str, ""); pg_name = page_names.get(pg_str, f"หน้า {pg}")
        tag_label = TAG_LABELS.get(tag, tag) if tag else ""
        scale_info = page_scales.get(pg_str, {})
        pts_per_m = scale_info.get("pts_per_m", 0) if isinstance(scale_info, dict) else 0
        polys = pg_data.get("polys", []); openings = pg_data.get("openings", [])
        if not polys and not openings: continue
        ws.merge_range(row, 0, row, 14, pg_name, fmt_sub); row += 1
        pg_total = 0.0; pg_openings = 0.0
        for poly in polys:
            if not poly.get("closed"): continue
            area = poly.get("area", 0) or 0
            if area <= 0 and poly.get("pts") and pts_per_m > 0:
                area = _poly_area_pt2(poly["pts"]) / (pts_per_m ** 2)
            name = poly.get("name", ""); area_type = poly.get("areaType", "room")
            semantic_tag = poly.get("semanticTag") or _semantic_tag("poly", poly)
            use_category = _use_category(poly, semantic_tag)
            rwu = _m2_to_rwu(area) if area_type == "land" else ""
            note = f"สเกล: {scale_info.get('label', '-')}" if isinstance(scale_info, dict) else ""
            ws.write(row, 0, pg_name, fmt_cell); ws.write(row, 1, tag_label, fmt_tag)
            ws.write(row, 2, name or f"พื้นที่ {poly.get('id', '')}", fmt_cell)
            ws.write(row, 3, round(area, 2) if area else None, fmt_num)
            if area_type == "land" and area > 0:
                ws.write(row, 4, rwu[0] if rwu else None, fmt_num)
                ws.write(row, 5, rwu[1] if rwu else None, fmt_num)
                ws.write(row, 6, rwu[2] if rwu else None, fmt_num)
            else:
                ws.write(row, 4, "", fmt_cell); ws.write(row, 5, "", fmt_cell); ws.write(row, 6, "", fmt_cell)
            ws.write(row, 7, note, fmt_note)
            ws.write(row, 8, semantic_tag, fmt_cell); ws.write(row, 9, use_category or "", fmt_cell)
            poly_meta = _get_meta(poly, semantic_tag)
            ws.write(row, 10, poly_meta["measurementProfile"], fmt_cell)
            ws.write(row, 11, poly_meta["objectCategory"], fmt_cell)
            ws.write(row, 12, poly_meta["reportTarget"], fmt_cell)
            ws.write(row, 13, poly_meta["lawBasis"] or "", fmt_cell)
            ws.write(row, 14, poly_meta["countingRule"], fmt_cell)
            row += 1
            if area > 0: pg_total += area
        for op in openings:
            area = op.get("area", 0) or 0; name = op.get("name", "ช่องว่าง")
            if area <= 0 and op.get("pts") and pts_per_m > 0:
                area = _poly_area_pt2(op["pts"]) / (pts_per_m ** 2)
            ws.write(row, 0, "", fmt_cell); ws.write(row, 1, "", fmt_tag)
            ws.write(row, 2, f"(-) {name}", fmt_cell)
            ws.write(row, 3, -round(area, 2) if area else None, fmt_deduct)
            ws.write(row, 4, "", fmt_cell); ws.write(row, 5, "", fmt_cell); ws.write(row, 6, "", fmt_cell)
            semantic_tag = op.get("semanticTag") or _semantic_tag("opening", op)
            ws.write(row, 7, op.get("note", "หักช่องว่าง"), fmt_note)
            ws.write(row, 8, semantic_tag, fmt_cell); ws.write(row, 9, "", fmt_cell)
            op_meta = _get_meta(op, semantic_tag)
            ws.write(row, 10, op_meta["measurementProfile"], fmt_cell)
            ws.write(row, 11, op_meta["objectCategory"], fmt_cell)
            ws.write(row, 12, op_meta["reportTarget"], fmt_cell)
            ws.write(row, 13, op_meta["lawBasis"] or "", fmt_cell)
            ws.write(row, 14, op_meta["countingRule"], fmt_cell)
            row += 1
            if area > 0: pg_openings += area
        net = pg_total - pg_openings
        ws.write(row, 0, "", fmt_cell); ws.write(row, 1, "", fmt_tag)
        ws.write(row, 2, "รวมสุทธิ", fmt_cell); ws.write(row, 3, round(net, 2), fmt_net)
        ws.write(row, 4, "", fmt_cell); ws.write(row, 5, "", fmt_cell); ws.write(row, 6, "", fmt_cell)
        ws.write(row, 7, f"รวม {pg_total:.2f} − ช่องว่าง {pg_openings:.2f}", fmt_note)
        for c in range(8, 15): ws.write(row, c, "", fmt_cell)
        row += 2
        grand_total += pg_total; grand_openings += pg_openings
    ws.merge_range(row, 0, row, 2, "รวมทั้งโปรเจกต์", fmt_hdr)
    ws.write(row, 3, round(grand_total, 2), fmt_total)
    ws.write(row, 4, "", fmt_cell); ws.write(row, 5, "", fmt_cell); ws.write(row, 6, "", fmt_cell)
    ws.write(row, 7, f"รวม {grand_total:.2f} − ช่องว่าง {grand_openings:.2f} = สุทธิ {grand_total - grand_openings:.2f} ตร.ม.", fmt_note)
    for c in range(8, 15): ws.write(row, c, "", fmt_cell)

    # ── Sheet 2: ความยาวเส้น Polygon (with Tag) ──
    ws2 = wb.add_worksheet("ความยาวเส้น Polygon")
    ws2.set_column(0, 0, 18); ws2.set_column(1, 1, 12); ws2.set_column(2, 2, 28)
    ws2.set_column(3, 3, 14); ws2.set_column(4, 4, 14); ws2.set_column(5, 5, 40); ws2.set_column(6, 7, 18)
    ws2.set_column(8, 12, 20)
    r2 = 0
    ws2.merge_range(r2, 0, r2, 12, "ความยาวเส้นทุกด้านของ Polygon", wb.add_format({"bold": True, "font_size": 14, "bg_color": "#1F4E79", "font_color": "white", "align": "center"}))
    r2 += 2
    for c, h in enumerate(["หน้า / ชั้น", "Tag", "ชื่อพื้นที่", "ด้านที่", "ความยาว (ม.)", "หมายเหตุ", "semanticTag", "useCategory", "measurementProfile", "objectCategory", "reportTarget", "lawBasis", "countingRule"]):
        ws2.write(r2, c, h, fmt_hdr)
    r2 += 1
    for pg_str in sorted(page_store.keys(), key=lambda x: int(x)):
        pg = int(pg_str); pg_data = page_store[pg_str]
        pg_name = page_names.get(pg_str, f"หน้า {pg}")
        tag = page_tags.get(pg_str, ""); tag_label = TAG_LABELS.get(tag, tag) if tag else ""
        scale_info = page_scales.get(pg_str, {})
        pts_per_m = scale_info.get("pts_per_m", 0) if isinstance(scale_info, dict) else 0
        if pts_per_m <= 0: continue
        for poly in pg_data.get("polys", []):
            if not poly.get("closed") or not poly.get("pts"): continue
            name = poly.get("name", f"พื้นที่ {poly.get('id', '')}"); pts = poly["pts"]
            semantic_tag = poly.get("semanticTag") or _semantic_tag("poly", poly)
            use_category = _use_category(poly, semantic_tag)
            for i in range(len(pts)):
                p1 = pts[i]; p2 = pts[(i + 1) % len(pts)]
                dist_pt = math.hypot(p2["x"] - p1["x"], p2["y"] - p1["y"])
                dist_m = dist_pt / pts_per_m
                ws2.write(r2, 0, pg_name, fmt_cell); ws2.write(r2, 1, tag_label, fmt_tag)
                ws2.write(r2, 2, name, fmt_cell); ws2.write(r2, 3, f"ด้าน {i+1}", fmt_cell)
                ws2.write(r2, 4, round(dist_m, 2), fmt_num); ws2.write(r2, 5, f"{dist_pt:.1f} pt", fmt_note)
                ws2.write(r2, 6, semantic_tag, fmt_cell); ws2.write(r2, 7, use_category or "", fmt_cell)
                poly2_meta = _get_meta(poly, semantic_tag)
                ws2.write(r2, 8, poly2_meta["measurementProfile"], fmt_cell)
                ws2.write(r2, 9, poly2_meta["objectCategory"], fmt_cell)
                ws2.write(r2, 10, poly2_meta["reportTarget"], fmt_cell)
                ws2.write(r2, 11, poly2_meta["lawBasis"] or "", fmt_cell)
                ws2.write(r2, 12, poly2_meta["countingRule"], fmt_cell)
                r2 += 1

    # ── Sheet 3: สรุปตามชั้น ──
    ws3 = wb.add_worksheet("สรุปตามชั้น")
    ws3.set_column(0, 0, 18); ws3.set_column(1, 1, 12); ws3.set_column(2, 2, 14)
    ws3.set_column(3, 3, 18); ws3.set_column(4, 4, 12)
    r3 = 0
    ws3.merge_range(r3, 0, r3, 4, "สรุปพื้นที่ตามชั้น", wb.add_format({"bold": True, "font_size": 14, "bg_color": "#1F4E79", "font_color": "white", "align": "center"}))
    r3 += 2
    for c, h in enumerate(["หน้า / ชั้น", "Tag", "จำนวนห้อง", "พื้นที่รวม (ตร.ม.)", "%"]):
        ws3.write(r3, c, h, fmt_hdr)
    r3 += 1
    grand_total_all = 0.0
    for pg_str in sorted(page_store.keys(), key=lambda x: int(x)):
        pg = int(pg_str); pg_data = page_store[pg_str]
        scale_info = page_scales.get(pg_str, {})
        pts_per_m = scale_info.get("pts_per_m", 0) if isinstance(scale_info, dict) else 0
        closed_polys = [p for p in pg_data.get("polys", []) if p.get("closed")]
        if not closed_polys: continue
        pg_area = 0.0
        for poly in closed_polys:
            area = poly.get("area", 0) or 0
            if area <= 0 and poly.get("pts") and pts_per_m > 0:
                area = _poly_area_pt2(poly["pts"]) / (pts_per_m ** 2)
            pg_area += area
        grand_total_all += pg_area
    for pg_str in sorted(page_store.keys(), key=lambda x: int(x)):
        pg = int(pg_str); pg_data = page_store[pg_str]
        pg_name = page_names.get(pg_str, f"หน้า {pg}")
        tag = page_tags.get(pg_str, ""); tag_label = TAG_LABELS.get(tag, tag) if tag else ""
        scale_info = page_scales.get(pg_str, {})
        pts_per_m = scale_info.get("pts_per_m", 0) if isinstance(scale_info, dict) else 0
        closed_polys = [p for p in pg_data.get("polys", []) if p.get("closed")]
        if not closed_polys: continue
        pg_area = 0.0
        for poly in closed_polys:
            area = poly.get("area", 0) or 0
            if area <= 0 and poly.get("pts") and pts_per_m > 0:
                area = _poly_area_pt2(poly["pts"]) / (pts_per_m ** 2)
            pg_area += area
        pct = (pg_area / grand_total_all * 100) if grand_total_all > 0 else 0
        ws3.write(r3, 0, pg_name, fmt_cell); ws3.write(r3, 1, tag_label, fmt_tag)
        ws3.write(r3, 2, len(closed_polys), fmt_cell)
        ws3.write(r3, 3, round(pg_area, 2), fmt_num)
        ws3.write(r3, 4, f"{pct:.1f}%", fmt_cell)
        r3 += 1
    if grand_total_all > 0:
        ws3.write(r3, 0, "", fmt_cell); ws3.write(r3, 1, "", fmt_tag)
        ws3.write(r3, 2, "", fmt_cell); ws3.write(r3, 3, round(grand_total_all, 2), fmt_total)
        ws3.write(r3, 4, "100%", fmt_cell)

    # ── Sheet 4: สรุปตามประเภท ──
    ws4 = wb.add_worksheet("สรุปตามประเภท")
    ws4.set_column(0, 0, 18); ws4.set_column(1, 1, 14); ws4.set_column(2, 2, 18)
    ws4.set_column(3, 3, 12)
    r4 = 0
    ws4.merge_range(r4, 0, r4, 3, "สรุปพื้นที่ตามประเภท", wb.add_format({"bold": True, "font_size": 14, "bg_color": "#1F4E79", "font_color": "white", "align": "center"}))
    r4 += 2
    for c, h in enumerate(["ประเภท (Tag)", "จำนวน", "พื้นที่รวม (ตร.ม.)", "%"]):
        ws4.write(r4, c, h, fmt_hdr)
    r4 += 1
    tag_stats = {}
    for pg_str in sorted(page_store.keys(), key=lambda x: int(x)):
        pg = int(pg_str); pg_data = page_store[pg_str]
        tag = page_tags.get(pg_str, "") or "untagged"
        scale_info = page_scales.get(pg_str, {})
        pts_per_m = scale_info.get("pts_per_m", 0) if isinstance(scale_info, dict) else 0
        for poly in pg_data.get("polys", []):
            if not poly.get("closed"): continue
            area = poly.get("area", 0) or 0
            if area <= 0 and poly.get("pts") and pts_per_m > 0:
                area = _poly_area_pt2(poly["pts"]) / (pts_per_m ** 2)
            if tag not in tag_stats: tag_stats[tag] = {"count": 0, "area": 0.0}
            tag_stats[tag]["count"] += 1; tag_stats[tag]["area"] += area
    total_by_tag = sum(s["area"] for s in tag_stats.values())
    for tag, stats in sorted(tag_stats.items()):
        tag_label = TAG_LABELS.get(tag, tag) if tag != "untagged" else "ไม่ได้ระบุ"
        pct = (stats["area"] / total_by_tag * 100) if total_by_tag > 0 else 0
        ws4.write(r4, 0, tag_label, fmt_cell); ws4.write(r4, 1, stats["count"], fmt_cell)
        ws4.write(r4, 2, round(stats["area"], 2), fmt_num)
        ws4.write(r4, 3, f"{pct:.1f}%", fmt_cell)
        r4 += 1
    if total_by_tag > 0:
        ws4.write(r4, 0, "รวม", fmt_cell); ws4.write(r4, 1, sum(s["count"] for s in tag_stats.values()), fmt_cell)
        ws4.write(r4, 2, round(total_by_tag, 2), fmt_total); ws4.write(r4, 3, "100%", fmt_cell)

    # ── Sheet 5: สรุปที่จอดรถ ──
    ws5 = wb.add_worksheet("ที่จอดรถ")
    ws5.set_column(0, 0, 18); ws5.set_column(1, 1, 12); ws5.set_column(2, 2, 18)
    ws5.set_column(3, 3, 12); ws5.set_column(4, 4, 36); ws5.set_column(5, 6, 18)
    ws5.set_column(7, 11, 20)
    r5 = 0
    ws5.merge_range(r5, 0, r5, 11, "สรุปที่จอดรถจากจุดที่ผู้ใช้ mark", wb.add_format({"bold": True, "font_size": 14, "bg_color": "#1F4E79", "font_color": "white", "align": "center"}))
    r5 += 2
    for c, h in enumerate(["หน้า / ชั้น", "Tag", "ประเภทที่จอด", "จำนวน (คัน)", "หมายเหตุ", "semanticTag", "useCategory", "measurementProfile", "objectCategory", "reportTarget", "lawBasis", "countingRule"]):
        ws5.write(r5, c, h, fmt_hdr)
    r5 += 1
    parking_totals = {}
    for pg_str in sorted(page_store.keys(), key=lambda x: int(x)):
        pg_data = page_store[pg_str]
        pg_name = page_names.get(pg_str, f"หน้า {pg_str}")
        tag = page_tags.get(pg_str, ""); tag_label = TAG_LABELS.get(tag, tag) if tag else ""
        by_type = {}
        by_semantic = {}
        for park in pg_data.get("parking", []):
            ptype = park.get("parkingType") or "car"
            by_type[ptype] = by_type.get(ptype, 0) + (park.get("count") or 1)
            by_semantic.setdefault(ptype, park.get("semanticTag") or _semantic_tag("parking", park))
            parking_totals[ptype] = parking_totals.get(ptype, 0) + (park.get("count") or 1)
        for ptype, count in sorted(by_type.items()):
            ws5.write(r5, 0, pg_name, fmt_cell); ws5.write(r5, 1, tag_label, fmt_tag)
            ws5.write(r5, 2, ptype, fmt_cell); ws5.write(r5, 3, count, fmt_cell)
            ws5.write(r5, 4, "ข้อมูลนับจาก marker ยังไม่เทียบเกณฑ์กฎหมาย", fmt_note)
            park_sem = by_semantic.get(ptype, "review_note")
            ws5.write(r5, 5, park_sem, fmt_cell); ws5.write(r5, 6, "", fmt_cell)
            park_meta = _derive_measurement_meta(park_sem)
            ws5.write(r5, 7, park_meta["measurementProfile"], fmt_cell)
            ws5.write(r5, 8, park_meta["objectCategory"], fmt_cell)
            ws5.write(r5, 9, park_meta["reportTarget"], fmt_cell)
            ws5.write(r5, 10, park_meta["lawBasis"] or "", fmt_cell)
            ws5.write(r5, 11, park_meta["countingRule"], fmt_cell)
            r5 += 1
    if parking_totals:
        ws5.write(r5, 0, "รวม", fmt_cell); ws5.write(r5, 1, "", fmt_tag)
        ws5.write(r5, 2, ", ".join(f"{k}: {v}" for k, v in sorted(parking_totals.items())), fmt_cell)
        ws5.write(r5, 3, sum(parking_totals.values()), fmt_total)
        ws5.write(r5, 4, "", fmt_note)
        for c in range(5, 12): ws5.write(r5, c, "", fmt_cell)

    # ── Sheet 6: ระยะถึงเส้นอ้างอิง ──
    ws6 = wb.add_worksheet("ระยะอ้างอิง")
    ws6.set_column(0, 0, 18); ws6.set_column(1, 1, 12); ws6.set_column(2, 2, 24)
    ws6.set_column(3, 3, 28); ws6.set_column(4, 4, 14); ws6.set_column(5, 5, 28); ws6.set_column(6, 7, 18)
    ws6.set_column(8, 12, 20)
    r6 = 0
    ws6.merge_range(r6, 0, r6, 12, "รายงานระยะ object ถึงเส้นอ้างอิง", wb.add_format({"bold": True, "font_size": 14, "bg_color": "#1F4E79", "font_color": "white", "align": "center"}))
    r6 += 2
    for c, h in enumerate(["หน้า / ชั้น", "Tag", "เส้นอ้างอิง", "Object", "ระยะ", "หมายเหตุ", "semanticTag", "useCategory", "measurementProfile", "objectCategory", "reportTarget", "lawBasis", "countingRule"]):
        ws6.write(r6, c, h, fmt_hdr)
    r6 += 1
    for pg_str in sorted(page_store.keys(), key=lambda x: int(x)):
        pg_data = page_store[pg_str]
        refs = pg_data.get("refs", [])
        if not refs:
            continue
        pg_name = page_names.get(pg_str, f"หน้า {pg_str}")
        tag = page_tags.get(pg_str, ""); tag_label = TAG_LABELS.get(tag, tag) if tag else ""
        scale_info = page_scales.get(pg_str, {})
        pts_per_m = scale_info.get("pts_per_m", 0) if isinstance(scale_info, dict) else 0
        objects = []
        for kind, items in (("parking", pg_data.get("parking", [])), ("poly", pg_data.get("polys", [])), ("opening", pg_data.get("openings", [])), ("line", pg_data.get("lines", []))):
            for idx, obj in enumerate(items):
                if kind in ("poly", "opening") and not obj.get("closed"):
                    continue
                objects.append((kind, idx, obj))
        for ref in refs:
            ref_name = ref.get("name") or ref.get("refType") or "reference"
            for kind, idx, obj in objects:
                best = None
                for pt in _object_points_for_ref_report(kind, obj):
                    hit = _distance_to_ref(pt, ref)
                    if hit and (best is None or hit["dist_pt"] < best["dist_pt"]):
                        best = hit
                if not best:
                    continue
                if kind == "parking":
                    obj_name = f"parking {idx+1} ({obj.get('parkingType', 'car')})"
                elif kind == "poly":
                    obj_name = obj.get("name") or obj.get("areaType") or f"poly {idx+1}"
                elif kind == "opening":
                    obj_name = obj.get("name") or f"opening {idx+1}"
                else:
                    obj_name = "path" if obj.get("kind") == "path" else "line"
                dist_val = best["dist_pt"] / pts_per_m if pts_per_m > 0 else best["dist_pt"]
                unit = "ม." if pts_per_m > 0 else "pt"
                ws6.write(r6, 0, pg_name, fmt_cell); ws6.write(r6, 1, tag_label, fmt_tag)
                ws6.write(r6, 2, ref_name, fmt_cell); ws6.write(r6, 3, obj_name, fmt_cell)
                ws6.write(r6, 4, round(dist_val, 2), fmt_num)
                ws6.write(r6, 5, f"{best.get('point_role', '')} · {unit}", fmt_note)
                semantic_tag = obj.get("semanticTag") or _semantic_tag(kind if kind != "poly" else "poly", obj)
                ws6.write(r6, 6, semantic_tag, fmt_cell); ws6.write(r6, 7, _use_category(obj, semantic_tag) or "", fmt_cell)
                ref_meta = _get_meta(obj, semantic_tag)
                ws6.write(r6, 8, ref_meta["measurementProfile"], fmt_cell)
                ws6.write(r6, 9, ref_meta["objectCategory"], fmt_cell)
                ws6.write(r6, 10, ref_meta["reportTarget"], fmt_cell)
                ws6.write(r6, 11, ref_meta["lawBasis"] or "", fmt_cell)
                ws6.write(r6, 12, ref_meta["countingRule"], fmt_cell)
                r6 += 1

    # ── Sheet 7: สรุปตาม Report Target ──
    ws7 = wb.add_worksheet("สรุปตาม Report Target")
    ws7.set_column(0, 0, 28); ws7.set_column(1, 1, 18); ws7.set_column(2, 2, 18)
    ws7.set_column(3, 3, 28); ws7.set_column(4, 4, 12); ws7.set_column(5, 6, 16); ws7.set_column(7, 7, 16)
    r7 = 0
    fmt_rt_title = wb.add_format({"bold": True, "font_size": 14, "bg_color": "#1F4E79", "font_color": "white", "align": "center"})
    ws7.merge_range(r7, 0, r7, 7, "สรุปตาม Report Target", fmt_rt_title)
    r7 += 2
    for c, h in enumerate(["Report Target", "Object Category", "Counting Rule", "Pages", "Objects", "Area (m²)", "Length (m)", "Parking"]):
        ws7.write(r7, c, h, fmt_hdr)
    r7 += 1
    RT_ORDER = ["Building Area Summary", "Use Category Summary", "Open Space Summary", "Deduction Summary",
                "Parking Summary", "Site Facts", "Distance Facts", "Height Facts", "Audit Log", "Unclassified"]
    rt_stats: dict = {}
    for pg_str in sorted(page_store.keys(), key=lambda x: int(x)):
        pg_data = page_store[pg_str]
        pg_name = page_names.get(pg_str, f"หน้า {pg_str}")
        scale_info7 = page_scales.get(pg_str, {})
        pts_per_m7 = scale_info7.get("pts_per_m", 0) if isinstance(scale_info7, dict) else 0
        for poly in pg_data.get("polys", []):
            if not poly.get("closed"):
                continue
            sem = poly.get("semanticTag") or _semantic_tag("poly", poly)
            meta = _get_meta(poly, sem)
            key = (meta["reportTarget"] or "Unclassified", meta["objectCategory"] or "annotation", meta["countingRule"] or "reference")
            if key not in rt_stats:
                rt_stats[key] = {"pages": set(), "objects": 0, "area": 0.0, "length": 0.0, "parking": 0}
            rt_stats[key]["pages"].add(pg_name); rt_stats[key]["objects"] += 1
            area = poly.get("area", 0) or 0
            if area <= 0 and poly.get("pts") and pts_per_m7 > 0:
                area = _poly_area_pt2(poly["pts"]) / (pts_per_m7 ** 2)
            rt_stats[key]["area"] += area
        for op in pg_data.get("openings", []):
            if not op.get("closed"):
                continue
            sem = op.get("semanticTag") or _semantic_tag("opening", op)
            meta = _get_meta(op, sem)
            key = (meta["reportTarget"] or "Unclassified", meta["objectCategory"] or "annotation", meta["countingRule"] or "reference")
            if key not in rt_stats:
                rt_stats[key] = {"pages": set(), "objects": 0, "area": 0.0, "length": 0.0, "parking": 0}
            rt_stats[key]["pages"].add(pg_name); rt_stats[key]["objects"] += 1
            area = op.get("area", 0) or 0
            if area <= 0 and op.get("pts") and pts_per_m7 > 0:
                area = _poly_area_pt2(op["pts"]) / (pts_per_m7 ** 2)
            rt_stats[key]["area"] += area
        for ln in pg_data.get("lines", []):
            sem = ln.get("semanticTag") or _semantic_tag("line", ln)
            meta = _get_meta(ln, sem)
            key = (meta["reportTarget"] or "Unclassified", meta["objectCategory"] or "annotation", meta["countingRule"] or "reference")
            if key not in rt_stats:
                rt_stats[key] = {"pages": set(), "objects": 0, "area": 0.0, "length": 0.0, "parking": 0}
            rt_stats[key]["pages"].add(pg_name); rt_stats[key]["objects"] += 1
            if pts_per_m7 > 0:
                rt_stats[key]["length"] += _line_length_pt(_line_points(ln)) / pts_per_m7
        for ref in pg_data.get("refs", []):
            sem = ref.get("semanticTag") or _semantic_tag("ref", ref)
            meta = _get_meta(ref, sem)
            key = (meta["reportTarget"] or "Unclassified", meta["objectCategory"] or "annotation", meta["countingRule"] or "reference")
            if key not in rt_stats:
                rt_stats[key] = {"pages": set(), "objects": 0, "area": 0.0, "length": 0.0, "parking": 0}
            rt_stats[key]["pages"].add(pg_name); rt_stats[key]["objects"] += 1
            if pts_per_m7 > 0:
                rt_stats[key]["length"] += _line_length_pt(_line_points(ref)) / pts_per_m7
        for park in pg_data.get("parking", []):
            sem = park.get("semanticTag") or _semantic_tag("parking", park)
            meta = _get_meta(park, sem)
            key = (meta["reportTarget"] or "Unclassified", meta["objectCategory"] or "annotation", meta["countingRule"] or "reference")
            if key not in rt_stats:
                rt_stats[key] = {"pages": set(), "objects": 0, "area": 0.0, "length": 0.0, "parking": 0}
            rt_stats[key]["pages"].add(pg_name); rt_stats[key]["objects"] += 1
            rt_stats[key]["parking"] += park.get("count") or 1
    sorted_keys = sorted(rt_stats.keys(), key=lambda k: (RT_ORDER.index(k[0]) if k[0] in RT_ORDER else 99, k[1], k[2]))
    for key in sorted_keys:
        rt, cat, cr = key
        stats = rt_stats[key]
        ws7.write(r7, 0, rt, fmt_cell)
        ws7.write(r7, 1, cat, fmt_cell)
        ws7.write(r7, 2, cr, fmt_cell)
        ws7.write(r7, 3, ", ".join(sorted(stats["pages"])), fmt_note)
        ws7.write(r7, 4, stats["objects"], fmt_cell)
        ws7.write(r7, 5, round(stats["area"], 2) if stats["area"] > 0 else "", fmt_num if stats["area"] > 0 else fmt_cell)
        ws7.write(r7, 6, round(stats["length"], 2) if stats["length"] > 0 else "", fmt_num if stats["length"] > 0 else fmt_cell)
        ws7.write(r7, 7, stats["parking"] if stats["parking"] > 0 else "", fmt_cell)
        r7 += 1
    if rt_stats:
        ws7.write(r7, 0, "รวม", fmt_hdr)
        for c in range(1, 4):
            ws7.write(r7, c, "", fmt_cell)
        ws7.write(r7, 4, sum(s["objects"] for s in rt_stats.values()), fmt_total)
        tot_area = sum(s["area"] for s in rt_stats.values())
        tot_len = sum(s["length"] for s in rt_stats.values())
        tot_park = sum(s["parking"] for s in rt_stats.values())
        ws7.write(r7, 5, round(tot_area, 2) if tot_area > 0 else "", fmt_total if tot_area > 0 else fmt_cell)
        ws7.write(r7, 6, round(tot_len, 2) if tot_len > 0 else "", fmt_total if tot_len > 0 else fmt_cell)
        ws7.write(r7, 7, tot_park if tot_park > 0 else "", fmt_total if tot_park > 0 else fmt_cell)

    wb.close(); buf.seek(0)
    safe = pdf_name.replace("/", "_").replace("\\", "_").replace(".pdf", "")
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{safe}_report.xlsx"'})


# Load UI from external file (hot-reload on change)
UI_PATH = Path(__file__).resolve().parent / "ui.html"
_HTML_CACHE = None
_HTML_MTIME = 0

def _load_html():
    global _HTML_CACHE, _HTML_MTIME
    try:
        mtime = os.path.getmtime(UI_PATH)
        if mtime > _HTML_MTIME:
            with open(UI_PATH, "r", encoding="utf-8") as f:
                _HTML_CACHE = f.read()
            _HTML_MTIME = mtime
    except Exception:
        pass
    return _HTML_CACHE or "<h1>ui.html not found</h1>"

HTML = _load_html()  # initial load


@app.get("/")
def root():
    return HTMLResponse(_load_html())

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
