"""
scale_bar_detect.py
────────────────────
ตรวจสอบ scale จริงใน PDF 3 วิธีซ้อนกัน:

  1. TITLE BLOCK  — อ่าน "1:100" จาก text
  2. SCALE BAR    — หาเส้น bar กราฟิก + label (0...5m)
  3. DIMENSION    — จับ dim line + ตัวเลข cross-validate

คืน verdict + confidence ว่า scale ตรงหรือไม่
"""

import fitz
import re
import math
from dataclasses import dataclass, field

PT_PER_MM  = 72 / 25.4
TITLE_ZONE = 0.86          # y > 86% = title block (exclude จาก dim/bar search)

# ── helpers ────────────────────────────────────────────────────
def pt_to_mm(pt):   return pt / PT_PER_MM
def mm_to_pt(mm):   return mm * PT_PER_MM

def page_drawing_rect(page: fitz.Page) -> fitz.Rect:
    """คืน Rect ของ drawing area (ไม่รวม title block)"""
    r = page.rect
    return fitz.Rect(r.x0, r.y0, r.x1, r.y1 * TITLE_ZONE)

def get_hlines(page: fitz.Page, clip: fitz.Rect,
               min_len=30, max_len=2000, max_dy=3) -> list[dict]:
    """เส้นแนวนอนใน clip"""
    lines = []
    for d in page.get_drawings():
        for item in d['items']:
            if item[0] != 'l': continue
            p1, p2 = item[1], item[2]
            dy = abs(p2.y - p1.y)
            dx = abs(p2.x - p1.x)
            if dy > max_dy or dx < min_len or dx > max_len: continue
            mx = (p1.x + p2.x) / 2
            my = (p1.y + p2.y) / 2
            if not clip.contains(fitz.Point(mx, my)): continue
            lines.append(dict(
                x0=round(min(p1.x, p2.x), 2),
                x1=round(max(p1.x, p2.x), 2),
                y=round(my, 2),
                length=round(dx, 2),
            ))
    return lines

def get_vlines(page: fitz.Page, clip: fitz.Rect,
               min_dy=5, max_dy=80, max_dx=3) -> list[dict]:
    """เส้นแนวตั้งสั้นๆ (tick marks)"""
    ticks = []
    for d in page.get_drawings():
        for item in d['items']:
            if item[0] != 'l': continue
            p1, p2 = item[1], item[2]
            dy = abs(p2.y - p1.y)
            dx = abs(p2.x - p1.x)
            if dx > max_dx or dy < min_dy or dy > max_dy: continue
            mx = (p1.x + p2.x) / 2
            my = (p1.y + p2.y) / 2
            if not clip.contains(fitz.Point(mx, my)): continue
            ticks.append(dict(x=round(mx, 2), y=round(my, 2), dy=round(dy, 2)))
    return ticks

# ════════════════════════════════════════════════════════
# METHOD 1: Title block scale text
# ════════════════════════════════════════════════════════
SCALE_RE = re.compile(r'1\s*[:/]\s*(\d{2,5})', re.IGNORECASE)

def method_title_block(page: fitz.Page) -> dict | None:
    """อ่าน scale จาก title block text"""
    r = page.rect
    # ค้นทั้งหน้า แต่เอา match ที่ font ใหญ่ที่สุด (title block มักใหญ่กว่า body text)
    spans = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0: continue
        for line in b["lines"]:
            for sp in line["spans"]:
                m = SCALE_RE.search(sp["text"])
                if m:
                    spans.append(dict(
                        N=int(m.group(1)),
                        text=sp["text"].strip(),
                        size=sp["size"],
                        bbox=sp["bbox"],
                    ))
    if not spans:
        return None
    # เอาที่ font size ใหญ่สุด
    best = max(spans, key=lambda s: s["size"])
    N = best["N"]
    w_mm = pt_to_mm(r.width)
    pts_per_m = (1000 / N) * PT_PER_MM
    return dict(
        method="title_block",
        stated_scale=f"1:{N}",
        N=N,
        pts_per_m=round(pts_per_m, 4),
        text=best["text"],
        confidence=90,
    )

# ════════════════════════════════════════════════════════
# METHOD 2: Graphical scale bar
# ════════════════════════════════════════════════════════
UNIT_RE  = re.compile(r'\b(m|M|ม\.?|เมตร|meter|meters|mm|cm)\b')
NUM_RE   = re.compile(r'^\d+(?:\.\d+)?$')

def method_scale_bar(page: fitz.Page) -> dict | None:
    """
    หา scale bar กราฟิก:
      - เส้นยาวแนวนอน + tick แนวตั้งที่ปลาย
      - มีตัวเลข 0 และ N อยู่ใกล้ปลายทั้งสอง
      - มี unit text ใกล้ๆ (m, ม., เมตร)
    """
    draw_area = page_drawing_rect(page)
    hlines    = get_hlines(page, draw_area, min_len=50, max_len=800)
    ticks     = get_vlines(page, draw_area, min_dy=5, max_dy=60)
    words     = page.get_text("words", clip=draw_area)

    def words_near(cx, cy, radius=60):
        return [w[4] for w in words
                if abs((w[0]+w[2])/2 - cx) < radius
                and abs((w[1]+w[3])/2 - cy) < radius]

    results = []
    for hl in sorted(hlines, key=lambda x: -x["length"]):
        x0, x1, y, length = hl["x0"], hl["x1"], hl["y"], hl["length"]

        # ตรวจ tick ที่ปลายซ้ายและขวา (±15pt)
        left_ticks  = [t for t in ticks if abs(t["x"] - x0) < 15 and abs(t["y"] - y) < 30]
        right_ticks = [t for t in ticks if abs(t["x"] - x1) < 15 and abs(t["y"] - y) < 30]
        if not (left_ticks and right_ticks):
            continue

        # หาตัวเลขที่ปลายทั้งสอง
        left_words  = words_near(x0, y)
        right_words = words_near(x1, y)
        nearby_all  = words_near((x0+x1)/2, y, radius=length/2+80)

        # ต้องมี "0" ข้างหนึ่งและ ตัวเลข > 0 อีกข้าง
        has_zero = any(w.strip() in ('0',) for w in left_words + right_words)
        nums = []
        for w in left_words + right_words:
            if NUM_RE.match(w.strip()):
                try:
                    v = float(w.strip())
                    if v > 0: nums.append(v)
                except: pass
        if not has_zero or not nums:
            continue

        # หา unit
        has_unit = any(UNIT_RE.search(w) for w in nearby_all)

        bar_value = max(nums)   # ค่า label ที่ปลายแถบ (เช่น 5, 10, 20)

        # แปลง unit → เมตร
        unit_words = [w for w in nearby_all if UNIT_RE.search(w)]
        unit = unit_words[0] if unit_words else "m"
        if "mm" in unit.lower():
            bar_m = bar_value / 1000
        elif "cm" in unit.lower():
            bar_m = bar_value / 100
        else:
            bar_m = bar_value   # assume เมตร

        pts_per_m = length / bar_m
        N = round(1000 * PT_PER_MM / pts_per_m)   # implied N ใน 1:N

        results.append(dict(
            method="scale_bar",
            bar_length_pt=round(length, 2),
            bar_value_m=bar_m,
            pts_per_m=round(pts_per_m, 4),
            implied_scale=f"1:{N}",
            N=N,
            has_unit=has_unit,
            pos=[round(x0), round(y)],
            confidence=80 if has_unit else 55,
        ))

    return results[0] if results else None

# ════════════════════════════════════════════════════════
# METHOD 3: Dimension line cross-validation
# ════════════════════════════════════════════════════════
DIM_VAL_RE = re.compile(r'^(\d{3,5})$|^(\d{1,3}[.,]\d{2,3})$')

def method_dimension(page: fitz.Page, stated_N: int) -> dict | None:
    """
    หา dimension annotations ใน drawing area
    (ไม่รวม title block)
    """
    draw_area = page_drawing_rect(page)
    hlines = get_hlines(page, draw_area, min_len=20, max_len=3000)
    words  = page.get_text("words", clip=draw_area)

    pts_per_m_stated = (1000 / stated_N) * PT_PER_MM
    matches = []

    for w in words:
        text = w[4].strip()
        m = DIM_VAL_RE.match(text)
        if not m: continue
        try:
            val = float(text.replace(',', '.'))
        except: continue

        # classify unit by magnitude
        if 500 <= val <= 50000:    val_m = val / 1000   # mm
        elif 0.5 <= val <= 60:     val_m = val           # m
        else: continue

        cx = (w[0] + w[2]) / 2
        cy = (w[1] + w[3]) / 2
        expected_pt = val_m * pts_per_m_stated

        # หาเส้นใกล้ๆ ที่ความยาวใกล้เคียง expected ±30%
        for hl in hlines:
            lx = (hl["x0"] + hl["x1"]) / 2
            ly = hl["y"]
            if abs(lx - cx) > expected_pt * 0.8: continue
            if abs(ly - cy) > 80: continue
            ratio = hl["length"] / expected_pt
            if 0.7 <= ratio <= 1.3:
                implied_N = round(val_m * 1000 * PT_PER_MM / hl["length"])
                matches.append(dict(
                    text=text,
                    val_m=val_m,
                    line_pt=hl["length"],
                    implied_N=implied_N,
                    ratio=round(ratio, 3),
                ))
                break

    if not matches: return None

    Ns = sorted([r["implied_N"] for r in matches])
    median_N = Ns[len(Ns) // 2]
    valid = [r for r in matches if abs(r["implied_N"] - median_N) / median_N < 0.15]

    if not valid: return None
    avg_N = round(sum(r["implied_N"] for r in valid) / len(valid))
    delta = abs(avg_N - stated_N) / stated_N * 100

    return dict(
        method="dimension",
        dims_found=len(matches),
        dims_valid=len(valid),
        implied_scale=f"1:{avg_N}",
        N=avg_N,
        delta_pct=round(delta, 1),
        confidence=min(95, 50 + len(valid) * 10),
        samples=valid[:5],
    )

# ════════════════════════════════════════════════════════
# FINAL: รวมผล + verdict
# ════════════════════════════════════════════════════════
def analyse_page_scale(pdf_path: str, page_no: int) -> dict:
    doc  = fitz.open(pdf_path)
    page = doc[page_no - 1]

    r1 = method_title_block(page)
    stated_N = r1["N"] if r1 else 100
    r2 = method_scale_bar(page)
    r3 = method_dimension(page, stated_N)

    # รวม evidence
    Ns = []
    if r1: Ns.append((r1["N"], r1["confidence"], "title_block"))
    if r2: Ns.append((r2["N"], r2["confidence"], "scale_bar"))
    if r3: Ns.append((r3["N"], r3["confidence"], "dimension"))

    # weighted average
    if Ns:
        total_w = sum(c for _, c, _ in Ns)
        avg_N   = round(sum(N * c for N, c, _ in Ns) / total_w)
        spread  = max(N for N, _, _ in Ns) - min(N for N, _, _ in Ns)
        consistent = spread < stated_N * 0.10   # ต่างกันไม่เกิน 10%
    else:
        avg_N, consistent = None, None

    # verdict
    if not r1:
        verdict = "⚠️  ไม่พบ scale ใน title block"
    elif avg_N and consistent:
        if abs(avg_N - stated_N) / stated_N < 0.05:
            verdict = f"✅ Scale ถูกต้อง — ทุก method ยืนยัน 1:{stated_N}"
        else:
            verdict = f"❌ Scale ไม่ตรง — title block={r1['stated_scale']} แต่วัดได้ ~1:{avg_N}"
    elif avg_N and not consistent:
        verdict = f"⚠️  Scale ขัดแย้งกัน (spread={spread:.0f}) — title={r1['stated_scale']} bar/dim≈1:{avg_N}"
    else:
        verdict = f"ℹ️  อ่าน title block ได้ 1:{stated_N} — ไม่มีข้อมูล cross-validate"

    return dict(
        page=page_no,
        methods=dict(
            title_block=r1,
            scale_bar=r2,
            dimension=r3,
        ),
        summary=dict(
            stated=r1["stated_scale"] if r1 else None,
            measured=f"1:{avg_N}" if avg_N else None,
            consistent=consistent,
            evidence_count=len(Ns),
        ),
        verdict=verdict,
    )


# ── CLI ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, json

    pdf = sys.argv[1] if len(sys.argv) > 1 else \
          "../20250616_RAMA4 APARTMENT PERMIT rev 1.pdf"

    pages = list(map(int, sys.argv[2:])) if len(sys.argv) > 2 else [5, 6, 7]

    for pg in pages:
        res = analyse_page_scale(pdf, pg)
        print(f"\n{'='*60}")
        print(f"หน้า {pg}")
        print(f"  Title block : {res['methods']['title_block']}")
        print(f"  Scale bar   : {res['methods']['scale_bar']}")
        print(f"  Dimension   : {res['methods']['dimension']}")
        print(f"\n  ➤ {res['verdict']}")
