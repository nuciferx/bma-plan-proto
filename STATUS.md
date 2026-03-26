# BMA Plan Proto — Status & Handoff
> อัปเดต: 2026-03-26

---

## ที่อยู่ไฟล์

| | Path |
|---|---|
| Local | `G:/drive/01 project/ai/bma-plan/proto/` |
| GitHub | https://github.com/nuciferx/bma-plan-proto |
| Online (Render) | https://bma-plan-proto.onrender.com |

---

## วิธีรันใหม่บนเครื่องอื่น

```bash
git clone https://github.com/nuciferx/bma-plan-proto.git
cd bma-plan-proto
pip install -r requirements.txt
python server.py
# เปิด http://localhost:8000
```

---

## สถาปัตยกรรม

```
Browser (vanilla JS + Canvas)
    ↕ HTTP (JSON / JPEG)
FastAPI server (server.py)   ← ไฟล์เดียว มี HTML/JS ฝังอยู่ใน string HTML
    ↕ fitz (PyMuPDF)
PDF file (upload ผ่าน /upload)
```

---

## ไฟล์ในโปรเจกต์

| ไฟล์ | หน้าที่ |
|---|---|
| `server.py` | **หลัก** — FastAPI + HTML/JS ทั้งหมดอยู่ในนี้ |
| `requirements.txt` | `fastapi uvicorn python-multipart pymupdf` |
| `render.yaml` | config deploy บน Render.com |
| `pdf_scale.py` | prototype เก่า — ไม่ได้ใช้แล้ว |
| `scale_validator.py` | validate scale — ยังไม่ต่อเข้า server |
| `scale_bar_detect.py` | detect scale bar — ยังไม่ต่อเข้า server |
| `make_test_pdf.py` | สร้าง test PDF |
| `test_plan_A1.pdf` | test PDF (A1, rooms, title block) |

---

## API Endpoints

| Method | Path | Returns |
|---|---|---|
| POST | `/upload` | `{pages, name}` |
| GET | `/page/{n}?scale=1.5&rot=0` | JPEG ภาพหน้า PDF |
| GET | `/thumb/{n}?rot=0` | JPEG thumbnail |
| GET | `/analyse/{n}?rot=0` | JSON: scale, snaps, lines, size |
| GET | `/` | HTML frontend |

---

## Features ที่ทำแล้ว

### การวัด
- [x] วัดระยะ (📏) — click 2 จุด แสดง เมตร
- [x] วัดพื้นที่ (⬡) — click หลายจุด ปิด polygon แสดง ตร.ม.
- [x] พื้นที่ประเภท ห้อง/อาคาร → แสดงแค่ ตร.ม.
- [x] พื้นที่ประเภท ที่ดิน → แสดง ตร.ม. + ไร่-งาน-วา
- [x] สอบเทียบ scale (📐) — click 2 จุด ป้อนระยะจริง
- [x] Per-page store — วัดไว้แล้วเปลี่ยนหน้ากลับมายังอยู่

### Snap
- [x] EP (endpoint), MP (midpoint), CT (center) — เปิดเริ่มต้น
- [x] NL (nearest on line), IX (intersection) — toggle ได้
- [x] OFF (ปิด snap ทั้งหมด)
- [x] Visual indicator วงกลมลอยตาม mouse + label ประเภท snap

### UI
- [x] ปุ่ม ◀ หน้า / ▶ หน้า + แสดง n / total
- [x] Thumbnail strip ซ้าย (lazy load, scroll to active)
- [x] Keyboard: ← → เปลี่ยนหน้า, F = fit, Esc = pan
- [x] Rotate ↺ ↻ (server-side, measurements ไม่หาย)
- [x] Zoom: scroll wheel, ปุ่ม + − ⊞

### สี & เลือก
- [x] Color picker inline ใน topbar (สี + opacity)
- [x] Right-click บน measurement → เปลี่ยนสี/opacity/ลบ/เปลี่ยนชื่อ
- [x] Select mode (↖) — click เลือก, drag ย้าย, double-click เปลี่ยนชื่อ, Delete ลบ
- [x] Label text สีขาว กล่องดำบาง ไม่ขึ้นกับสี shape
- [x] Label ขนาดคงที่ ~13px บนหน้าจอ ไม่ว่าจะ zoom เท่าไหร่

### Export
- [x] Export CSV
- [x] Export JSON

---

## TODO ที่ยังค้าง

### ง่าย
- [ ] Summary panel รวม area/distance ทุกหน้า
- [ ] Scale confidence badge (ต่อ scale_bar_detect.py เข้า /analyse)

### กลาง
- [ ] Multi-user session (ตอนนี้ SESSION เป็น global — single user)
- [ ] Save annotation ลง PDF (fitz page.add_line_annot())
- [ ] Per-user PDF storage (ตอนนี้ถ้า 2 คน upload พร้อมกัน ไฟล์จะชนกัน)

### ยาก
- [ ] BOQ integration — measurement → รายการวัสดุ/ราคา
- [ ] ค.1 draft — เชื่อมพื้นที่กับ docx template
- [ ] PDF scanned (ตอนนี้ใช้ได้เฉพาะ vector PDF)

---

## Coordinate System

```
PDF space (fitz)  → pt, origin top-left, 72pt = 1 inch
render_scale=1.5  → canvas px = PDF pt × 1.5
CSS transform     → translate(panX,panY) scale(zoom)
```

### Transforms
```javascript
pdfToC(px,py)  // PDF pt → canvas px  (รองรับ rotation)
cToPdf(cx,cy)  // canvas px → PDF pt  (รองรับ rotation)
```

Measurements เก็บใน **PDF pt (unrotated)** เสมอ — ปลอดภัยต่อการหมุนหน้า

---

## Deployment

```bash
# push code ใหม่ → Render auto-deploy
git add -A && git commit -m "..." && git push

# รัน local
python server.py  # http://localhost:8000
```

**Render free tier**: sleep หลัง 15 นาที → ครั้งแรกรอ ~30 วินาที
**แก้**: upgrade $7/เดือน หรือรัน local + ngrok

---

## Bugs ที่แก้ไปแล้ว

| Bug | Fix |
|---|---|
| Measurements ตามข้ามหน้า | loadPage ล้าง mPts + calibPts + pageStore |
| PDF ไม่กลางจอ | fitToWindow() คำนวณ panX/Y ถูกต้อง |
| Zoom ไม่ทำงาน | เพิ่มปุ่ม +/−/⊞ + adjustZoom() |
| Color picker ไม่ทำงาน | เปลี่ยนจาก popup → inline topbar + right-click menu |
| Label ใช้สี shape | label text ใช้ #ffffff เสมอ (strokeText กรอบดำ) |
| หน้าไม่มีปุ่มเปลี่ยน | เพิ่มปุ่ม ◀ n/total ▶ |
| Render multi-worker SESSION หาย | render.yaml --workers 1 |
