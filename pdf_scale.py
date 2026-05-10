"""
proto/pdf_scale.py
──────────────────
ทดสอบ 3 อย่าง:
  1. อ่าน PDF → ขนาดหน้า (mm)
  2. จับ scale จาก text  (มาตราส่วน / SCALE / 1:xxx)
  3. Extract vector snap candidates (เส้น/path endpoints)

ใช้:  python pdf_scale.py <ไฟล์.pdf> [หน้า]
      python pdf_scale.py plan.pdf 1
"""

import sys
import re
import json
import time
import fitz   # PyMuPDF

# ────────────────────────────────────────────
# 1.  ขนาดหน้า
# ────────────────────────────────────────────
def page_size_mm(page: fitz.Page) -> dict:
    """คืน width/height เป็น mm และ paper size ที่ใกล้ที่สุด"""
    r = page.rect
    w_mm = round(r.width  * 25.4 / 72, 1)
    h_mm = round(r.height * 25.4 / 72, 1)

    PAPER = {
        'A0': (841, 1189), 'A1': (594, 841), 'A2': (420, 594),
        'A3': (297, 420),  'A4': (210, 297),
    }
    def match(w, h):
        for name, (pw, ph) in PAPER.items():
            if abs(w - pw) < 10 and abs(h - ph) < 10: return name
            if abs(w - ph) < 10 and abs(h - pw) < 10: return name + ' (landscape)'
        return 'custom'

    return {
        'width_mm':  w_mm,
        'height_mm': h_mm,
        'width_pt':  round(r.width,  2),
        'height_pt': round(r.height, 2),
        'paper':     match(w_mm, h_mm),
        'rotation':  page.rotation,
    }


# ────────────────────────────────────────────
# 2.  จับ scale จาก text
# ────────────────────────────────────────────
SCALE_PATTERNS = [
    # ภาษาไทย: มาตราส่วน 1:100
    r'มาตราส่วน\s*(\d+)\s*:\s*(\d+)',
    r'scale\s*(\d+)\s*:\s*(\d+)',
    # ย่อ: 1:100  หรือ  1/100
    r'\b1\s*[:/]\s*(\d{2,5})\b',
    # AS NOTED / NTS
    r'\b(AS\s+NOTED|NTS|NOT\s+TO\s+SCALE)\b',
]

def detect_scale(page: fitz.Page) -> list[dict]:
    """สแกน text ทั้งหน้าหา scale"""
    blocks = page.get_text('dict', flags=fitz.TEXT_PRESERVE_WHITESPACE)['blocks']
    found = []

    for b in blocks:
        if b.get('type') != 0:   # type 0 = text
            continue
        for line in b.get('lines', []):
            for span in line.get('spans', []):
                text = span['text'].strip()
                if not text:
                    continue
                tu = text.upper()

                for pat in SCALE_PATTERNS:
                    m = re.search(pat, tu, re.IGNORECASE)
                    if m:
                        bbox = span['bbox']   # (x0, y0, x1, y1) in points
                        entry = {
                            'text':   text,
                            'matched': m.group(0),
                            'bbox':   [round(v, 2) for v in bbox],
                            'center': [
                                round((bbox[0] + bbox[2]) / 2, 2),
                                round((bbox[1] + bbox[3]) / 2, 2),
                            ],
                            'font_size': round(span['size'], 1),
                        }
                        # แยก ratio ถ้าได้
                        nums = re.findall(r'\d+', m.group(0))
                        if len(nums) == 2:
                            a, b_ = int(nums[0]), int(nums[1])
                            entry['ratio'] = f'1:{b_}' if a == 1 else f'{a}:{b_}'
                            entry['scale_factor'] = b_ / a   # 1 pt ใน PDF = scale_factor pt จริง
                        found.append(entry)
                        break   # ไม่ต้อง match pattern ซ้ำ

    # deduplicate โดย matched text
    seen, unique = set(), []
    for f in found:
        k = f['matched']
        if k not in seen:
            seen.add(k)
            unique.append(f)

    return unique


# ────────────────────────────────────────────
# 3.  Vector snap candidates
# ────────────────────────────────────────────
def extract_snap_candidates(page: fitz.Page, max_pts: int = 5000) -> dict:
    """
    ดึง endpoint ของทุก path ในหน้า
    คืน list ของ {x, y} ใน point coordinates
    """
    t0 = time.perf_counter()
    paths = page.get_drawings()   # list ของ path dicts
    pts = set()

    for path in paths:
        for item in path.get('items', []):
            op = item[0]
            if op == 'l':    # line: (op, p1, p2)
                pts.add((round(item[1].x, 2), round(item[1].y, 2)))
                pts.add((round(item[2].x, 2), round(item[2].y, 2)))
            elif op == 're': # rect: (op, Rect)
                r = item[1]
                for corner in [(r.x0,r.y0),(r.x1,r.y0),(r.x1,r.y1),(r.x0,r.y1)]:
                    pts.add((round(corner[0],2), round(corner[1],2)))
            elif op == 'c':  # curve: (op, p1, p2, p3, p4)
                pts.add((round(item[1].x, 2), round(item[1].y, 2)))
                pts.add((round(item[4].x, 2), round(item[4].y, 2)))
            elif op == 'qu': # quad
                for p in item[1:]:
                    if hasattr(p, 'x'):
                        pts.add((round(p.x, 2), round(p.y, 2)))

    elapsed = time.perf_counter() - t0
    total = len(pts)
    candidates = list(pts)[:max_pts]

    return {
        'total_paths':      len(paths),
        'total_pts':        total,
        'returned_pts':     len(candidates),
        'truncated':        total > max_pts,
        'extract_ms':       round(elapsed * 1000, 1),
        'candidates':       [{'x': p[0], 'y': p[1]} for p in candidates],
    }


# ────────────────────────────────────────────
# main
# ────────────────────────────────────────────
def analyse(pdf_path: str, page_no: int = 1) -> dict:
    doc  = fitz.open(pdf_path)
    page = doc[page_no - 1]

    size   = page_size_mm(page)
    scales = detect_scale(page)
    snaps  = extract_snap_candidates(page)

    # ถ้า PDF มี mediabox กับ cropbox ต่างกัน
    mb = page.mediabox
    cb = page.cropbox
    crop_info = None
    if abs(mb.width - cb.width) > 1 or abs(mb.height - cb.height) > 1:
        crop_info = {
            'mediabox': [round(v,1) for v in [mb.x0,mb.y0,mb.width,mb.height]],
            'cropbox':  [round(v,1) for v in [cb.x0,cb.y0,cb.width,cb.height]],
        }

    # ── คำนวณ calibration อัตโนมัติ ──────────────────────────
    # 1pt ใน PDF = (ขนาดกระดาษ mm / ขนาดหน้า pt) = mm/pt บนกระดาษจริง
    # ถ้ารู้ scale 1:N → 1pt ในแบบ = N * mm/pt ในโลกจริง
    # pixels_per_meter (ที่ render scale 1.0) = 1000mm / (N * mm_per_pt)
    auto_calibration = None
    if scales:
        best = next((s for s in scales if 'scale_factor' in s), None)
        if best:
            N        = best['scale_factor']          # เช่น 100 สำหรับ 1:100
            mm_per_pt = size['width_mm'] / size['width_pt']
            real_mm_per_pt = N * mm_per_pt           # mm จริงต่อ 1pt ใน PDF
            pts_per_meter  = 1000 / real_mm_per_pt   # pt ใน PDF ต่อ 1m จริง
            auto_calibration = {
                'scale_ratio':       best.get('ratio'),
                'scale_N':           N,
                'mm_per_pt':         round(mm_per_pt, 4),
                'real_mm_per_pt':    round(real_mm_per_pt, 4),
                'pts_per_meter':     round(pts_per_meter, 4),
                'note': f'1m จริง = {pts_per_meter:.2f} pt ใน PDF (ที่ render scale 1.0)'
            }

    return {
        'file':        pdf_path,
        'total_pages': len(doc),
        'page':        page_no,
        'size':        size,
        'crop':        crop_info,
        'scales':      scales,
        'auto_calibration': auto_calibration,
        'snap': {
            'total_paths':  snaps['total_paths'],
            'total_pts':    snaps['total_pts'],
            'returned_pts': snaps['returned_pts'],
            'truncated':    snaps['truncated'],
            'extract_ms':   snaps['extract_ms'],
            'sample':       snaps['candidates'][:20],
        },
        'pdf_type': 'vector' if snaps['total_paths'] > 10 else 'raster/scanned',
    }


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python pdf_scale.py <file.pdf> [page_no]')
        sys.exit(1)

    pdf_path = sys.argv[1]
    page_no  = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    result = analyse(pdf_path, page_no)

    # pretty print
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # สรุปสั้น
    print('\n' + '='*50)
    print(f"📄 {result['file']}  ({result['total_pages']} หน้า)")
    print(f"📐 หน้า {result['page']}: {result['size']['paper']}  "
          f"{result['size']['width_mm']}×{result['size']['height_mm']} mm")

    if result['scales']:
        print(f"\n🔍 พบ scale:")
        for s in result['scales']:
            ratio = s.get('ratio', s['matched'])
            print(f"   {ratio}  ← \"{s['text']}\"  @ {s['center']}")
    else:
        print('\n⚠️  ไม่พบ scale text ในหน้านี้')

    if result['auto_calibration']:
        c = result['auto_calibration']
        print(f"\n📏 Auto-calibration: {c['scale_ratio']}")
        print(f"   1m จริง = {c['pts_per_meter']:.2f} pt ใน PDF")
        print(f"   → ใช้ค่านี้แทนการคลิก 2 จุดได้เลย")

    sn = result['snap']
    print(f"\n🗂  Vector paths: {sn['total_paths']}  |  "
          f"snap pts: {sn['total_pts']}  |  "
          f"ใช้เวลา: {sn['extract_ms']} ms")
    print(f"📊 PDF type: {result['pdf_type']}")
