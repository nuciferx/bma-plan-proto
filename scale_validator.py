"""
scale_validator.py
──────────────────
ตรวจว่า scale ที่ระบุในแบบ ตรงกับ scale จริงในไฟล์ PDF หรือไม่

วิธีการ:
  1. หา dimension annotations (เส้น + ตัวเลขระยะ เช่น "7.71")
  2. วัดความยาวเส้นนั้นในหน่วย pt
  3. คำนวณ scale จริง = ความยาว pt / ระยะ (m) → เทียบกับ stated scale
"""

import fitz
import re
import math
from dataclasses import dataclass

PT_PER_MM = 72 / 25.4   # 1mm = 2.835 pt


@dataclass
class DimCandidate:
    value_m: float     # ระยะที่ตัวเลขระบุ (เมตร)
    text: str
    text_pos: tuple    # (x, y) จุดกลางข้อความ
    line_length_pt: float | None = None   # ความยาวเส้น dimension ที่จับคู่ได้
    implied_scale: float | None = None    # N ใน 1:N จากการวัดจริง
    delta_pct: float | None = None        # % ต่างจาก stated scale


# ─── regex หาตัวเลขที่เป็น dimension ───────────────────────────────────────
# รูปแบบ: 7.71 / 3000 / 12.50 / 4,500 (หน่วย mm หรือ m)
DIM_RE = re.compile(r'^\d{1,4}(?:[.,]\d{1,3})?$')

def looks_like_dim(text: str) -> tuple[bool, float | None]:
    """คืน (is_dim, value_in_meters)"""
    t = text.replace(',', '')
    m = DIM_RE.match(t)
    if not m:
        return False, None
    try:
        v = float(t)
    except ValueError:
        return False, None
    # กรองค่าที่ไม่สมเหตุ: ช่วง 0.5–60 เมตร หรือ 500–60000 มม.
    if 0.5 <= v <= 60:       # น่าจะเป็นเมตร
        return True, v
    if 500 <= v <= 60000:    # น่าจะเป็น มม. → แปลงเป็นเมตร
        return True, v / 1000
    return False, None


# ─── หาเส้น dimension ใกล้จุด text ────────────────────────────────────────
def find_nearby_hline(page: fitz.Page, cx: float, cy: float,
                      expected_len_pt: float, tol: float = 0.25) -> float | None:
    """
    หาเส้นแนวนอนที่อยู่ใกล้ (cx, cy) และมีความยาวใกล้ expected_len_pt ± tol
    คืน ความยาวจริง (pt) หรือ None ถ้าไม่เจอ
    """
    search_r = max(expected_len_pt * 1.5, 200)
    clip = fitz.Rect(cx - search_r, cy - 60, cx + search_r, cy + 60)

    best_len, best_diff = None, float('inf')
    for d in page.get_drawings():
        for item in d['items']:
            if item[0] != 'l':
                continue
            p1, p2 = item[1], item[2]
            dy = abs(p2.y - p1.y)
            if dy > 4:                     # ไม่แนวนอน
                continue
            lx0 = min(p1.x, p2.x)
            lx1 = max(p1.x, p2.x)
            ly  = (p1.y + p2.y) / 2
            # ต้องอยู่ใน clip
            if not (clip.x0 <= lx0 and lx1 <= clip.x1 and clip.y0 <= ly <= clip.y1):
                continue
            length = lx1 - lx0
            diff = abs(length - expected_len_pt) / expected_len_pt
            if diff < tol and diff < best_diff:
                best_diff = diff
                best_len  = length

    return best_len


# ─── main validator ─────────────────────────────────────────────────────────
def validate_scale(pdf_path: str, page_no: int, stated_scale: int,
                   paper_width_mm: float = 841.0) -> dict:
    """
    stated_scale : N ใน 1:N (เช่น 100 สำหรับ 1:100)
    paper_width_mm: ความกว้างกระดาษจริง (A1=841, A3=420)
    """
    doc  = fitz.open(pdf_path)
    page = doc[page_no - 1]

    page_w_mm  = page.rect.width / PT_PER_MM
    pts_per_m  = (1000 / stated_scale) * PT_PER_MM   # pt ใน PDF ต่อ 1m จริง

    # ── ขั้น 1: เก็บ word positions ─────────────────────────────────────
    words = page.get_text("words")

    candidates: list[DimCandidate] = []
    for w in words:
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        ok, val_m = looks_like_dim(text)
        if not ok:
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        candidates.append(DimCandidate(value_m=val_m, text=text, text_pos=(cx, cy)))

    # ── ขั้น 2: หาเส้น dimension ที่จับคู่ ──────────────────────────────
    results = []
    for cand in candidates:
        expected_pt = cand.value_m * pts_per_m
        actual_pt   = find_nearby_hline(page, cand.text_pos[0], cand.text_pos[1],
                                        expected_pt, tol=0.30)
        if actual_pt is None:
            continue

        implied_N   = (actual_pt / (cand.value_m * PT_PER_MM * 1000 / 1000))
        # implied_N = actual_pt / (value_m * pt_per_m_if_scale_1)
        # pt_per_m @ scale 1:1 = 1000mm * PT_PER_MM = 2834.6 pt
        implied_N   = 2834.6 * cand.value_m / actual_pt
        delta_pct   = (implied_N - stated_scale) / stated_scale * 100

        cand.line_length_pt  = round(actual_pt, 2)
        cand.implied_scale   = round(implied_N, 1)
        cand.delta_pct       = round(delta_pct, 2)
        results.append(cand)

    # ── ขั้น 3: สรุป ────────────────────────────────────────────────────
    # กรอง outlier และเอา median
    if results:
        scales = sorted([r.implied_scale for r in results])
        mid    = scales[len(scales) // 2]
        # กรองเฉพาะที่ใกล้ median ±20%
        valid  = [r for r in results if abs(r.implied_scale - mid) / mid < 0.20]
    else:
        valid, mid = [], None

    # paper size mismatch check
    actual_paper_w_mm = page_w_mm
    paper_scale_ratio = actual_paper_w_mm / paper_width_mm   # 1.0 = ตรงกัน
    paper_ok = abs(paper_scale_ratio - 1.0) < 0.02

    verdict = "ไม่สามารถตรวจสอบได้ (ไม่พบ dimension)"
    scale_ok = None
    if valid:
        avg_implied = sum(r.implied_scale for r in valid) / len(valid)
        delta = abs(avg_implied - stated_scale) / stated_scale * 100
        scale_ok = delta < 5
        if scale_ok and paper_ok:
            verdict = f"✅ Scale ถูกต้อง (~{avg_implied:.0f} ≈ {stated_scale})"
        elif scale_ok and not paper_ok:
            verdict = (f"⚠️  Scale ถูกที่ stated paper แต่ PDF ขนาด {actual_paper_w_mm:.0f}mm "
                       f"≠ stated {paper_width_mm:.0f}mm (ratio={paper_scale_ratio:.2f})")
        else:
            verdict = (f"❌ Scale ไม่ตรง! stated=1:{stated_scale} "
                       f"แต่วัดได้ ~1:{avg_implied:.0f} (ต่าง {delta:.1f}%)")

    return {
        'page':           page_no,
        'stated_scale':   f'1:{stated_scale}',
        'paper_stated_mm': paper_width_mm,
        'paper_actual_mm': round(actual_paper_w_mm, 1),
        'paper_match':    paper_ok,
        'dimensions_found': len(results),
        'dimensions_valid': len(valid),
        'implied_scale':  f'1:{mid:.0f}' if mid else None,
        'verdict':        verdict,
        'top_dims': [
            {
                'text':    r.text,
                'value_m': r.value_m,
                'line_pt': r.line_length_pt,
                'implied': f'1:{r.implied_scale:.0f}',
                'delta%':  r.delta_pct,
                'pos':     [round(v) for v in r.text_pos],
            }
            for r in valid[:8]
        ],
    }


# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import sys, json

    pdf   = sys.argv[1] if len(sys.argv) > 1 else "../20250616_RAMA4 APARTMENT PERMIT rev 1.pdf"
    pg    = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    scale = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    paper = float(sys.argv[4]) if len(sys.argv) > 4 else 841.0   # A1 landscape

    result = validate_scale(pdf, pg, scale, paper)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    print(f'\n{"="*55}')
    print(f"📄 หน้า {result['page']}  stated={result['stated_scale']}")
    print(f"📐 กระดาษ stated={result['paper_stated_mm']}mm  actual={result['paper_actual_mm']}mm  "
          f"{'✅' if result['paper_match'] else '❌'}")
    print(f"📏 Dimensions จับได้ {result['dimensions_found']} / valid {result['dimensions_valid']}")
    if result['implied_scale']:
        print(f"🔍 Scale วัดได้จริง: {result['implied_scale']}")
    print(f"\n{result['verdict']}")
