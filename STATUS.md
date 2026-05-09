# BMA Plan Proto — Status & Handoff
> อัปเดต: 2026-04-26

## ที่อยู่ไฟล์

| | Path |
|---|---|
| Local | `G:/drive/01 project/ai/bma-plan/proto/` |
| Main app | `G:/drive/01 project/ai/bma-plan/proto/server.py` |
| E2E tests | `G:/drive/01 project/ai/bma-plan/proto/e2e_ui_test.py` |
| Real permit PDF used in testing | `G:/drive/01 project/ai/bma-plan/20250616_RAMA4 APARTMENT PERMIT rev 1.pdf` |

## ตอนนี้ระบบทำอะไรได้จริง

> อัปเดต: 2026-05-05

- เปิด PDF เป็นรายเคส ไม่ชนกันข้ามไฟล์
- render หน้า PDF, thumbnail, medium thumbnail, analyse แยกตาม `case_id`
- detect scale อัตโนมัติแบบ `auto-unverified`
- manual calibration ต่อหน้า
- วัดระยะ, ระยะต่อเนื่อง, reference line, polygon พื้นที่, opening, และที่จอดรถต่อหน้า
- เลือก object แล้วแก้สี/opacity/ชื่อ และลากแก้ vertex ของ line/ref/polygon/opening ได้ทีละจุด
- summary พื้นที่ต่อหน้าใน infobar และ Check panel มีรายงานที่จอดรถ + รายงานระยะถึงเส้นอ้างอิง
- save / load `.bmaplan`
- export `CSV`, `JSON`, `XLSX`, `PDF`, `PDF + annotations`
- degraded raster mode เมื่อหน้าไม่มี vector geometry
- เปลี่ยนหน้า, หมุนหน้า, กลับมาหน้าเดิม โดย measurement ไม่หาย
- **Object picker** เมื่อคลิกซ้อน ≥2 object → popup แสดงรายการ เลือกได้ถูกตัว
- **Layer visibility** toggle ซ่อน/แสดง base_area / sub_area / deduction / labels
- **Layer lock** ล็อก layer = มองเห็นแต่คลิกเลือกไม่ได้ (🔓/🔒 ข้าง layer button)
- **Loupe magnifier** toggle เปิด/ปิด, ปรับขนาด 100–320px, crosshair สีดำบนพื้นใดก็ได้
- **Shift-constrain** (Shift ค้าง) + **Orthogonal mode** (⊖ ตั้งฉาก toggle) → ล็อก 0°/90°
- **Draw Bar** — ปุ่ม ✓จบ / ↩ลบจุด / ✗ยกเลิก ปรากฏบน canvas ขณะวาดค้าง
- **Ctrl+Z ขณะวาดค้าง** → ลบจุดล่าสุดออก (ไม่ไป undo object ที่ commit แล้ว)

## สถาปัตยกรรมปัจจุบัน

```text
Browser (HTML + JS + Canvas in server.py)
    ↕ HTTP
FastAPI app (server.py)
    ↕ PyMuPDF (render / export / fallback analysis)
    ↕ PDFium via pypdfium2 (vector snap extraction)
Uploaded PDF stored as per-case temp file
```

จุดสำคัญ:
- backend ไม่ใช้ `SESSION` global แล้ว
- ใช้ `CASES[case_id]`
- แต่ละ case เก็บ `doc`, `path`, `page_cache`, `image_cache`, `page_tags`, `project_info`
- มี TTL cleanup สำหรับเคสเก่า

## API ที่ใช้อยู่ตอนนี้

| Method | Path | หมายเหตุ |
|---|---|---|
| POST | `/upload` | คืน `{pages, name, case_id, max_upload_mb}` |
| GET | `/page/{n}?case_id=...&scale=1.5&rot=0` | render หน้า |
| GET | `/thumb/{n}?case_id=...&rot=0` | thumbnail |
| GET | `/thumb-md/{n}?case_id=...&rot=0` | medium thumbnail |
| GET | `/analyse/{n}?case_id=...&rot=0` | scale, snaps, lines, degraded mode |
| POST | `/project` | save `page_tags` + `project_info` เข้า case |
| GET | `/project?case_id=...` | อ่านกลับ |
| POST | `/export-pdf` | export subset pages และ optional annotations |

## สิ่งที่แก้ไปแล้วในรอบนี้

### Backend

- upload ไม่อ่านไฟล์ทั้งก้อนเข้า RAM แล้ว
- มี size cap, empty file check, invalid PDF check, encrypted PDF check
- page routes เช็ค bounds แล้ว
- per-case session isolation
- stale case cleanup ด้วย TTL
- `/analyse` ส่ง `degraded_mode` และ `warning`
- render/analyse cache ต่อเคสถูกจำกัดขนาดแล้ว และ `/page` reject render scale ที่อยู่นอกช่วงปลอดภัย
- export PDF รองรับ page rotation
- export `PDF + annotations` ใช้งานได้จริงแล้ว
  - เดิมพังเงียบ ๆ เพราะใช้ `page.draw_polygon()` ซึ่งไม่มีใน PyMuPDF ที่ติดตั้ง
  - แก้เป็น `draw_polyline(..., closePath=True)`
  - รองรับ annotation ของระยะต่อเนื่องหลายจุด, reference line, และ marker ที่จอดรถ
- export XLSX มี sheet สรุปพื้นที่, ความยาวเส้น polygon, สรุปตามชั้น, สรุปตามประเภท, ที่จอดรถ, และระยะอ้างอิง

### Frontend

- request ทุกตัวผูกกับ `currentCaseId`
- มี guard กัน stale page load / stale analyse response
- page load / rotate จะ reset `pageData` ก่อนเสมอ
- scale badge แยกสถานะ `manual`, `auto-unverified`, `unknown`
- raster/manual mode แสดง `manual only`
- measurement ไม่ใช้ค่าที่ cache แบบ stale หลัง recalibrate
- page summary ใช้ polygon สดของหน้าปัจจุบัน ไม่ต้องรอ save ลง store
- save/load project เก็บ `pageStore`, `pageRotations`, `pageTags`, `projectInfo`, `excludedPages`
- load project เช็คว่า PDF ตรงกับไฟล์ปัจจุบันก่อน restore
- **Page Exclusion (Soft Delete):** 
  - ซ่อนหน้า (display: none) ใน thumbnail strip เมื่อถูกกดลบใน Setup
  - ระบบเปลี่ยนหน้า (Next/Prev/Keyboard) ข้ามหน้าที่ถูกลบอัตโนมัติ
  - Page Manager (📄 หน้า) กรองหน้าทีลบออกจากการแสดงผลและการเลือก Export PDF
  - หน้าแรกหลังกด "เริ่มตรวจ" จะหาหน้าที่ไม่ถูกลบแสดงผลทันที
- **Snap / CAD-like measurement:**
  - ใช้ PDFium + spatial grid index เป็น snap engine หลัก
  - รองรับ endpoint, midpoint, center, nearest-line, intersection, perpendicular และ close polygon radius ตาม zoom
  - geometry ที่ผู้ใช้วาดเองกลายเป็น snap source ได้
- **Reference / Parking / Report:**
  - reference line ใช้กำหนดแนวถนน คลอง เขตที่ดิน ผนัง แกน หรือแนว custom
  - toggle แสดงระยะจาก object ที่เลือกถึง reference line ที่ใกล้สุด
  - Check panel แสดงรายงานที่จอดรถตามหน้า/ชั้นและรายงานระยะ object ถึงเส้นอ้างอิง

## Logic เรื่อง scale / การวัด

ระบบ “รู้ขนาดจริง” ได้ต่อเมื่ออย่างน้อยหนึ่งในนี้เป็นจริง:

- detect scale ได้จากหน้า PDF
- ผู้ใช้ manual calibration

ถ้ายังไม่มีสองอย่างนี้:
- วัดได้แค่ `pt` หรือ `pt²`
- ยังบอกเมตรหรือ ตร.ม. จริงไม่ได้

ตรรกะปัจจุบัน:

```text
raw PDF geometry
→ current page scale
→ lineMetrics / polyMetrics
→ render label / page summary / export rows
```

ผลคือ:
- recalibrate แล้วค่าที่วัดไว้ก่อนหน้า จะคำนวณใหม่จาก raw geometry
- export JSON/CSV จะใช้ scale ปัจจุบัน ไม่ใช้ค่าค้างเก่า

## ชุดทดสอบที่มี

ไฟล์: [e2e_ui_test.py](G:\drive\01 project\ai\bma-plan\proto\e2e_ui_test.py)

รองรับ 2 โหมด:

```bash
python proto/e2e_ui_test.py smoke
python proto/e2e_ui_test.py full
```

### smoke

- vector area
- recalibrate after drawing
- export JSON/CSV
- save/load project
- raster degraded mode

### full

- ทุกอย่างใน smoke
- export PDF + annotations
- multi-page persistence บนไฟล์ permit จริง
- navigate page 1 ↔ 2 บนไฟล์จริง
- rotate หน้า
- export subset PDF และตรวจ page count / rotation

## ผลทดสอบล่าสุด

รันเมื่อ 2026-05-05 ด้วย `python`:

```text
python -m py_compile proto/server.py proto/e2e_ui_test.py
python proto/e2e_ui_test.py smoke
python proto/e2e_ui_test.py full
```

ผลลัพธ์สำคัญ:

```text
CACHE_OK {'entries': 24, 'bytes': 469294, 'bad_scale': 400}
VECTOR_OK {'summary': 'อาคาร/ห้อง 165.20 · สุทธิ 165.20 ตร.ม.', 'measure': '⬡ 165.20 ตร.ม.'}
RECAL_OK {'scale': '★ 1:7 (สอบเทียบ)', 'summary': 'อาคาร/ห้อง 0.83 · สุทธิ 0.83 ตร.ม.', 'json_file': 'measurements.json', 'csv_file': 'measurements.csv'}
XLSX_OK {'summary': 'อาคาร/ห้อง 0.83 · (-)ช่องว่าง 0.11 · สุทธิ 0.72 ตร.ม.', 'xlsx_file': 'measurements_report.xlsx'}
SNAP_OK {'ix': {'x': 100, 'y': 100, 't': 'ix'}, 'perp': {'x': 100, 'y': 100, 't': 'perp'}, 'close': {'x': 100, 'y': 100, 't': 'close'}}
SELECT_OK {'landSelected': True, 'polyType': 'room', 'hit': {'type': 'opening', 'idx': 0}}
SETBACK_OK {'buildingSelected': True, 'count': 12, 'allPerp': True}
EXT_MEASURE_OK {'lineCount': 1, 'linePts': 3, 'beforeVertex': 20, 'afterVertex': 30, 'total': 5.848, 'refs': 1, 'parking': 2, 'refDistance': 4.667, 'parkingSummary': 2, 'refReportRows': 5, 'reportHasSections': True}
ANNOT_OK {'annotated_file': 'annotated_export.pdf', 'label': 'E2E_ROOM_A'}
REAL_OK {'page_label': '1 / 45', 'page_label_2': '2 / 45', 'rotation': '90°', 'export_pages': 2}
```

หมายเหตุ:
- รอบ 2026-05-05 full suite จบด้วย exit code 0
- ในรอบเก่าเคยพบ `ConnectionResetError [WinError 10054]` ตอน shutdown test harness แต่ไม่ทำให้ test fail

## ไฟล์ทดสอบที่ใช้

- `proto/test_plan_A1.pdf`
- `20250616_RAMA4 APARTMENT PERMIT rev 1.pdf`
- raster test PDF ถูกสร้างชั่วคราวจาก `test_plan_A1.pdf` ใน test script

## ช่องโหว่ / งานค้างที่สำคัญ

### สูง

- ระบบยัง “ไม่รู้ขนาดจริงเองทั้งหมด”
  - ต้องมี scale detection ที่เชื่อถือได้ หรือ calibration
  - ยังไม่ได้อ่าน dimension text ทั้งหน้าอัตโนมัติ
- PDF + annotations export ผ่านแล้ว แต่ assertion ยังตรวจเชิงโครงสร้าง ไม่ได้ตรวจ label ทุกชิ้นแบบ OCR/text-exact

### กลาง

- test harness ควร cleanup shutdown ให้เงียบกว่านี้
- ยังไม่มี CI/report format ที่อ่านง่ายสำหรับ release gate
- raster/scanned mode ยังเป็น manual assist เป็นหลัก

### ถัดไปถ้าจะทำต่อ

1. อ่าน `scale text`, `dimension text`, `scale bar`
2. สร้าง workflow calibration ที่ง่ายขึ้นต่อหน้า
3. เพิ่ม rule engine ที่จอดรถตามกฎหมายจริง หลังตรวจเอกสารอ้างอิงต้นฉบับ
4. เพิ่ม extraction ของ semantic entities
   - ที่ดิน
   - แนวอาคาร
   - ห้อง
   - บันได
   - corridor
4. เพิ่ม test สำหรับ `PDF + annotations` ให้ตรวจ object/label ละเอียดยิ่งขึ้น
5. แยก smoke/full เป็นคำสั่ง build หรือ CI step

## คำสั่งที่ใช้บ่อย

```bash
python -m py_compile proto/server.py proto/e2e_ui_test.py
python proto/e2e_ui_test.py smoke
python proto/e2e_ui_test.py full
python proto/server.py
```

## สรุปสั้น

ตอนนี้ต้นแบบอยู่ในสถานะ:

- วัดได้จริงเมื่อรู้ scale หรือ calibrate
- per-case isolation ทำงานแล้ว
- stale measurement หลัง recalibrate ถูกแก้แล้ว
- export PDF + annotations ใช้งานได้แล้ว
- มี XLSX report รวมพื้นที่/ที่จอดรถ/ระยะอ้างอิง
- มี smoke/full regression กับไฟล์จริงในโฟลเดอร์

แต่ยังไม่ใช่ระบบที่ “อ่านขนาดทั้งแบบอัตโนมัติครบทุกอย่าง” เพราะส่วน `dimension / scale extraction` ยังไม่ได้ทำ
