"""
server.py — PDF Scale Prototype Backend v3
รัน: python server.py
เปิด: http://localhost:8000
"""
import io, math, re, json, tempfile
from typing import Optional

import fitz
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, Response

app = FastAPI()

SESSION: dict = {}
PT_PER_MM = 72 / 25.4
SCALE_RE  = re.compile(r'1\s*[:/]\s*(\d{2,5})', re.IGNORECASE)
SNAP_GRID = 25   # PDF pt — 1 snap per cell


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
            "pts_per_m": round((1000 / N) * PT_PER_MM, 4)}


def extract_snaps_typed(drawings: list, max_snaps=2000, max_lines=2000):
    """Returns (snaps, lines).
    snaps: [{x,y,t}]  t = ep|mp|ct
    lines: [[x0,y0,x1,y1], ...]  for NL/IX client-side snap
    """
    ep_grid, mp_grid, ct_grid = {}, {}, {}
    lines_out = []

    for d in drawings:
        for item in d["items"]:
            op = item[0]
            if op == "l":
                p1, p2 = item[1], item[2]
                # EP – endpoints
                for pt in [p1, p2]:
                    k = (int(pt.x / SNAP_GRID), int(pt.y / SNAP_GRID))
                    if k not in ep_grid:
                        ep_grid[k] = (round(pt.x, 1), round(pt.y, 1))
                # MP – midpoint
                mx, my = (p1.x + p2.x) / 2, (p1.y + p2.y) / 2
                k = (int(mx / SNAP_GRID), int(my / SNAP_GRID))
                if k not in mp_grid:
                    mp_grid[k] = (round(mx, 1), round(my, 1))
                # lines for NL/IX
                if len(lines_out) < max_lines:
                    lines_out.append([round(p1.x,1), round(p1.y,1),
                                      round(p2.x,1), round(p2.y,1)])
            elif op == "re":
                r = item[1]
                # EP – corners
                for pt in [(r.x0,r.y0),(r.x1,r.y0),(r.x1,r.y1),(r.x0,r.y1)]:
                    k = (int(pt[0] / SNAP_GRID), int(pt[1] / SNAP_GRID))
                    if k not in ep_grid:
                        ep_grid[k] = (round(pt[0],1), round(pt[1],1))
                # CT – rectangle center
                cx2, cy2 = (r.x0+r.x1)/2, (r.y0+r.y1)/2
                k = (int(cx2 / SNAP_GRID), int(cy2 / SNAP_GRID))
                if k not in ct_grid:
                    ct_grid[k] = (round(cx2,1), round(cy2,1))
                # edges as lines
                if len(lines_out) < max_lines:
                    for seg in [(r.x0,r.y0,r.x1,r.y0),(r.x1,r.y0,r.x1,r.y1),
                                (r.x1,r.y1,r.x0,r.y1),(r.x0,r.y1,r.x0,r.y0)]:
                        lines_out.append([round(v,1) for v in seg])

    snaps  = [{"x":v[0],"y":v[1],"t":"ep"} for v in list(ep_grid.values())[:max_snaps//2]]
    snaps += [{"x":v[0],"y":v[1],"t":"mp"} for v in list(mp_grid.values())[:max_snaps//4]]
    snaps += [{"x":v[0],"y":v[1],"t":"ct"} for v in list(ct_grid.values())[:max_snaps//4]]
    return snaps, lines_out[:max_lines]


def _rotate_snaps(snaps, rot, W, H):
    out = []
    for s in snaps:
        x, y = s["x"], s["y"]
        if rot == 90:    x, y = H - y, x
        elif rot == 180: x, y = W - x, H - y
        elif rot == 270: x, y = y, W - x
        out.append({"x": round(x,1), "y": round(y,1), "t": s["t"]})
    return out


def _rotate_lines(lines, rot, W, H):
    out = []
    for l in lines:
        x0,y0,x1,y1 = l
        if rot == 90:
            x0,y0 = H-y0,x0;  x1,y1 = H-y1,x1
        elif rot == 180:
            x0,y0 = W-x0,H-y0; x1,y1 = W-x1,H-y1
        elif rot == 270:
            x0,y0 = y0,W-x0;  x1,y1 = y1,W-x1
        out.append([round(x0,1),round(y0,1),round(x1,1),round(y1,1)])
    return out


# ══════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    tmp  = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(data); tmp.close()
    doc  = fitz.open(tmp.name)
    SESSION.clear()
    SESSION["doc"]        = doc
    SESSION["path"]       = tmp.name
    SESSION["page_cache"] = {}
    return {"pages": len(doc), "name": file.filename}


@app.get("/page/{n}")
def get_page(n: int, scale: float = 1.5, rot: int = 0):
    doc = SESSION.get("doc")
    if not doc: return JSONResponse({"error":"no file"}, 400)
    mat = fitz.Matrix(scale, scale).prerotate(rot)
    pix = doc[n-1].get_pixmap(matrix=mat)
    return StreamingResponse(io.BytesIO(pix.tobytes("jpeg", jpg_quality=88)),
                             media_type="image/jpeg")


@app.get("/thumb/{n}")
def get_thumb(n: int, rot: int = 0):
    doc = SESSION.get("doc")
    if not doc: return JSONResponse({"error":"no file"}, 400)
    mat = fitz.Matrix(0.18, 0.18).prerotate(rot)
    pix = doc[n-1].get_pixmap(matrix=mat)
    return StreamingResponse(io.BytesIO(pix.tobytes("jpeg", jpg_quality=70)),
                             media_type="image/jpeg")


@app.get("/analyse/{n}")
def analyse(n: int, rot: int = 0):
    doc = SESSION.get("doc")
    if not doc: return JSONResponse({"error":"no file"}, 400)
    cache = SESSION.setdefault("page_cache", {})
    key   = (n, rot)
    if key in cache:
        return Response(cache[key], media_type="application/json")

    page   = doc[n-1]
    orig_W = page.rect.width
    orig_H = page.rect.height

    drawings          = page.get_drawings()          # called ONCE
    scale             = detect_scale(page)
    snaps, lines      = extract_snaps_typed(drawings)

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
              "snaps":snaps, "lines":lines, "render_scale":1.5}
    # cache pre-serialized JSON bytes — avoids re-serializing on every request
    result_bytes = json.dumps(result, separators=(",",":")).encode()
    cache[key] = result_bytes
    return Response(result_bytes, media_type="application/json")





# ══════════════════════════════════════════════════════
# Frontend
# ══════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PDF Scale</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#1c1c1e;color:#e5e5e7;
     height:100vh;display:flex;flex-direction:column;overflow:hidden}
#topbar{background:#2c2c2e;padding:6px 12px;display:flex;align-items:center;
        gap:6px;border-bottom:1px solid #3a3a3c;flex-shrink:0;flex-wrap:wrap}
#topbar h1{font-size:13px;font-weight:700;white-space:nowrap}
#upload-btn{background:#0a84ff;color:#fff;border:none;border-radius:7px;
            padding:5px 13px;font-size:12px;font-weight:600;cursor:pointer}
#upload-btn:hover{background:#0071e3}
#file-input{display:none}
.sep{width:1px;height:22px;background:#3a3a3c;flex-shrink:0}
.tb-btn{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);
        color:#e5e5e7;border-radius:7px;padding:4px 10px;font-size:12px;
        cursor:pointer;white-space:nowrap}
.tb-btn:hover{background:rgba(255,255,255,.14)}
.tb-btn.active{background:#0a84ff;border-color:#0a84ff;color:#fff}
.tb-btn.danger{background:rgba(255,59,48,.15);border-color:rgba(255,59,48,.3);color:#ff453a}
.tb-btn.danger:hover{background:rgba(255,59,48,.28)}
/* snap toggles */
#snap-bar{display:flex;align-items:center;gap:4px;flex-wrap:nowrap}
#snap-bar span{font-size:11px;color:#636366;margin-right:2px}
.sn-btn{border:1px solid rgba(255,255,255,.12);border-radius:20px;
        padding:2px 8px;font-size:11px;font-weight:600;cursor:pointer;
        background:rgba(255,255,255,.05);color:#636366;transition:all .15s}
.sn-btn:hover{background:rgba(255,255,255,.1)}
.sn-btn.on{color:#fff}
.sn-ep.on{background:#ffd60a33;border-color:#ffd60a;color:#ffd60a}
.sn-mp.on{background:#ff950033;border-color:#ff9500;color:#ff9500}
.sn-ct.on{background:#0a84ff33;border-color:#0a84ff;color:#0a84ff}
.sn-nl.on{background:#30d15833;border-color:#30d158;color:#30d158}
.sn-ix.on{background:#ff453a33;border-color:#ff453a;color:#ff453a}
.sn-off.on{background:#48484a;border-color:#636366;color:#e5e5e7}
#scale-badge{font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;
             background:rgba(255,69,58,.2);color:#ff453a;border:1px solid rgba(255,69,58,.3)}
#scale-badge.ok{background:rgba(48,209,88,.15);color:#30d158;border-color:rgba(48,209,88,.3)}
#status{font-size:11px;color:#98989f;overflow:hidden;text-overflow:ellipsis;
        max-width:240px;white-space:nowrap}
#body{display:flex;flex:1;overflow:hidden}
#thumb-strip{width:130px;min-width:130px;background:#2c2c2e;
             border-right:1px solid #3a3a3c;overflow-y:auto;flex-shrink:0}
#thumb-strip::-webkit-scrollbar{width:4px}
#thumb-strip::-webkit-scrollbar-thumb{background:#48484a;border-radius:2px}
.thumb-item{padding:6px;cursor:pointer;border-bottom:1px solid #3a3a3c;
            display:flex;flex-direction:column;align-items:center;gap:3px}
.thumb-item:hover{background:rgba(255,255,255,.06)}
.thumb-item.active{background:rgba(10,132,255,.2);border-left:3px solid #0a84ff}
.thumb-item img{width:100%;border-radius:3px;background:#333}
.thumb-item span{font-size:10px;color:#98989f}
.thumb-item.active span{color:#e5e5e7}
.thumb-scale{font-size:9px;color:#30d158;font-weight:600}
.thumb-scale.none{color:#ff453a}
.thumb-dot{font-size:9px;color:#0a84ff}
#workspace{flex:1;overflow:hidden;position:relative;background:#111;cursor:grab}
#workspace.drawing{cursor:crosshair}
#cc{position:absolute;top:0;left:0;transform-origin:0 0}
canvas{display:block}
#infobar{background:#2c2c2e;border-top:1px solid #3a3a3c;padding:5px 14px;
         font-size:11px;display:flex;gap:16px;align-items:center;flex-shrink:0}
#infobar span{color:#98989f}#infobar b{color:#e5e5e7}
#measure-result{color:#ffd60a;font-weight:600;margin-left:auto}
/* snap cursor */
#snap-cur{position:fixed;width:14px;height:14px;border:2px solid #ffd60a;
          border-radius:50%;transform:translate(-50%,-50%);
          pointer-events:none;display:none;z-index:200;
          box-shadow:0 0 6px rgba(255,214,10,.5);transition:border-color .1s}
#snap-lbl{position:fixed;font-size:9px;font-weight:700;
          pointer-events:none;display:none;z-index:201;
          background:rgba(0,0,0,.7);padding:1px 4px;border-radius:3px}
/* panels */
.panel{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);
       background:#2c2c2e;border:1px solid #48484a;border-radius:12px;
       padding:20px 24px;z-index:500;display:none;min-width:280px;
       box-shadow:0 8px 32px rgba(0,0,0,.6)}
.panel h3{font-size:13px;margin-bottom:12px}
.panel label{font-size:12px;color:#98989f;display:block;margin-bottom:6px}
.panel-input{width:100%;background:#1c1c1e;border:1px solid #48484a;border-radius:7px;
             color:#e5e5e7;font-size:14px;padding:7px 10px;outline:none;margin-bottom:14px}
.panel-input:focus{border-color:#0a84ff}
.panel-row{display:flex;gap:8px}
.btn-ok{flex:1;background:#0a84ff;color:#fff;border:none;border-radius:7px;
        padding:7px;font-size:12px;font-weight:600;cursor:pointer}
.btn-ok:hover{background:#0071e3}
.btn-cancel{background:rgba(255,255,255,.08);color:#e5e5e7;
            border:1px solid rgba(255,255,255,.1);border-radius:7px;
            padding:7px 14px;font-size:12px;cursor:pointer}
/* context menu */
#ctx-menu{position:fixed;background:#2c2c2e;border:1px solid #48484a;
          border-radius:9px;z-index:600;display:none;min-width:180px;
          box-shadow:0 4px 20px rgba(0,0,0,.6);overflow:hidden}
.ctx-item{padding:9px 14px;font-size:12px;cursor:pointer;color:#e5e5e7}
.ctx-item:hover{background:rgba(255,255,255,.1)}
.ctx-item.del{color:#ff453a}
.ctx-sep{height:1px;background:#3a3a3c}
.ctx-color-row{padding:7px 14px;display:flex;align-items:center;gap:6px}
.ctx-color-row span{font-size:11px;color:#98989f;white-space:nowrap}
#ctx-inp-color{width:26px;height:22px;border:none;padding:0;cursor:pointer;
               border-radius:4px;background:none;flex-shrink:0}
#ctx-inp-opacity{width:56px;accent-color:#0a84ff;cursor:pointer}
#ctx-opacity-val{font-size:11px;color:#e5e5e7;min-width:28px}
/* inline color controls */
#color-bar{display:flex;align-items:center;gap:6px}
#color-bar span{font-size:11px;color:#636366;white-space:nowrap}
#inp-color{width:28px;height:26px;border:none;padding:0;cursor:pointer;
           border-radius:5px;background:none;overflow:hidden;flex-shrink:0}
#inp-opacity{width:64px;accent-color:#0a84ff;cursor:pointer}
#opacity-val{font-size:11px;color:#e5e5e7;min-width:28px}
/* zoom controls */
#zoom-bar{display:flex;align-items:center;gap:4px}
#zoom-val{font-size:11px;color:#e5e5e7;min-width:36px;text-align:center}
/* area type in name panel */
.atype-row{display:flex;gap:8px;margin-bottom:14px}
.atype-btn{flex:1;padding:6px 4px;font-size:12px;font-weight:600;cursor:pointer;
           border-radius:7px;border:1px solid rgba(255,255,255,.1);
           background:rgba(255,255,255,.05);color:#98989f;text-align:center}
.atype-btn.sel{background:rgba(10,132,255,.18);border-color:#0a84ff;color:#0a84ff}
</style>
</head>
<body>
<div id="topbar">
  <h1>📐 PDF Scale</h1>
  <label id="upload-btn">📂 เปิด PDF<input id="file-input" type="file" accept=".pdf"></label>
  <div class="sep"></div>
  <button class="tb-btn" id="btn-prev" onclick="if(curPage>1)loadPage(curPage-1)" title="หน้าก่อน (←)">◀</button>
  <span id="page-lbl" style="font-size:11px;color:#e5e5e7;min-width:48px;text-align:center">— / —</span>
  <button class="tb-btn" id="btn-next" onclick="if(curPage<totalPages)loadPage(curPage+1)" title="หน้าถัดไป (→)">▶</button>
  <div class="sep"></div>
  <button class="tb-btn active" id="btn-pan"   onclick="setMode('pan')">✋ Pan</button>
  <button class="tb-btn"        id="btn-sel"   onclick="setMode('sel')">↖ เลือก</button>
  <button class="tb-btn"        id="btn-dist"  onclick="setMode('dist')">📏 ระยะ</button>
  <button class="tb-btn"        id="btn-area"  onclick="setMode('area')">⬡ พื้นที่</button>
  <button class="tb-btn"        id="btn-calib" onclick="setMode('calib')">📐 สอบเทียบ</button>
  <button class="tb-btn danger" onclick="clearMeasures()">🗑</button>
  <div class="sep"></div>
  <div id="color-bar">
    <span>สี</span>
    <input type="color" id="inp-color" value="#30d158" oninput="applyColor(this.value)" title="สี">
    <input type="range" id="inp-opacity" min="10" max="100" value="85" oninput="applyOpacity(+this.value)" title="ความโปร่ง">
    <span id="opacity-val">85%</span>
  </div>
  <div class="sep"></div>
  <div id="zoom-bar">
    <button class="tb-btn" onclick="adjustZoom(1.3)" title="ซูมเข้า">+</button>
    <span id="zoom-val">100%</span>
    <button class="tb-btn" onclick="adjustZoom(1/1.3)" title="ซูมออก">−</button>
    <button class="tb-btn" onclick="fitToWindow()" title="Fit">⊞</button>
  </div>
  <div class="sep"></div>
  <button class="tb-btn" onclick="rotatePage(-90)">↺</button>
  <button class="tb-btn" onclick="rotatePage(90)">↻</button>
  <span id="rot-badge" style="font-size:11px;color:#98989f;display:none"></span>
  <div class="sep"></div>
  <div id="snap-bar">
    <span>Snap:</span>
    <button class="sn-btn sn-ep on"  id="sn-ep"  onclick="toggleSnap('ep')">EP</button>
    <button class="sn-btn sn-mp on"  id="sn-mp"  onclick="toggleSnap('mp')">MP</button>
    <button class="sn-btn sn-ct on"  id="sn-ct"  onclick="toggleSnap('ct')">CT</button>
    <button class="sn-btn sn-nl"     id="sn-nl"  onclick="toggleSnap('nl')">NL</button>
    <button class="sn-btn sn-ix"     id="sn-ix"  onclick="toggleSnap('ix')">IX</button>
    <button class="sn-btn sn-off"    id="sn-off" onclick="toggleSnap('off')">—</button>
  </div>
  <div class="sep"></div>
  <span id="scale-badge">ยังไม่มีไฟล์</span>
  <div class="sep"></div>
  <button class="tb-btn" onclick="exportCSV()">⬇ CSV</button>
  <button class="tb-btn" onclick="exportJSON()">⬇ JSON</button>
  <span id="status" style="margin-left:4px"></span>
</div>
<div id="body">
  <div id="thumb-strip"></div>
  <div id="workspace">
    <div id="cc"><canvas id="canvas"></canvas></div>
  </div>
</div>
<div id="infobar">
  <span>Mode: <b id="lbl-mode">Pan</b></span>
  <span>Scale: <b id="lbl-scale">—</b></span>
  <span>Snap: <b id="lbl-snaps">—</b></span>
  <span id="measure-result"></span>
</div>
<div id="snap-cur"></div>
<div id="snap-lbl"></div>

<div id="calib-panel" class="panel">
  <h3>📐 สอบเทียบสเกล</h3>
  <div id="calib-line-info" style="font-size:11px;color:#98989f;margin-bottom:14px">คลิก 2 จุด บนเส้นที่ทราบระยะจริง</div>
  <label>ระยะจริงระหว่าง 2 จุด (เมตร)</label>
  <input id="calib-input" class="panel-input" type="number" step="0.01" min="0.01" placeholder="เช่น 7.71">
  <div class="panel-row">
    <button class="btn-ok" onclick="finishCalib()">ยืนยัน</button>
    <button class="btn-cancel" onclick="cancelCalib()">ยกเลิก</button>
  </div>
</div>

<div id="name-panel" class="panel">
  <h3 id="name-panel-title">ชื่อพื้นที่</h3>
  <div class="atype-row" id="atype-row" style="display:none">
    <button class="atype-btn sel" id="atype-room" onclick="setAType('room')">🏠 ห้อง/อาคาร</button>
    <button class="atype-btn"    id="atype-land" onclick="setAType('land')">🌿 ที่ดิน</button>
  </div>
  <label>ชื่อพื้นที่ / ห้อง</label>
  <input id="name-input" class="panel-input" type="text" placeholder="เช่น ห้องนอน 1">
  <div class="panel-row">
    <button class="btn-ok" onclick="finishName()">ตกลง</button>
    <button class="btn-cancel" onclick="cancelName()">ข้าม</button>
  </div>
</div>

<div id="ctx-menu">
  <div class="ctx-color-row">
    <span>สี</span>
    <input type="color" id="ctx-inp-color" oninput="ctxColor(this.value)" title="เปลี่ยนสี">
    <input type="range" id="ctx-inp-opacity" min="10" max="100" value="85" oninput="ctxOpacity(+this.value)" title="ความโปร่ง">
    <span id="ctx-opacity-val">85%</span>
  </div>
  <div class="ctx-sep"></div>
  <div class="ctx-item" id="ctx-rename" onclick="ctxRename()">✏️ เปลี่ยนชื่อ</div>
  <div class="ctx-sep"></div>
  <div class="ctx-item del" onclick="ctxDelete()">🗑 ลบ</div>
</div>

<script>
// ── state ──────────────────────────────────────────────
let totalPages=0, curPage=1, pageData=null;
const RS=1.5;
let zoom=1, panX=0, panY=0, mode="pan";
let mPts=[];           // in-progress — PDF pt (unrotated)
let mLines=[], mPolys=[];
let isPan=false, lastMx=0, lastMy=0;
let bgImg=null, snapTarget=null, snapTargetType=null;
let calibPts=[];       // canvas px
let pageRotations={};
let pageStore={};      // {page: {lines,polys,calibScale}}
let analyseCache={};
let nameCb=null;
let ctxTarget=null;
let snapModes={ep:true,mp:true,ct:true,nl:false,ix:false,off:false};
let curColor="#30d158", curOpacity=0.85, curAType="room";
let selItem=null;    // {type:'line'|'poly', idx}
let dragState=null;  // {type, idx, startPdf, origData}

const canvas    = document.getElementById("canvas");
const ctx       = canvas.getContext("2d");
const ws        = document.getElementById("workspace");
const cc        = document.getElementById("cc");
const snapCur   = document.getElementById("snap-cur");
const snapLbl   = document.getElementById("snap-lbl");
const ctxMenu   = document.getElementById("ctx-menu");
const namePanel = document.getElementById("name-panel");
const calibPanel= document.getElementById("calib-panel");

// snap type config
const SNAP_COLORS={ep:"#ffd60a",mp:"#ff9500",ct:"#5ac8fa",nl:"#30d158",ix:"#ff453a"};
const SNAP_LABELS={ep:"EP",mp:"MP",ct:"CT",nl:"NL",ix:"IX"};

// ── inline color controls ──────────────────────────────
function applyColor(c){
  curColor=c;
  // if item selected → update it live
  if(selItem){
    if(selItem.type==="line"&&mLines[selItem.idx]) mLines[selItem.idx].color=c;
    if(selItem.type==="poly"&&mPolys[selItem.idx]) mPolys[selItem.idx].color=c;
    redraw();
  }
}
function applyOpacity(v){
  curOpacity=v/100;
  document.getElementById("opacity-val").textContent=v+"%";
  if(selItem){
    if(selItem.type==="line"&&mLines[selItem.idx]) mLines[selItem.idx].opacity=curOpacity;
    if(selItem.type==="poly"&&mPolys[selItem.idx]) mPolys[selItem.idx].opacity=curOpacity;
    redraw();
  }
}
function syncColorBar(item){
  // update color bar to reflect selected item's color/opacity
  const obj=item?.type==="line"?mLines[item.idx]:item?.type==="poly"?mPolys[item.idx]:null;
  if(obj){
    const c=obj.color||curColor, o=Math.round((obj.opacity??curOpacity)*100);
    document.getElementById("inp-color").value=c.length===7?c:"#30d158";
    document.getElementById("inp-opacity").value=o;
    document.getElementById("opacity-val").textContent=o+"%";
  }
}
function hexAlpha(hex,a){
  const r=parseInt(hex.slice(1,3),16)||0;
  const g=parseInt(hex.slice(3,5),16)||0;
  const b=parseInt(hex.slice(5,7),16)||0;
  return`rgba(${r},${g},${b},${a})`;
}
document.addEventListener("click",()=>ctxMenu.style.display="none");

// ── zoom buttons ───────────────────────────────────────
function adjustZoom(factor){
  const W=ws.clientWidth,H=ws.clientHeight;
  const fx=W/2,fy=H/2;
  const nz=Math.max(0.08,Math.min(8,zoom*factor));
  panX=fx-(fx-panX)*(nz/zoom); panY=fy-(fy-panY)*(nz/zoom); zoom=nz; applyT();
  document.getElementById("zoom-val").textContent=Math.round(zoom*100)+"%";
}

// ── area type ──────────────────────────────────────────
function setAType(t){
  curAType=t;
  document.getElementById("atype-room").classList.toggle("sel",t==="room");
  document.getElementById("atype-land").classList.toggle("sel",t==="land");
}

// ── snap toggles ───────────────────────────────────────
function toggleSnap(t){
  if(t==="off"){
    snapModes.off=!snapModes.off;
  }else{
    snapModes[t]=!snapModes[t];
    if(snapModes[t]) snapModes.off=false;
  }
  updateSnapUI();
}
function updateSnapUI(){
  ["ep","mp","ct","nl","ix","off"].forEach(t=>{
    document.getElementById("sn-"+t).classList.toggle("on", snapModes[t]);
  });
  // auto-load lines if NL or IX just enabled
  if((snapModes.nl||snapModes.ix)&&pageData&&!pageData.lines?.length){
    fetchAnalyse(curPage,true);
  }
}

// ── coordinate transforms ──────────────────────────────
function origSize(){
  if(pageData?.size?.orig_w_pt)
    return{W:pageData.size.orig_w_pt, H:pageData.size.orig_h_pt};
  return{W:canvas.width/RS, H:canvas.height/RS};
}
function pdfToC(px,py){
  const {W,H}=origSize(), rot=getRot(curPage);
  if(rot===0)   return{x:px*RS,       y:py*RS};
  if(rot===90)  return{x:(H-py)*RS,   y:px*RS};
  if(rot===180) return{x:(W-px)*RS,   y:(H-py)*RS};
               return{x:py*RS,       y:(W-px)*RS};
}
function cToPdf(cx,cy){
  const {W,H}=origSize(), rot=getRot(curPage);
  if(rot===0)   return{x:cx/RS,       y:cy/RS};
  if(rot===90)  return{x:cy/RS,       y:H-cx/RS};
  if(rot===180) return{x:W-cx/RS,     y:H-cy/RS};
               return{x:W-cy/RS,     y:cx/RS};
}

// ── per-page store ─────────────────────────────────────
function getStore(n){
  if(!pageStore[n]) pageStore[n]={lines:[],polys:[],calibScale:null};
  return pageStore[n];
}
function saveCurrentPage(){
  const s=getStore(curPage);
  s.lines=[...mLines]; s.polys=[...mPolys];
  if(pageData?.scale?.calibrated) s.calibScale=pageData.scale;
}
function restorePage(n){
  const s=getStore(n);
  mLines=[...s.lines]; mPolys=[...s.polys];
  if(s.calibScale&&analyseCache[n]) analyseCache[n].scale=s.calibScale;
}

// ── rotation ───────────────────────────────────────────
function getRot(n){return pageRotations[n]||0;}
function rotatePage(delta){
  saveCurrentPage();
  const r=(getRot(curPage)+delta+360)%360;
  pageRotations[curPage]=r;
  const b=document.getElementById("rot-badge");
  b.style.display=r?"inline":"none"; b.textContent=r+"°";
  reloadPageImage();
}
function reloadPageImage(){
  const rot=getRot(curPage);
  const img=new Image();
  img.onload=()=>{bgImg=img;canvas.width=img.width;canvas.height=img.height;fitToWindow();redraw();};
  img.src=`/page/${curPage}?scale=${RS}&rot=${rot}&t=${Date.now()}`;
  fetchAnalyse(curPage);
}
function fetchAnalyse(n, forceLines=false){
  const rot=getRot(n);
  fetch(`/analyse/${n}?rot=${rot}`).then(r=>r.json()).then(d=>{
    if(pageStore[n]?.calibScale) d.scale=pageStore[n].calibScale;
    analyseCache[n]=d;
    if(n===curPage){pageData=d;updateAnalyseUI(d);}
    const ts=document.getElementById("ts-"+n);
    if(ts){
      if(d.scale){ts.textContent=d.scale.label;ts.className="thumb-scale";}
      else{ts.textContent="no scale";ts.className="thumb-scale none";}
    }
    setStatus((d.scale?.label??"ไม่พบ scale")+" · "+
              (d.snaps?.length||0)+" pts · "+
              (d.lines?.length||0)+" segs");
  });
}
function updateAnalyseUI(d){
  const badge=document.getElementById("scale-badge");
  const lblS=document.getElementById("lbl-scale");
  if(d.scale){badge.className="ok";badge.textContent=d.scale.label;lblS.textContent=d.scale.label;}
  else{badge.className="";badge.textContent="ไม่พบ scale";lblS.textContent="—";}
  document.getElementById("lbl-snaps").textContent=(d.snaps?.length||0)+" pts";
  redraw();
}

// ── upload ─────────────────────────────────────────────
document.getElementById("file-input").addEventListener("change",async e=>{
  const file=e.target.files[0]; if(!file)return;
  setStatus("กำลังโหลด…");
  pageStore={};analyseCache={};pageRotations={};
  const fd=new FormData();fd.append("file",file);
  const res=await fetch("/upload",{method:"POST",body:fd});
  const d=await res.json();
  totalPages=d.pages;
  buildThumbs(totalPages);
  await loadPage(1);
});

// ── thumbs ─────────────────────────────────────────────
function buildThumbs(n){
  const strip=document.getElementById("thumb-strip");strip.innerHTML="";
  for(let i=1;i<=n;i++){
    const div=document.createElement("div");
    div.className="thumb-item";div.id="th-"+i;
    div.innerHTML=`<img src="/thumb/${i}" loading="lazy"><span>หน้า ${i}</span><span class="thumb-scale none" id="ts-${i}">…</span><span class="thumb-dot" id="td-${i}"></span>`;
    div.addEventListener("click",()=>loadPage(i));
    strip.appendChild(div);
  }
}
function setThumbActive(n){
  document.querySelectorAll(".thumb-item").forEach(e=>e.classList.remove("active"));
  const el=document.getElementById("th-"+n);
  if(el){el.classList.add("active");el.scrollIntoView({block:"nearest"});}
}
function updateThumbDot(n){
  const el=document.getElementById("td-"+n);
  if(!el)return;
  const s=pageStore[n];
  const cnt=(s?.lines?.length||0)+(s?.polys?.filter(p=>p.closed).length||0);
  el.textContent=cnt?cnt+" รายการ":"";
}

// ── load page ──────────────────────────────────────────
async function loadPage(n){
  saveCurrentPage(); updateThumbDot(curPage);
  curPage=n; setThumbActive(n); setStatus("โหลดหน้า "+n+"…");
  document.getElementById("page-lbl").textContent=n+" / "+totalPages;
  document.getElementById("btn-prev").disabled=n<=1;
  document.getElementById("btn-next").disabled=n>=totalPages;
  mPts=[];calibPts=[];
  calibPanel.style.display="none";
  snapTarget=null;snapCur.style.display="none";snapLbl.style.display="none";
  document.getElementById("measure-result").textContent="";
  const rot=getRot(n);
  const b=document.getElementById("rot-badge");
  b.style.display=rot?"inline":"none"; b.textContent=rot?rot+"°":"";
  restorePage(n);
  await new Promise(res=>{
    const img=new Image();
    img.onload=()=>{bgImg=img;canvas.width=img.width;canvas.height=img.height;res();};
    img.src=`/page/${n}?scale=${RS}&rot=${rot}&t=${Date.now()}`;
  });
  fitToWindow();redraw();
  if(analyseCache[n]){pageData=analyseCache[n];updateAnalyseUI(pageData);}
  fetchAnalyse(n);
}

// ── fit ────────────────────────────────────────────────
function fitToWindow(){
  const W=ws.clientWidth,H=ws.clientHeight;
  zoom=Math.min((W-20)/canvas.width,(H-20)/canvas.height,2);
  panX=Math.round((W-canvas.width*zoom)/2);
  panY=Math.round((H-canvas.height*zoom)/2);
  applyT();
}
function applyT(){
  cc.style.transform=`translate(${panX}px,${panY}px) scale(${zoom})`;
  document.getElementById("zoom-val").textContent=Math.round(zoom*100)+"%";
}

// ── snap ───────────────────────────────────────────────
function snap(cx,cy){
  // cx,cy in canvas px
  if(snapModes.off||!pageData) return{x:cx,y:cy,t:null};
  const R=20/zoom;  // snap radius scales with zoom
  let best=null,bd=R*R,bt=null;

  const check=(sx,sy,t)=>{
    if(!snapModes[t])return;
    const d=(sx-cx)*(sx-cx)+(sy-cy)*(sy-cy);
    if(d<bd){bd=d;best={x:sx,y:sy};bt=t;}
  };

  // static snaps (ep, mp, ct) — in rotated PDF pt, convert to canvas px
  for(const s of (pageData.snaps||[])){
    check(s.x*RS, s.y*RS, s.t);
  }

  // NL — nearest on segment
  if(snapModes.nl){
    for(const l of (pageData.lines||[])){
      const p=nearestOnSeg(cx,cy, l[0]*RS,l[1]*RS, l[2]*RS,l[3]*RS);
      check(p.x,p.y,"nl");
    }
  }

  // IX — intersection of nearby line pairs
  if(snapModes.ix){
    const near=(pageData.lines||[]).filter(l=>{
      const mx=(l[0]+l[2])/2*RS, my=(l[1]+l[3])/2*RS;
      return Math.hypot(mx-cx,my-cy)<200;
    });
    for(let i=0;i<near.length;i++){
      for(let j=i+1;j<near.length;j++){
        const p=segIntersect(
          near[i][0]*RS,near[i][1]*RS,near[i][2]*RS,near[i][3]*RS,
          near[j][0]*RS,near[j][1]*RS,near[j][2]*RS,near[j][3]*RS);
        if(p) check(p.x,p.y,"ix");
      }
    }
  }

  return best?{...best,t:bt}:{x:cx,y:cy,t:null};
}

function nearestOnSeg(px,py,x0,y0,x1,y1){
  const dx=x1-x0,dy=y1-y0,len2=dx*dx+dy*dy;
  if(len2<0.01)return{x:x0,y:y0};
  const t=Math.max(0,Math.min(1,((px-x0)*dx+(py-y0)*dy)/len2));
  return{x:x0+t*dx,y:y0+t*dy};
}
function segIntersect(x0,y0,x1,y1,x2,y2,x3,y3){
  const d1x=x1-x0,d1y=y1-y0,d2x=x3-x2,d2y=y3-y2;
  const cross=d1x*d2y-d1y*d2x;
  if(Math.abs(cross)<0.5)return null;
  const dx=x2-x0,dy=y2-y0;
  const t=(dx*d2y-dy*d2x)/cross;
  const u=(dx*d1y-dy*d1x)/cross;
  if(t<-0.1||t>1.1||u<-0.1||u>1.1)return null;
  return{x:x0+t*d1x,y:y0+t*d1y};
}

// ── measure helpers ────────────────────────────────────
function ptsToM(p1,p2){
  if(!pageData?.scale)return null;
  return Math.hypot(p2.x-p1.x,p2.y-p1.y)/pageData.scale.pts_per_m;
}
function polyAreaM2(pts){
  if(!pageData?.scale)return null;
  let a=0;
  for(let i=0;i<pts.length;i++){
    const j=(i+1)%pts.length;
    a+=pts[i].x*pts[j].y-pts[j].x*pts[i].y;
  }
  return Math.abs(a)/(2*pageData.scale.pts_per_m**2);
}
function toRNW(m2){
  if(m2==null)return"";
  return`${Math.floor(m2/1600)}-${Math.floor((m2%1600)/400)}-${((m2%400)/4).toFixed(2)} ว.`;
}
let _id=0;
function nid(){return++_id;}

// ── redraw ─────────────────────────────────────────────
function redraw(){
  if(!bgImg)return;
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.drawImage(bgImg,0,0);

  const lw=Math.max(1, 2/zoom);  // line width stays ~2px on screen
  const ds=[8/zoom,4/zoom];       // dash stays ~8px on screen

  mLines.forEach(s=>{
    const a=pdfToC(s.x0,s.y0),b=pdfToC(s.x1,s.y1);
    const col=s.color||"#ffd60a", opa=s.opacity??0.85;
    ctx.save();ctx.globalAlpha=opa;
    ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);
    ctx.strokeStyle=col;ctx.lineWidth=lw;ctx.setLineDash(ds);ctx.stroke();
    ctx.restore();
    const lbl=s.dist!=null?s.dist.toFixed(2)+" ม.":s.ptDist.toFixed(1)+" pt";
    drawLbl((a.x+b.x)/2,(a.y+b.y)/2,lbl);
  });

  mPolys.forEach(poly=>{
    if(poly.pts.length<2)return;
    const cp=poly.pts.map(p=>pdfToC(p.x,p.y));
    const col=poly.color||"#30d158", opa=poly.opacity??0.85;
    ctx.save();ctx.globalAlpha=opa;
    ctx.beginPath();ctx.moveTo(cp[0].x,cp[0].y);
    cp.slice(1).forEach(p=>ctx.lineTo(p.x,p.y));
    if(poly.closed)ctx.closePath();
    ctx.strokeStyle=col;ctx.lineWidth=lw;ctx.stroke();
    if(poly.closed){ctx.fillStyle=hexAlpha(col,.08);ctx.fill();}
    ctx.restore();
    if(poly.closed){
      const cx2=cp.reduce((s,p)=>s+p.x,0)/cp.length;
      const cy2=cp.reduce((s,p)=>s+p.y,0)/cp.length;
      drawPolyLabel(cx2,cy2,poly.name,poly.area,poly.areaType);
    }
  });

  // in-progress pts
  if(mPts.length>0){
    const cp=mPts.map(p=>pdfToC(p.x,p.y));
    ctx.save();
    ctx.beginPath();ctx.moveTo(cp[0].x,cp[0].y);
    cp.slice(1).forEach(p=>ctx.lineTo(p.x,p.y));
    ctx.strokeStyle=curColor;ctx.lineWidth=lw;ctx.setLineDash([6/zoom,3/zoom]);ctx.stroke();ctx.restore();
    cp.forEach(p=>{
      ctx.beginPath();ctx.arc(p.x,p.y,4/zoom,0,Math.PI*2);
      ctx.fillStyle=curColor;ctx.fill();
    });
  }

  // calib
  calibPts.forEach(p=>{
    ctx.save();ctx.beginPath();ctx.arc(p.x,p.y,8,0,Math.PI*2);
    ctx.strokeStyle="#bf5af2";ctx.lineWidth=2;ctx.stroke();
    ctx.beginPath();ctx.arc(p.x,p.y,3,0,Math.PI*2);ctx.fillStyle="#bf5af2";ctx.fill();ctx.restore();
  });
  if(calibPts.length===2){
    ctx.save();ctx.beginPath();ctx.moveTo(calibPts[0].x,calibPts[0].y);
    ctx.lineTo(calibPts[1].x,calibPts[1].y);
    ctx.strokeStyle="#bf5af2";ctx.lineWidth=2;ctx.setLineDash([10,5]);ctx.stroke();ctx.restore();
  }

  // snap dot on canvas
  if(snapTarget&&mode!=="pan"&&mode!=="sel"){
    const col=SNAP_COLORS[snapTarget.t]||"#ffd60a";
    ctx.save();ctx.beginPath();ctx.arc(snapTarget.x,snapTarget.y,7,0,Math.PI*2);
    ctx.strokeStyle=col;ctx.lineWidth=2;ctx.stroke();
    ctx.beginPath();ctx.arc(snapTarget.x,snapTarget.y,2,0,Math.PI*2);ctx.fillStyle=col;ctx.fill();
    ctx.restore();
  }

  // selection highlight
  if(selItem&&mode==="sel") drawSelHighlight(selItem);
}

function drawSelHighlight(sel){
  const hw=6/zoom;  // handle half-width in canvas px
  ctx.save();
  ctx.strokeStyle="#fff";ctx.lineWidth=2/zoom;ctx.setLineDash([6/zoom,3/zoom]);
  if(sel.type==="line"&&mLines[sel.idx]){
    const s=mLines[sel.idx];
    const a=pdfToC(s.x0,s.y0),b=pdfToC(s.x1,s.y1);
    ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    // endpoint handles
    [a,b].forEach(p=>{
      ctx.fillStyle="#fff";ctx.fillRect(p.x-hw,p.y-hw,hw*2,hw*2);
      ctx.strokeStyle="#0a84ff";ctx.lineWidth=1.5/zoom;ctx.strokeRect(p.x-hw,p.y-hw,hw*2,hw*2);
    });
  }else if(sel.type==="poly"&&mPolys[sel.idx]){
    const poly=mPolys[sel.idx];
    const cp=poly.pts.map(p=>pdfToC(p.x,p.y));
    ctx.beginPath();ctx.moveTo(cp[0].x,cp[0].y);
    cp.slice(1).forEach(p=>ctx.lineTo(p.x,p.y));
    if(poly.closed)ctx.closePath();
    ctx.stroke();
    // vertex handles
    cp.forEach(p=>{
      ctx.fillStyle="#fff";ctx.fillRect(p.x-hw,p.y-hw,hw*2,hw*2);
      ctx.strokeStyle="#0a84ff";ctx.lineWidth=1.5/zoom;ctx.strokeRect(p.x-hw,p.y-hw,hw*2,hw*2);
    });
  }
  ctx.restore();
}

// Labels always appear ~13px on screen regardless of zoom
function lblFs(){return Math.max(10, Math.round(13/zoom));}

function drawLbl(x,y,t){
  const fs=lblFs(), pad=Math.round(3/zoom);
  ctx.save();ctx.font=`bold ${fs}px sans-serif`;
  const tw=ctx.measureText(t).width;
  ctx.fillStyle="rgba(0,0,0,.72)";
  ctx.fillRect(x-tw/2-pad, y-fs*0.85, tw+pad*2, fs*1.25);
  ctx.fillStyle="#ffffff";ctx.fillText(t,x-tw/2,y+fs*0.3);
  ctx.restore();
}
function drawPolyLabel(cx2,cy2,name,area,areaType){
  const fs=lblFs(), lh=Math.round(fs*1.4), pad=Math.round(4/zoom);
  ctx.save();ctx.font=`bold ${fs}px sans-serif`;
  const rows=[];
  if(name) rows.push({t:name,c:"#5ac8fa"});
  if(area!=null){
    rows.push({t:area.toFixed(2)+" ตร.ม.",c:"#ffffff"});
    if(areaType==="land") rows.push({t:toRNW(area),c:"#a8e6c0"});
  }
  if(!rows.length){ctx.restore();return;}
  const mw=Math.max(...rows.map(r=>ctx.measureText(r.t).width));
  const bh=rows.length*lh+pad*2;
  const bx=cx2-mw/2-pad, by=cy2-bh/2;
  ctx.fillStyle="rgba(0,0,0,.72)";
  ctx.beginPath();
  if(ctx.roundRect) ctx.roundRect(bx,by,mw+pad*2,bh,Math.round(4/zoom));
  else ctx.rect(bx,by,mw+pad*2,bh);
  ctx.fill();
  rows.forEach((r,i)=>{
    const tw=ctx.measureText(r.t).width;
    ctx.fillStyle=r.c;
    ctx.fillText(r.t,cx2-tw/2,by+pad+fs*0.9+i*lh);
  });
  ctx.restore();
}

// ── screen ─────────────────────────────────────────────
function cXY(e){
  const r=canvas.getBoundingClientRect();
  return{x:(e.clientX-r.left)*(canvas.width/r.width),
         y:(e.clientY-r.top)*(canvas.height/r.height)};
}

// ── hit detection ─────────────────────────────────────
function distToSeg(px,py,x0,y0,x1,y1){
  const dx=x1-x0,dy=y1-y0,len2=dx*dx+dy*dy;
  if(len2<0.01) return Math.hypot(px-x0,py-y0);
  const t=Math.max(0,Math.min(1,((px-x0)*dx+(py-y0)*dy)/len2));
  return Math.hypot(px-x0-t*dx,py-y0-t*dy);
}
function ptInPoly(px,py,pts){
  let inside=false;
  for(let i=0,j=pts.length-1;i<pts.length;j=i++){
    const xi=pts[i].x,yi=pts[i].y,xj=pts[j].x,yj=pts[j].y;
    if(((yi>py)!==(yj>py))&&(px<(xj-xi)*(py-yi)/(yj-yi)+xi)) inside=!inside;
  }
  return inside;
}
function hitTest(cx,cy){
  // cx,cy in canvas px — returns {type,idx} or null
  const THR=12/zoom;
  // check polys first (filled area easier to click)
  for(let i=mPolys.length-1;i>=0;i--){
    const poly=mPolys[i];
    if(!poly.closed) continue;
    const cp=poly.pts.map(p=>pdfToC(p.x,p.y));
    if(ptInPoly(cx,cy,cp)) return{type:"poly",idx:i};
    // also check border
    for(let j=0;j<cp.length;j++){
      const nj=(j+1)%cp.length;
      if(distToSeg(cx,cy,cp[j].x,cp[j].y,cp[nj].x,cp[nj].y)<THR)
        return{type:"poly",idx:i};
    }
  }
  // check lines
  for(let i=mLines.length-1;i>=0;i--){
    const s=mLines[i];
    const a=pdfToC(s.x0,s.y0),b=pdfToC(s.x1,s.y1);
    if(distToSeg(cx,cy,a.x,a.y,b.x,b.y)<THR) return{type:"line",idx:i};
  }
  return null;
}

// ── click ──────────────────────────────────────────────
ws.addEventListener("mousedown",e=>{
  if(e.button===2)return;
  // select mode
  if(mode==="sel"){
    const {x:cx,y:cy}=cXY(e);
    const hit=hitTest(cx,cy);
    if(hit){
      selItem=hit;
      syncColorBar(hit);
      // start drag — store starting PDF pt and original data
      const pStart=cToPdf(cx,cy);
      let origData;
      if(hit.type==="line"){
        const s=mLines[hit.idx];
        origData={x0:s.x0,y0:s.y0,x1:s.x1,y1:s.y1};
      }else{
        origData=mPolys[hit.idx].pts.map(p=>({...p}));
      }
      dragState={...hit,startPdf:pStart,origData};
      ws.style.cursor="move";
    }else{
      selItem=null;dragState=null;
    }
    redraw();return;
  }
  if(mode==="pan"||e.button===1){
    isPan=true;lastMx=e.clientX;lastMy=e.clientY;ws.style.cursor="grabbing";return;
  }
  const {x:cx,y:cy}=cXY(e);
  const sc=snap(cx,cy);
  const p=cToPdf(sc.x,sc.y);  // store as unrotated PDF pt

  if(mode==="dist"){
    mPts.push(p);
    if(mPts.length===1){
      document.getElementById("measure-result").textContent="• คลิกจุดที่ 2";
    }else{
      const dist=ptsToM(mPts[0],mPts[1]);
      const ptD=Math.hypot(mPts[1].x-mPts[0].x,mPts[1].y-mPts[0].y);
      const base={x0:mPts[0].x,y0:mPts[0].y,x1:mPts[1].x,y1:mPts[1].y,
                  ptDist:ptD,id:nid(),color:curColor,opacity:curOpacity};
      if(dist!=null){
        mLines.push({...base,dist});
        document.getElementById("measure-result").textContent="📏 "+dist.toFixed(3)+" ม.";
      }else{
        mLines.push({...base,dist:null});
        document.getElementById("measure-result").textContent="📏 "+ptD.toFixed(1)+" pt (สอบเทียบก่อน)";
      }
      mPts=[];
    }
  }else if(mode==="area"){
    if(mPts.length>=2){
      const c0=pdfToC(mPts[0].x,mPts[0].y);
      if(Math.hypot(sc.x-c0.x,sc.y-c0.y)<18){
        const area=polyAreaM2(mPts);
        curAType="room"; setAType("room");
        const poly={pts:[...mPts],closed:true,area,name:"",areaType:"room",
                    id:nid(),color:curColor,opacity:curOpacity};
        mPolys.push(poly);
        if(area!=null)
          document.getElementById("measure-result").textContent=
            `⬡ ${area.toFixed(2)} ตร.ม.`;
        mPts=[];
        openNamePanel("ชื่อพื้นที่",(nm,at)=>{poly.name=nm;poly.areaType=at;redraw();},
          "", true);
        return;
      }
    }
    mPts.push(p);
  }else if(mode==="calib"){
    calibPts.push({x:sc.x,y:sc.y});
    if(calibPts.length===1){setStatus("คลิกจุดที่ 2…");redraw();return;}
    if(calibPts.length===2){
      const dx=(calibPts[1].x-calibPts[0].x)/RS,dy=(calibPts[1].y-calibPts[0].y)/RS;
      document.getElementById("calib-line-info").textContent=
        "เส้น "+Math.hypot(dx,dy).toFixed(1)+" pt · ป้อนระยะจริง:";
      calibPanel.style.display="block";
      document.getElementById("calib-input").value="";
      document.getElementById("calib-input").focus();
    }
    return;
  }
  redraw();
});

// ── context menu ───────────────────────────────────────
ws.addEventListener("contextmenu",e=>{
  e.preventDefault();
  const {x:cx,y:cy}=cXY(e);
  // in select mode, prefer the already-selected item if click is near it
  const hit=(mode==="sel"&&selItem)?selItem:findNearest(cx,cy,40);
  if(!hit){ctxMenu.style.display="none";return;}
  ctxTarget=hit;
  if(mode==="sel"){selItem=hit;redraw();}
  // sync color row to item's current color
  const ctxObj=hit.type==="line"?mLines[hit.idx]:mPolys[hit.idx];
  if(ctxObj){
    const c=ctxObj.color||curColor, o=Math.round((ctxObj.opacity??curOpacity)*100);
    document.getElementById("ctx-inp-color").value=c.length===7?c:"#30d158";
    document.getElementById("ctx-inp-opacity").value=o;
    document.getElementById("ctx-opacity-val").textContent=o+"%";
  }
  document.getElementById("ctx-rename").style.display=hit.type==="poly"?"block":"none";
  // position menu (flip if near bottom/right edge)
  const mw=200, mh=150;
  const lx=e.clientX+mw>innerWidth?e.clientX-mw:e.clientX;
  const ly=e.clientY+mh>innerHeight?e.clientY-mh:e.clientY;
  ctxMenu.style.left=lx+"px";ctxMenu.style.top=ly+"px";
  ctxMenu.style.display="block";
});
// stop ctx-menu clicks from closing the menu via document listener
ctxMenu.addEventListener("click",e=>e.stopPropagation());

function findNearest(cx,cy,r){
  let best=null,bd=r;
  mLines.forEach((s,i)=>{
    const a=pdfToC(s.x0,s.y0),b=pdfToC(s.x1,s.y1);
    const d=Math.hypot(cx-(a.x+b.x)/2, cy-(a.y+b.y)/2);
    if(d<bd){bd=d;best={type:"line",idx:i};}
  });
  mPolys.forEach((poly,i)=>{
    if(!poly.closed)return;
    const cp=poly.pts.map(p=>pdfToC(p.x,p.y));
    const mx=cp.reduce((s,p)=>s+p.x,0)/cp.length;
    const my=cp.reduce((s,p)=>s+p.y,0)/cp.length;
    const d=Math.hypot(cx-mx,cy-my);
    if(d<bd){bd=d;best={type:"poly",idx:i};}
  });
  return best;
}
function ctxColor(c){
  if(!ctxTarget)return;
  const obj=ctxTarget.type==="line"?mLines[ctxTarget.idx]:mPolys[ctxTarget.idx];
  if(obj){obj.color=c;redraw();}
  curColor=c;
  document.getElementById("inp-color").value=c.length===7?c:"#30d158";
}
function ctxOpacity(v){
  if(!ctxTarget)return;
  const obj=ctxTarget.type==="line"?mLines[ctxTarget.idx]:mPolys[ctxTarget.idx];
  if(obj){obj.opacity=v/100;redraw();}
  curOpacity=v/100;
  document.getElementById("ctx-opacity-val").textContent=v+"%";
  document.getElementById("inp-opacity").value=v;
  document.getElementById("opacity-val").textContent=v+"%";
}
function ctxDelete(){
  if(!ctxTarget)return;
  if(ctxTarget.type==="line") mLines.splice(ctxTarget.idx,1);
  else mPolys.splice(ctxTarget.idx,1);
  ctxTarget=null;redraw();
}
function ctxRename(){
  if(!ctxTarget||ctxTarget.type!=="poly")return;
  const poly=mPolys[ctxTarget.idx];
  openNamePanel("เปลี่ยนชื่อ",(nm)=>{poly.name=nm;redraw();},poly.name);
}

// ── name panel ─────────────────────────────────────────
function openNamePanel(title,cb,val="",showAType=false){
  document.getElementById("name-panel-title").textContent=title;
  document.getElementById("name-input").value=val;
  document.getElementById("atype-row").style.display=showAType?"flex":"none";
  if(showAType){curAType="room";setAType("room");}
  namePanel.style.display="block";
  document.getElementById("name-input").focus();
  nameCb=cb;
}
function finishName(){
  const v=document.getElementById("name-input").value.trim();
  namePanel.style.display="none";
  if(nameCb)nameCb(v, curAType); nameCb=null;
}
function cancelName(){
  namePanel.style.display="none";
  if(nameCb)nameCb("", curAType); nameCb=null;
}
document.getElementById("name-input").addEventListener("keydown",e=>{
  if(e.key==="Enter")finishName();
  if(e.key==="Escape")cancelName();
});

// ── calibrate ──────────────────────────────────────────
function finishCalib(){
  const dist=parseFloat(document.getElementById("calib-input").value);
  if(!dist||dist<=0){alert("ระยะต้องมากกว่า 0");return;}
  if(calibPts.length<2){cancelCalib();return;}
  const dx=(calibPts[1].x-calibPts[0].x)/RS,dy=(calibPts[1].y-calibPts[0].y)/RS;
  const ppm=Math.hypot(dx,dy)/dist;
  const N=Math.round(1000*(72/25.4)/ppm);
  const sc={N,label:`★ 1:${N} (สอบเทียบ)`,pts_per_m:ppm,calibrated:true};
  if(!pageData)pageData={};
  pageData.scale=sc;
  getStore(curPage).calibScale=sc;
  document.getElementById("scale-badge").className="ok";
  document.getElementById("scale-badge").textContent=sc.label;
  document.getElementById("lbl-scale").textContent=sc.label;
  calibPanel.style.display="none"; calibPts=[];
  setMode("dist");
  setStatus(`✅ 1:${N} สอบเทียบแล้ว`);
}
function cancelCalib(){calibPts=[];calibPanel.style.display="none";setMode("pan");}

// ── mouse move ─────────────────────────────────────────
ws.addEventListener("mousemove",e=>{
  if(isPan){panX+=e.clientX-lastMx;panY+=e.clientY-lastMy;lastMx=e.clientX;lastMy=e.clientY;applyT();return;}

  // select+drag
  if(mode==="sel"){
    const {x:cx,y:cy}=cXY(e);
    if(dragState){
      const cur=cToPdf(cx,cy);
      const dx=cur.x-dragState.startPdf.x, dy=cur.y-dragState.startPdf.y;
      if(dragState.type==="line"){
        const o=dragState.origData;
        mLines[dragState.idx].x0=o.x0+dx; mLines[dragState.idx].y0=o.y0+dy;
        mLines[dragState.idx].x1=o.x1+dx; mLines[dragState.idx].y1=o.y1+dy;
      }else{
        mPolys[dragState.idx].pts=dragState.origData.map(p=>({x:p.x+dx,y:p.y+dy}));
      }
      redraw();
    }else{
      // hover cursor
      ws.style.cursor=hitTest(cx,cy)?"move":"default";
    }
    return;
  }

  if(mode==="pan"||!pageData){
    snapCur.style.display="none";snapLbl.style.display="none";snapTarget=null;return;
  }
  const {x,y}=cXY(e);
  const s=snap(x,y);
  snapTarget=s;
  const wsR=ws.getBoundingClientRect();
  const scx=s.x*zoom+panX+wsR.left;
  const scy=s.y*zoom+panY+wsR.top;
  snapCur.style.left=scx+"px"; snapCur.style.top=scy+"px";
  snapCur.style.display="block";
  const col=s.t?SNAP_COLORS[s.t]:"#aaa";
  snapCur.style.borderColor=col;
  snapCur.style.boxShadow=`0 0 6px ${col}66`;
  if(s.t){
    snapLbl.textContent=SNAP_LABELS[s.t];
    snapLbl.style.color=col;
    snapLbl.style.left=(scx+10)+"px";
    snapLbl.style.top=(scy-8)+"px";
    snapLbl.style.display="block";
  }else{
    snapLbl.style.display="none";
  }
  redraw();
});
ws.addEventListener("mouseup",()=>{
  isPan=false;
  dragState=null;
  ws.style.cursor=mode==="pan"?"grab":mode==="sel"?"default":"crosshair";
});
ws.addEventListener("mouseleave",()=>{
  isPan=false;dragState=null;snapCur.style.display="none";snapLbl.style.display="none";snapTarget=null;
});
ws.addEventListener("dblclick",e=>{
  if(mode!=="sel")return;
  const {x:cx,y:cy}=cXY(e);
  const hit=hitTest(cx,cy);
  if(!hit)return;
  selItem=hit;
  if(hit.type==="poly"){
    const poly=mPolys[hit.idx];
    openNamePanel("เปลี่ยนชื่อ",(nm,at)=>{poly.name=nm;if(at)poly.areaType=at;redraw();},
      poly.name, true);
  }
});
ws.addEventListener("wheel",e=>{
  e.preventDefault();
  const r=ws.getBoundingClientRect(),fx=e.clientX-r.left,fy=e.clientY-r.top;
  const dz=e.deltaY>0?0.85:1.18,nz=Math.max(0.08,Math.min(8,zoom*dz));
  panX=fx-(fx-panX)*(nz/zoom); panY=fy-(fy-panY)*(nz/zoom); zoom=nz; applyT();
},{passive:false});

// ── mode ───────────────────────────────────────────────
function setMode(m){
  mode=m; mPts=[]; snapTarget=null;
  snapCur.style.display="none"; snapLbl.style.display="none";
  if(m!=="calib"){calibPts=[];calibPanel.style.display="none";}
  ["pan","sel","dist","area","calib"].forEach(k=>{
    document.getElementById("btn-"+k)?.classList.toggle("active",m===k);
  });
  document.getElementById("lbl-mode").textContent=
    {pan:"Pan ✋",sel:"↖ เลือก",dist:"วัดระยะ 📏",area:"วัดพื้นที่ ⬡",calib:"📐 สอบเทียบ"}[m]||m;
  ws.className=(m==="pan"||m==="sel")?"":"drawing";
  ws.style.cursor=m==="sel"?"default":m==="pan"?"grab":"crosshair";
  if(m!=="sel"){selItem=null;dragState=null;}
  document.getElementById("measure-result").textContent="";
  if(m==="calib")setStatus("คลิกจุดที่ 1…");
  redraw();
}
function clearMeasures(){
  mPts=[];mLines=[];mPolys=[];calibPts=[];
  calibPanel.style.display="none";
  document.getElementById("measure-result").textContent="";
  redraw();
}

// ── export ─────────────────────────────────────────────
function buildRows(){
  saveCurrentPage();
  const rows=[];
  for(const [pg,data] of Object.entries(pageStore)){
    const scl=analyseCache[pg]?.scale?.label??data.calibScale?.label??"-";
    for(const l of (data.lines||[])){
      rows.push({หน้า:+pg,ประเภท:"ระยะ",ชื่อ:"",
        ค่า:l.dist!=null?+l.dist.toFixed(4):null,
        หน่วย:l.dist!=null?"ม.":"pt","ไร่-งาน-วา":"",scale:scl});
    }
    for(const p of (data.polys||[])){
      if(!p.closed)continue;
      rows.push({หน้า:+pg,ประเภท:"พื้นที่",ชื่อ:p.name||"",
        ค่า:p.area!=null?+p.area.toFixed(4):null,
        หน่วย:"ตร.ม.","ไร่-งาน-วา":toRNW(p.area),scale:scl});
    }
  }
  return rows.sort((a,b)=>a.หน้า-b.หน้า);
}
function exportJSON(){
  const rows=buildRows();
  if(!rows.length){alert("ยังไม่มีข้อมูล");return;}
  dlBlob(new Blob([JSON.stringify({measurements:rows},null,2)],
    {type:"application/json"}),"measurements.json");
}
function exportCSV(){
  const rows=buildRows();
  if(!rows.length){alert("ยังไม่มีข้อมูล");return;}
  const cols=["หน้า","ประเภท","ชื่อ","ค่า","หน่วย","ไร่-งาน-วา","scale"];
  const esc=v=>{const s=String(v??"");return s.includes(",")?`"${s}"`:s;};
  const csv="\uFEFF"+[cols.join(","),
    ...rows.map(r=>cols.map(c=>esc(r[c])).join(","))].join("\r\n");
  dlBlob(new Blob([csv],{type:"text/csv;charset=utf-8"}),"measurements.csv");
}
function dlBlob(blob,name){
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);a.download=name;a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),5000);
}

// ── keyboard ───────────────────────────────────────────
document.addEventListener("keydown",e=>{
  if(e.target.tagName==="INPUT")return;
  if(e.key==="ArrowRight"&&curPage<totalPages)loadPage(curPage+1);
  if(e.key==="ArrowLeft"&&curPage>1)loadPage(curPage-1);
  if(e.key==="Escape"){
    setMode("pan");
    namePanel.style.display="none";
    ctxMenu.style.display="none";
  }
  if((e.key==="Delete"||e.key==="Backspace")&&mode==="sel"&&selItem){
    if(selItem.type==="line") mLines.splice(selItem.idx,1);
    else mPolys.splice(selItem.idx,1);
    selItem=null;dragState=null;redraw();
  }
  if(e.key==="f"||e.key==="F")fitToWindow();
  if(e.key==="Delete"&&ctxTarget)ctxDelete();
});
function setStatus(t){document.getElementById("status").textContent=t;}
window.addEventListener("resize",()=>{if(bgImg)fitToWindow();});
</script>
</body>
</html>"""

@app.get("/")
def root():
    return HTMLResponse(HTML)

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
