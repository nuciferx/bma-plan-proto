"""
สร้าง test PDF จำลองแบบก่อสร้าง A1 มี:
- เส้น vector เยอะๆ (ผังพื้น)
- text "มาตราส่วน 1:100" ใน title block
"""
import fitz

def make():
    doc  = fitz.open()
    # A1 landscape: 841 × 594 mm → points (1pt = 1/72 inch = 0.3528 mm)
    W = 841 / 25.4 * 72   # ≈ 2383.9 pt
    H = 594 / 25.4 * 72   # ≈ 1683.8 pt
    page = doc.new_page(width=W, height=H)

    blue  = (0.0, 0.2, 0.6)
    black = (0, 0, 0)
    gray  = (0.5, 0.5, 0.5)

    # ── Title block (ขวาล่าง) ──────────────────────────────
    tb_x = W - 340
    tb_y = H - 180
    tb_w = 340
    tb_h = 180
    page.draw_rect(fitz.Rect(tb_x, tb_y, W, H), color=black, width=1.5)

    # เส้นแบ่งใน title block
    for dy in [40, 80, 120, 160]:
        page.draw_line(fitz.Point(tb_x, tb_y+dy), fitz.Point(W, tb_y+dy), color=black, width=0.5)
    page.draw_line(fitz.Point(tb_x+170, tb_y), fitz.Point(tb_x+170, H), color=black, width=0.5)

    # ข้อความใน title block
    page.insert_text(fitz.Point(tb_x+10, tb_y+28), "โครงการ: อาคาร XYZ", fontsize=10, color=black)
    page.insert_text(fitz.Point(tb_x+10, tb_y+68), "แบบ: ผังพื้นชั้น 1",  fontsize=10, color=black)
    page.insert_text(fitz.Point(tb_x+10, tb_y+108), "มาตราส่วน 1:100",    fontsize=11, color=blue)
    page.insert_text(fitz.Point(tb_x+10, tb_y+148), "เลขที่แบบ: A-101",   fontsize=9,  color=black)
    page.insert_text(fitz.Point(tb_x+180, tb_y+108), "วันที่: มกราคม 2568", fontsize=9, color=black)

    # ── Border ─────────────────────────────────────────────
    margin = 40
    page.draw_rect(fitz.Rect(margin, margin, W-margin, H-margin), color=black, width=2)

    # ── ผังพื้นจำลอง (กล่องซ้อนกัน + ผนัง) ───────────────
    ox, oy = 80, 80   # origin ของแบบ

    # กรอบนอกอาคาร (30x20 เมตร @ 1:100 = 300x200 pt ≈ scale 1pt=100pt จริง)
    # แต่ใน PDF เราวาดที่ 1pt = 1pt ก่อน ให้ scale text บอก
    scale = 1.0   # 1 unit = 1 pt ใน PDF, scale จริงตาม title block = 1:100

    def rect(x, y, w, h, lw=1.0, col=black):
        page.draw_rect(fitz.Rect(ox+x, oy+y, ox+x+w, oy+y+h), color=col, width=lw)

    def line(x1, y1, x2, y2, lw=0.5, col=black):
        page.draw_line(fitz.Point(ox+x1, oy+y1), fitz.Point(ox+x2, oy+y2), color=col, width=lw)

    # ผนังนอก (หนา 2)
    rect(0, 0, 1200, 800, lw=2.0)

    # ห้องภายใน
    for (x, y, w, h) in [
        (0,   0,   400, 400),   # ห้องนอน 1
        (400, 0,   400, 400),   # ห้องนอน 2
        (800, 0,   400, 400),   # ห้องนอน 3
        (0,   400, 300, 400),   # ครัว
        (300, 400, 500, 400),   # ห้องนั่งเล่น
        (800, 400, 400, 400),   # ห้องน้ำ
    ]:
        rect(x, y, w, h, lw=1.0)

    # เส้นมิติ (dimension lines)
    dim_y = 860
    line(0, dim_y, 1200, dim_y, lw=0.5, col=gray)
    line(0, dim_y-10, 0, dim_y+10, lw=0.5, col=gray)
    line(1200, dim_y-10, 1200, dim_y+10, lw=0.5, col=gray)
    page.insert_text(fitz.Point(ox+580, oy+dim_y+18), "30.00 ม.", fontsize=9, color=gray)

    dim_x = 1260
    line(dim_x, 0, dim_x, 800, lw=0.5, col=gray)
    line(dim_x-10, 0, dim_x+10, 0, lw=0.5, col=gray)
    line(dim_x-10, 800, dim_x+10, 800, lw=0.5, col=gray)
    page.insert_text(fitz.Point(ox+dim_x+8, oy+390), "20.00 ม.", fontsize=9, color=gray)

    # ── Scale bar ──────────────────────────────────────────
    sb_x, sb_y = 100, H - 220
    page.draw_line(fitz.Point(sb_x, sb_y), fitz.Point(sb_x+500, sb_y), color=black, width=1.5)
    for i in range(6):
        page.draw_line(fitz.Point(sb_x + i*100, sb_y-8), fitz.Point(sb_x + i*100, sb_y+8),
                       color=black, width=1.0)
    page.insert_text(fitz.Point(sb_x-5, sb_y+22), "0", fontsize=8, color=black)
    page.insert_text(fitz.Point(sb_x+490, sb_y+22), "5ม.", fontsize=8, color=black)
    page.insert_text(fitz.Point(sb_x+170, sb_y-20), "SCALE BAR", fontsize=8, color=black)

    out = "test_plan_A1.pdf"
    doc.save(out)
    print(f"✅ สร้าง {out}  ({W:.0f}×{H:.0f} pt)")

if __name__ == '__main__':
    make()
