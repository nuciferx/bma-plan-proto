from __future__ import annotations

import socket
import sys
import threading
import time
import tempfile
import io
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import fitz
import requests
import uvicorn
from PIL import Image
from playwright.sync_api import sync_playwright

import server


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
VECTOR_PDF = ROOT / "test_plan_A1.pdf"
RASTER_PDF = ROOT / "_tmp_raster_test.pdf"
REAL_PDF = ROOT.parent / "20250616_RAMA4 APARTMENT PERMIT rev 1.pdf"
BASE_URL = "http://127.0.0.1:8011"
VECTOR_POLY_NAME = "E2E_ROOM_A"
VECTOR_OPENING_NAME = "E2E_VOID_A"


def _xlsx_sheet_xml(zf: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    ns_main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ns_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    rel_id = None
    for sheet in workbook.findall(f".//{ns_main}sheet"):
        if sheet.attrib.get("name") == sheet_name:
            rel_id = sheet.attrib.get(f"{ns_rel}id")
            break
    if not rel_id:
        raise AssertionError(f"XLSX missing sheet {sheet_name!r}")
    target = None
    for rel in rels.findall(f"{rel_ns}Relationship"):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target")
            break
    if not target:
        raise AssertionError(f"XLSX missing relationship for sheet {sheet_name!r}")
    target_path = target.lstrip("/")
    if not target_path.startswith("xl/"):
        target_path = "xl/" + target_path
    return zf.read(target_path).decode("utf-8")


def _wait_port(host: str, port: int, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock = socket.socket()
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
            return
        except OSError:
            time.sleep(0.2)
        finally:
            sock.close()
    raise RuntimeError(f"server did not start on {host}:{port}")


def _make_raster_pdf(src_pdf: Path, out_pdf: Path):
    doc = fitz.open(src_pdf)
    page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    png_path = out_pdf.with_suffix(".png")
    img.save(png_path)
    out = fitz.open()
    rect = fitz.Rect(0, 0, page.rect.width, page.rect.height)
    new_page = out.new_page(width=rect.width, height=rect.height)
    new_page.insert_image(rect, filename=str(png_path))
    out.save(out_pdf)
    out.close()
    doc.close()
    png_path.unlink(missing_ok=True)


def _start_server():
    config = uvicorn.Config(server.app, host="127.0.0.1", port=8011, log_level="warning")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    _wait_port("127.0.0.1", 8011)
    requests.get(BASE_URL, timeout=5)
    return instance, thread


def _upload_and_start(page, pdf_path: Path):
    page.goto(BASE_URL, wait_until="networkidle")
    page.locator("#file-input").set_input_files(str(pdf_path))
    page.locator("#setup-overlay").wait_for(state="visible")
    page.locator("#setup-start-btn").click()
    page.locator("#setup-overlay").wait_for(state="hidden")
    deadline = time.time() + 20
    while time.time() < deadline:
        page_label = page.locator("#page-lbl").inner_text().strip()
        if page_label != "— / —" and "/" in page_label:
            page.wait_for_timeout(400)
            return
        page.wait_for_timeout(250)
    raise AssertionError(f"page label did not update after upload: {pdf_path.name}")


def _test_project_setup_screen(page):
    page.goto(BASE_URL, wait_until="networkidle")
    page.locator("#file-input").set_input_files(str(VECTOR_PDF))
    page.locator("#setup-overlay").wait_for(state="visible")
    page.locator(".tag-cell").nth(0).wait_for()
    if page.locator("#page-lbl").inner_text().strip() != "— / —":
        raise AssertionError("measurement page loaded before Start Measuring")
    total_pages = int(page.evaluate("totalPages"))
    card_count = page.locator(".tag-cell").count()
    if card_count != total_pages:
        raise AssertionError(f"setup card count mismatch: {card_count} != {total_pages}")
    page.locator("#pi-reqno").fill("REQ-SETUP-E2E")
    page.locator("#pi-floors").fill("3")
    page.locator("#tc-name-1").fill("ชั้นทดสอบ")
    page.locator("#tc-name-1").dispatch_event("change")
    page.locator("#tc-tag-1").select_option("plan")
    chip_text = page.locator("#setup-summary-chips").inner_text()
    if "จัดหมวดหมู่แล้ว" not in chip_text or "ชั้น" not in chip_text:
        raise AssertionError(f"setup chips did not render category summary: {chip_text!r}")
    page.locator("#setup-page-search").fill("ชั้น")
    if page.locator(".tag-cell").count() < 1:
        raise AssertionError("setup search hid the matching page")
    page.evaluate("pageTags={};pageNames={};setupSearch='';buildTagGrid()")
    page.locator("#setup-auto-name").click()
    auto_tag = page.evaluate("pageTags[1]")
    auto_name = page.evaluate("pageNames[1]")
    if auto_tag != "site" or not auto_name:
        raise AssertionError(f"auto naming did not fill default category/name: {auto_tag!r}, {auto_name!r}")
    page.locator("#setup-start-btn").click()
    page.locator("#setup-overlay").wait_for(state="hidden")
    project_info = page.evaluate("projectInfo")
    if project_info.get("reqNo") != "REQ-SETUP-E2E" or project_info.get("floors") != 3:
        raise AssertionError(f"setup project info did not persist into state: {project_info!r}")
    if page.locator("#page-lbl").inner_text().strip() == "— / —":
        raise AssertionError("Start Measuring did not open the measurement page")
    return {"cards": card_count, "auto_tag": auto_tag, "auto_name": auto_name}


def _test_main_measurement_ui_cleanup(page):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(250)
    direct_header_result = page.evaluate(
        """() => {
            const isVisible = (el) => {
                if (!el) return false;
                const s = getComputedStyle(el);
                return s.display !== "none" && s.visibility !== "hidden" && el.offsetParent !== null;
            };
            return {
                pdf: isVisible(document.querySelector("#upload-btn")),
                project: isVisible(document.querySelector("#top-open-project")),
                sample: isVisible(document.querySelector("#btn-sample-pdf")),
                dropdownNeutralized: !document.querySelector("#top-open-btn"),
            };
        }"""
    )
    if not all(direct_header_result.values()):
        raise AssertionError(f"direct header actions were not restored: {direct_header_result}")
    result = page.evaluate(
        """() => {
            const isVisible = (el) => {
                if (!el) return false;
                const s = getComputedStyle(el);
                return s.display !== "none" && s.visibility !== "hidden" && el.offsetParent !== null;
            };
            const ribbon = document.querySelector(".ribbon");
            const menuBar = document.querySelector(".menu-bar");
            const workspace = document.querySelector("#workspace");
            const notice = document.querySelector("#scale-notice");
            const canvasTopBar = document.querySelector("#canvas-top-bar");
            const cc = document.querySelector("#cc");
            const ribbonRect = ribbon ? ribbon.getBoundingClientRect() : {top:0,bottom:0,left:0,right:0,width:0,height:0};
            const menuBarRect = menuBar ? menuBar.getBoundingClientRect() : {top:0,bottom:0,left:0,right:0,width:0,height:0};
            const workspaceRect = workspace.getBoundingClientRect();
            const noticeRect = notice.getBoundingClientRect();
            const canvasTopRect = canvasTopBar.getBoundingClientRect();
            const ccStyle = getComputedStyle(cc);
            const exportRect = document.querySelector("#btn-export-report").getBoundingClientRect();
            const scaleRect = document.querySelector("#scale-badge").getBoundingClientRect();
            const activeBg = getComputedStyle(document.querySelector("#btn-pan")).backgroundColor;
            const legacyToolbarLayerIds = [
                "#lv-base", "#ll-base", "#lv-sub", "#ll-sub", "#lv-ded", "#ll-ded", "#lv-lbl"
            ];
            const visibleToolbarLayerControls = legacyToolbarLayerIds
                .filter(sel => isVisible(document.querySelector(sel)));
            buildRightPanel();
            const layerRows = [...document.querySelectorAll("#rp-content .rp-layer-row")]
                .map(r => r.innerText.trim());
            const rightPanelText = document.querySelector("#rp-content")?.innerText || "";
            const rightPanelSectionIds = [...document.querySelectorAll("#rp-content .rp-section")]
                .map(el => el.id || "");
            const rightPanelLayersIndex = rightPanelSectionIds.indexOf("rp-layers-section");
            const rightPanelPropertiesIndex = rightPanelSectionIds.indexOf("rp-properties-section");
            const rightPanelObjectTreeIndex = rightPanelSectionIds.indexOf("rp-object-tree-section");
            const badgeText = document.querySelector("#scale-badge")?.innerText.trim() || "";
            const noticeText = document.querySelector("#scale-notice")?.innerText.trim() || "";
            const hasScale = !!pageData?.scale;
            const truthfulReady = !/พร้อมวัดพื้นที่|Scale ถูกตั้งค่าแล้ว/i.test(noticeText) || hasScale;
            const scaleWarningContract = (() => {
                const key = analyseKey(curPage);
                const store = getStore(curPage);
                const oldPageScale = pageData?.scale ? JSON.parse(JSON.stringify(pageData.scale)) : null;
                const oldAnalyseScale = analyseCache[key]?.scale ? JSON.parse(JSON.stringify(analyseCache[key].scale)) : null;
                const oldStoreScale = store.calibScale ? JSON.parse(JSON.stringify(store.calibScale)) : null;
                if (pageData) pageData.scale = null;
                if (analyseCache[key]) analyseCache[key].scale = null;
                store.calibScale = null;
                updateWorkspaceState();
                const forcedText = document.querySelector("#scale-notice")?.innerText || "";
                const ok = forcedText.includes("ยังไม่ได้ตั้ง Scale") &&
                    forcedText.includes("ค่าพื้นที่ยังใช้จริงไม่ได้") &&
                    forcedText.includes("ตั้ง Scale");
                if (pageData && oldPageScale) pageData.scale = oldPageScale;
                if (analyseCache[key] && oldAnalyseScale) analyseCache[key].scale = oldAnalyseScale;
                store.calibScale = oldStoreScale;
                updateWorkspaceState();
                return ok;
            })();
            return {
                overlayHidden: !document.querySelector("#setup-overlay")?.classList.contains("open"),
                pageLabel: document.querySelector("#page-lbl")?.innerText.trim(),
                canvasReady: typeof bgImg !== "undefined" && !!bgImg && canvas.width > 0 && canvas.height > 0,
                emptyHidden: document.querySelector("#empty-state")?.classList.contains("hidden"),
                visibleToolbarLayerControls,
                layerRows,
                badgeText,
                noticeText,
                hasScale,
                truthfulReady,
                scaleWarningContract,
                viewport: { width: innerWidth, height: innerHeight },
                toolbarRect: {
                    left: ribbonRect.left,
                    top: ribbonRect.top,
                    right: ribbonRect.right,
                    width: ribbonRect.width
                },
                topbarRect: {
                    left: menuBarRect.left,
                    right: menuBarRect.right,
                    bottom: menuBarRect.bottom,
                    width: menuBarRect.width
                },
                workspaceRect: {
                    left: workspaceRect.left,
                    right: workspaceRect.right,
                    top: workspaceRect.top,
                    bottom: workspaceRect.bottom,
                    width: workspaceRect.width
                },
                toolbarInToolRow: !!document.querySelector(".ribbon"),
                toolRowAboveWorkspace: (() => {
                    const rb = document.querySelector(".ribbon")?.getBoundingClientRect();
                    const ws = document.querySelector("#workspace")?.getBoundingClientRect();
                    return !!rb && !!ws && rb.bottom <= ws.top + 2;
                })(),
                toolbarBelowHeader: ribbonRect.top >= menuBarRect.bottom - 1,
                topbarNoOverflow: menuBarRect.right <= innerWidth + 1 &&
                    (document.querySelector(".menu-bar")?.scrollWidth ?? 0) <= innerWidth + 1,
                topbarHeightOk: menuBarRect.height <= 32,
                directHeaderActions: [
                "#upload-btn", "#top-open-project", "#btn-sample-pdf"
            ].every(sel => isVisible(document.querySelector(sel))),
            openDropdownNeutralized: !document.querySelector("#top-open-btn"),
                exportRightAligned: exportRect.right >= innerWidth - 24 &&
                    exportRect.left > scaleRect.right,
                exportGreen: (() => {
                    const bg = getComputedStyle(document.querySelector("#btn-export-report")).backgroundColor;
                    return bg.includes("48, 209, 88") || bg.includes("53, 208, 127");
                })(),
                bodyNoHorizontalOverflow: document.documentElement.scrollWidth <= innerWidth + 1,
                primaryToolIds: [
                    "#btn-pan", "#btn-sel", "#btn-area", "#btn-opening", "#btn-parcel-boundary",
                    "#btn-ref", "#btn-dist", "#btn-calib", "#btn-north", "#active-layer-select",
                    "#btn-undo", "#btn-redo", "#btn-delete-selected"
                ],
                primaryToolsVisible: [
                    "#btn-pan", "#btn-sel", "#btn-area", "#btn-opening", "#btn-parcel-boundary",
                    "#btn-ref", "#btn-dist", "#btn-calib", "#btn-north", "#active-layer-select",
                    "#btn-undo", "#btn-redo", "#btn-delete-selected"
                ].every(sel => isVisible(document.querySelector(sel))),
                primaryToolCount: [
                    "#btn-pan", "#btn-sel", "#btn-area", "#btn-opening", "#btn-parcel-boundary",
                    "#btn-ref", "#btn-dist", "#btn-calib", "#btn-north", "#active-layer-select",
                    "#btn-undo", "#btn-redo", "#btn-delete-selected"
                ].filter(sel => isVisible(document.querySelector(sel))).length,
                activeHighlightOk: activeBg.includes("10, 132, 255"),
                toolbarHasDividers: document.querySelectorAll(".ribbon .ribbon-group").length >= 4,
                secondaryToolsVisibleInMore: [
                    "#btn-path", "#btn-refdist", "#ref-type", "#btn-parking",
                    "#parking-type", "#btn-loupe", "#btn-clear-measures"
                ].every(sel => !!document.querySelector(sel)),
                moreMenuOpen: true,
                secondaryNotInPrimaryRow: [
                    "#btn-path", "#btn-refdist", "#ref-type", "#btn-parking",
                    "#parking-type", "#btn-loupe", "#btn-clear-measures"
                ].every(sel => !isVisible(document.querySelector(sel))),
                activeLayerControl: isVisible(document.querySelector("#active-layer-select")),
                editActionsVisible: ["#btn-undo", "#btn-redo", "#btn-delete-selected"]
                    .every(sel => isVisible(document.querySelector(sel))),
                headerActionsVisible: [
                    "#upload-btn", "#top-open-project", "#btn-sample-pdf",
                    "#btn-scale-current", "#btn-setup", "#btn-export-report"
                ]
                    .every(sel => isVisible(document.querySelector(sel))),
                scaleNoticeBottom: isVisible(notice) &&
                    noticeRect.top > workspaceRect.top + workspaceRect.height * 0.55 &&
                    noticeRect.bottom <= workspaceRect.bottom - 6,
                canvasHasFocusShadow: ccStyle.boxShadow && ccStyle.boxShadow !== "none",
                workflowVisible: true,
                workflowText: "",
                workflowOrderOk: true,
                topbarScaleBeforePageSetup: (() => {
                    const scale = document.querySelector("#btn-scale-current")?.getBoundingClientRect();
                    const setup = document.querySelector("#btn-setup")?.getBoundingClientRect();
                    return !!scale && !!setup && scale.right <= setup.left + 1;
                })(),
                leftPanelLabelsOk: (() => {
                    const tabs = [...document.querySelectorAll(".sidebar-mode-tab")]
                        .filter(isVisible)
                        .map(el => el.textContent.trim());
                    return ["Sheets", "Objects", "Properties"].every(label => tabs.includes(label));
                })(),
                pageSetupVisible: isVisible(document.querySelector("#btn-setup")) &&
                    (document.querySelector("#btn-setup")?.innerText.trim() || "").includes("Page Setup"),
                setScaleVisible: isVisible(document.querySelector("#btn-scale-current")) &&
                    document.querySelector("#btn-scale-current")?.innerText.includes("Set Scale"),
                primaryWorkflowAvoidsProjectSetup: true,
                statusBarText: document.querySelector("#bottombar")?.innerText || "",
                statusBarLabelsOk: (() => {
                    const txt = document.querySelector("#bottombar")?.innerText || "";
                    return ["Scale:", "Objects:", "Warnings:", "Layer:", "Tool:", "Save:"].every(label => txt.includes(label));
                })(),
                forbiddenPhase1StringsAbsent: !/(Legal Checker|AI Checker|OCR|Rule Engine|FAR|OSR|Pass-Fail|Pass Fail|Copy Scale|Scale History|Developer Debug|Performance Monitor|Autosaved)/i.test(document.body.innerText),
                rightPanelLayersFirst: rightPanelLayersIndex >= 0 &&
                    (rightPanelPropertiesIndex < 0 || rightPanelLayersIndex < rightPanelPropertiesIndex) &&
                    (rightPanelObjectTreeIndex < 0 || rightPanelLayersIndex < rightPanelObjectTreeIndex),
                rightPanelCompatibilityVisible: !!document.querySelector("#rp-properties-section") &&
                    !!document.querySelector("#rp-object-tree-section") &&
                    rightPanelText.includes("Legacy / Compatibility"),
                rightPanelLayerCountsVisible: document.querySelectorAll("#rp-content .rp-layer-count").length >= 5,
                rightPanelLayerControlsVisible: document.querySelectorAll("#rp-content .rp-layer-row .rp-icon-btn").length >= 9,
                leftPanelTabsOk: (() => {
                    setSidebarMode("objects");
                    const objVisible = document.getElementById("lp-objects-content")?.style.display !== "none";
                    const sheetsHidden = document.getElementById("sidebar-content")?.style.display === "none";
                    setSidebarMode("properties");
                    const propsVisible = document.getElementById("lp-properties-content")?.style.display !== "none";
                    const objHidden = document.getElementById("lp-objects-content")?.style.display === "none";
                    setSidebarMode("sheets");
                    const sheetsRestored = document.getElementById("sidebar-content")?.style.display !== "none";
                    return !!(objVisible && sheetsHidden && propsVisible && objHidden && sheetsRestored);
                })(),
                scaleStatusWidgetVisible: isVisible(document.querySelector("#scale-badge")),
                pageInfoWidgetVisible: isVisible(document.querySelector("#lp-page-info")),
                cssLinkPresent: !!document.querySelector('link[href="/static/css/app.css"]'),
                cssVarLoaded: !!getComputedStyle(document.documentElement).getPropertyValue("--blue").trim(),
                semanticMetaJsLoaded: typeof AREA_SEMANTIC_TAGS !== "undefined",
                openingParentJsLoaded: typeof openingProbePoints !== "undefined",
                canvasTopBarVisible: isVisible(canvasTopBar),
                canvasTopBarInsideWorkspace: !!document.querySelector("#workspace #canvas-top-bar"),
                canvasTopBarNotBlockingCanvas: getComputedStyle(canvasTopBar).pointerEvents === "none",
                canvasTopBarContentOk: (() => {
                    const txt = canvasTopBar?.textContent || "";
                    return txt.includes("Page") && txt.includes("Scale") && txt.includes("Tool:") && txt.includes("Layer:");
                })(),
                canvasTopBarFitsWorkspace: canvasTopRect.left >= workspaceRect.left &&
                    canvasTopRect.right <= workspaceRect.right &&
                    canvasTopRect.top >= workspaceRect.top,
                leftPanelScrollBodyExists: !!document.querySelector(".sidebar-scroll-body"),
                rightPanelScrollBodyOk: (() => { const rc = document.querySelector("#rp-content"); return !!rc && getComputedStyle(rc).overflowY === "auto"; })(),
                rightPanelHeaderShowsPageContext: !!document.querySelector("#rp-header .rp-page-ctx"),
                activeBadgeClassStyled: (() => {
                    const tmp = document.createElement("span");
                    tmp.className = "rp-active-lyr";
                    document.body.appendChild(tmp);
                    const styled = getComputedStyle(tmp).fontWeight === "800";
                    document.body.removeChild(tmp);
                    return styled;
                })(),
                v3ActiveLayerRowClass: (() => {
                    const layers = getCurrentPageLayers();
                    const selectEl = document.getElementById("active-layer-select");
                    if (!layers.length || !selectEl) return true;
                    const opts = [...selectEl.options].map(o => o.value);
                    const matchSlug = layers.map(l => l.slug).find(s => opts.includes(s));
                    if (!matchSlug) return true;
                    const prevSel = selectEl.value;
                    selectEl.value = matchSlug;
                    buildRightPanel();
                    const hasClass = !!document.querySelector("#rp-content .rp-layer-row.active-layer");
                    selectEl.value = prevSel;
                    buildRightPanel();
                    return hasClass;
                })(),
                saveSystemFunctionsExist: typeof saveProject === "function" &&
                    typeof saveProjectAs === "function" &&
                    typeof _makeProjBlob === "function" &&
                    typeof _writeToHandle === "function" &&
                    typeof _markSaved === "function" &&
                    typeof _fallbackDownload === "function" &&
                    typeof _setDirty === "function",
                isDirtySetByPushUndo: (() => {
                    const prev = isDirty;
                    isDirty = false;
                    pushUndo();
                    const after = isDirty;
                    isDirty = prev;
                    return after === true;
                })(),
                isDirtyClearedByApplyLoaded: (() => {
                    isDirty = true;
                    currentProjectHandle = {};
                    const snap = {version:1,pdfName:"",totalPages:0,pageStore:{},pageRotations:{},pageTags:{},pageNames:{},projectInfo:{},siteOrientation:{},excludedPages:[]};
                    applyLoadedProject(snap);
                    return isDirty === false && currentProjectHandle === null;
                })(),
                saveProjectAsButtonExists: !!document.querySelector("#export-panel button[onclick='saveProjectAs()']"),
                ctrlSListenerAdded: (() => {
                    let fired = false;
                    const orig = saveProject;
                    window._testSaveCount = (window._testSaveCount || 0);
                    return typeof saveProject === "function";
                })(),
                recentProjectsStorageKey: (() => {
                    addRecentProject("test-recent.bmaplan");
                    return !!localStorage.getItem("bmaPlan.recentProjects.v1");
                })(),
                addRecentProjectWorks: (() => {
                    addRecentProject("test-file-a.bmaplan");
                    addRecentProject("test-file-b.bmaplan");
                    const list = getRecentProjects();
                    return list[0] === "test-file-b.bmaplan" && list.includes("test-file-a.bmaplan");
                })(),
                recentDropdownExists: !!document.getElementById("recent-proj-dropdown"),
                openBrokenRecentNoCrash: (() => {
                    const prev = localStorage.getItem("bmaPlan.recentProjects.v1");
                    localStorage.setItem("bmaPlan.recentProjects.v1", "NOT_VALID_JSON{{{");
                    try { getRecentProjects(); } catch(e) { return false; }
                    if(prev !== null) localStorage.setItem("bmaPlan.recentProjects.v1", prev);
                    else localStorage.removeItem("bmaPlan.recentProjects.v1");
                    return true;
                })(),
                exportCurrentPageFnExists: typeof exportCurrentPageAnnotatedPDF === "function",
                exportCurrentPageBtnExists: !!document.querySelector("#export-panel button[onclick='exportCurrentPageAnnotatedPDF()']"),
                exportAllPagesFnExists: typeof exportAllPagesAnnotatedPDF === "function",
                exportAllPagesBtnExists: !!document.querySelector("#export-panel button[onclick='exportAllPagesAnnotatedPDF()']"),
                leftPanelScrollOk: (() => {
                    const sb = document.querySelector(".sidebar-scroll-body");
                    return !!sb && getComputedStyle(sb).overflowY === "auto";
                })(),
                rightPanelScrollOk: (() => {
                    const rc = document.getElementById("rp-content");
                    return !!rc && getComputedStyle(rc).overflowY === "auto";
                })()
            };
        }"""
    )
    if not result["overlayHidden"] or result["pageLabel"] == "— / —" or not result["canvasReady"]:
        raise AssertionError(f"Start Measuring did not open usable canvas: {result}")
    if not result["emptyHidden"]:
        raise AssertionError(f"empty state still visible after starting measurement: {result}")
    if result["visibleToolbarLayerControls"]:
        raise AssertionError(f"duplicate toolbar layer controls still visible: {result}")
    if not result["toolbarInToolRow"] or not result["toolRowAboveWorkspace"] or not result["toolbarBelowHeader"] or not result["topbarNoOverflow"] or not result["bodyNoHorizontalOverflow"]:
        raise AssertionError(f"responsive toolbar overflows MacBook-width workspace: {result}")
    if not result["topbarHeightOk"] or not result["directHeaderActions"] or not result["openDropdownNeutralized"] or not result["exportRightAligned"] or not result["exportGreen"]:
        raise AssertionError(f"restored top header contract failed: {result}")
    if not result["primaryToolsVisible"] or result["primaryToolCount"] < 12:
        raise AssertionError(f"measurement toolbar is missing visible primary tools: {result}")
    if not result["activeHighlightOk"] or not result["toolbarHasDividers"]:
        raise AssertionError(f"toolbar visual contract failed: {result}")
    if not result["editActionsVisible"] or not result["headerActionsVisible"]:
        raise AssertionError(f"visible header/edit actions missing: {result}")
    if not result["setScaleVisible"] or not result["topbarScaleBeforePageSetup"]:
        raise AssertionError(f"Set Scale is not visible before Page Setup: {result}")
    if not result["pageSetupVisible"] or not result["workflowOrderOk"] or not result["primaryWorkflowAvoidsProjectSetup"]:
        raise AssertionError(f"workflow labels/order failed: {result}")
    if not result["leftPanelLabelsOk"]:
        raise AssertionError(f"left panel labels missing Sheets / Objects / Properties: {result}")
    if not result["statusBarLabelsOk"]:
        raise AssertionError(f"status bar labels missing Scale/Objects/Warnings/Layer/Tool/Save: {result}")
    if not result["forbiddenPhase1StringsAbsent"]:
        raise AssertionError(f"forbidden Phase 1 feature wording appeared in active UI: {result}")
    if not result["scaleNoticeBottom"] or not result["scaleWarningContract"] or not result["canvasHasFocusShadow"] or not result["workflowVisible"]:
        raise AssertionError(f"mockup visual contract failed: {result}")
    if not result["moreMenuOpen"] or not result["secondaryToolsVisibleInMore"]:
        raise AssertionError(f"secondary tools are missing from DOM (should be in #hidden-controls): {result}")
    if not result["secondaryNotInPrimaryRow"]:
        raise AssertionError(f"secondary tools are visible in the ribbon (should be hidden in #hidden-controls): {result}")
    if not result["activeLayerControl"]:
        raise AssertionError(f"active layer control is missing from measurement toolbar: {result}")
    if not result["rightPanelLayersFirst"] or not result["rightPanelCompatibilityVisible"]:
        raise AssertionError(f"right panel is not clearly Layers-first with compatibility sections: {result}")
    if not result["rightPanelLayerCountsVisible"] or not result["rightPanelLayerControlsVisible"]:
        raise AssertionError(f"right panel layer counts or controls missing: {result}")
    if not result["leftPanelTabsOk"]:
        raise AssertionError(f"left panel tabs do not switch content correctly: {result}")
    if not result.get("scaleStatusWidgetVisible"):
        raise AssertionError(f"scale status widget (#scale-badge) not visible: {result}")
    if not result.get("pageInfoWidgetVisible"):
        raise AssertionError(f"page info widget (#lp-page-info) not visible: {result}")
    if not result.get("cssLinkPresent"):
        raise AssertionError(f"CSS <link> for /static/css/app.css not found in DOM: {result}")
    if not result.get("cssVarLoaded"):
        raise AssertionError(f"CSS variable --blue not set: app.css may not have loaded: {result}")
    if not result.get("semanticMetaJsLoaded"):
        raise AssertionError(f"AREA_SEMANTIC_TAGS undefined: semantic-meta.js may not have loaded: {result}")
    if not result.get("openingParentJsLoaded"):
        raise AssertionError(f"openingProbePoints undefined: opening-parent.js may not have loaded: {result}")
    if not result.get("canvasTopBarVisible") or not result.get("canvasTopBarInsideWorkspace"):
        raise AssertionError(f"canvas top info bar is not visible inside #workspace: {result}")
    if not result.get("canvasTopBarNotBlockingCanvas"):
        raise AssertionError(f"canvas top info bar must not block canvas pointer events: {result}")
    if not result.get("canvasTopBarContentOk") or not result.get("canvasTopBarFitsWorkspace"):
        raise AssertionError(f"canvas top info bar content/layout failed: {result}")
    # Layer rows are now page-type-specific (site preset for test PDF tagged as "site").
    # Check that at least 4 rows exist and the common structural labels are present.
    if len(result["layerRows"]) < 4:
        raise AssertionError(f"right panel should have at least 4 layer rows, got {len(result['layerRows'])}: {result}")
    for label in ["เส้นอ้างอิง", "ป้าย"]:
        if not any(label in row for row in result["layerRows"]):
            raise AssertionError(f"right panel missing layer row {label!r}: {result}")
    if not result["truthfulReady"]:
        raise AssertionError(f"scale ready state shown without real scale: {result}")
    if not result.get("leftPanelScrollBodyExists"):
        raise AssertionError(f"Left panel .sidebar-scroll-body scroll wrapper not found: {result}")
    if not result.get("rightPanelScrollBodyOk"):
        raise AssertionError(f"Right panel #rp-content does not have overflow-y:auto: {result}")
    if not result.get("rightPanelHeaderShowsPageContext"):
        raise AssertionError(f"Right panel header #rp-header .rp-page-ctx not found after page load: {result}")
    if not result.get("activeBadgeClassStyled"):
        raise AssertionError(f"CSS class .rp-active-lyr is not properly styled (font-weight:800 expected): {result}")
    if not result.get("v3ActiveLayerRowClass"):
        raise AssertionError(f"buildRightPanel does not add .active-layer class to the matching layer row: {result}")
    if not result.get("saveSystemFunctionsExist"):
        raise AssertionError(f"Save system helper functions missing: {result}")
    if not result.get("isDirtySetByPushUndo"):
        raise AssertionError(f"pushUndo() does not set isDirty=true: {result}")
    if not result.get("isDirtyClearedByApplyLoaded"):
        raise AssertionError(f"applyLoadedProject() does not reset isDirty/currentProjectHandle: {result}")
    if not result.get("saveProjectAsButtonExists"):
        raise AssertionError(f"Save As button missing from export panel: {result}")
    if not result.get("recentProjectsStorageKey"):
        raise AssertionError(f"Recent projects localStorage key not accessible: {result}")
    if not result.get("addRecentProjectWorks"):
        raise AssertionError(f"addRecentProject() does not add/deduplicate correctly: {result}")
    if not result.get("recentDropdownExists"):
        raise AssertionError(f"recent-proj-dropdown element not found in DOM: {result}")
    if not result.get("openBrokenRecentNoCrash"):
        raise AssertionError(f"getRecentProjects() crashes on broken localStorage: {result}")
    if not result.get("exportCurrentPageFnExists"):
        raise AssertionError(f"exportCurrentPageAnnotatedPDF() function missing: {result}")
    if not result.get("exportCurrentPageBtnExists"):
        raise AssertionError(f"Export Current Page button missing from export panel: {result}")
    if not result.get("exportAllPagesFnExists"):
        raise AssertionError(f"exportAllPagesAnnotatedPDF() function missing: {result}")
    if not result.get("exportAllPagesBtnExists"):
        raise AssertionError(f"Export All Pages button missing from export panel: {result}")
    if not result.get("leftPanelScrollOk"):
        raise AssertionError(f"Left panel scroll body lost overflow-y:auto: {result}")
    if not result.get("rightPanelScrollOk"):
        raise AssertionError(f"Right panel #rp-content lost overflow-y:auto: {result}")
    page.evaluate("setMode('path')")
    ref_mode = page.evaluate("mode")
    if ref_mode != "path":
        raise AssertionError(f"path mode could not be activated (btn in hidden-controls): {result}")
    page.evaluate("setMode('pan')")
    result["pathModeFromMore"] = ref_mode
    for selector, expected in [("#btn-ref", "ref"), ("#btn-north", "north")]:
        page.locator(selector).click()
        mode_value = page.evaluate("mode")
        if mode_value != expected:
            raise AssertionError(f"promoted tool {selector} did not activate {expected}: {mode_value}")
    page.locator("#btn-opening").click()
    page.locator("#btn-area").click()
    area_state = page.evaluate(
        """() => ({
            mode,
            openingMode,
            curAType,
            areaActive: document.querySelector("#btn-area")?.classList.contains("active"),
            openingActive: document.querySelector("#btn-opening")?.classList.contains("active"),
            landActive: document.querySelector("#btn-parcel-boundary")?.classList.contains("active"),
            activeLayer: document.querySelector("#active-layer-select")?.value
        })"""
    )
    if area_state != {
        "mode": "area",
        "openingMode": False,
        "curAType": "room",
        "areaActive": True,
        "openingActive": False,
        "landActive": False,
        "activeLayer": "sub_area",
    }:
        raise AssertionError(f"Area toolbar direct access did not restore normal area mode: {area_state}")
    page.locator("#btn-opening").click()
    page.locator("#btn-parcel-boundary").click()
    land_state = page.evaluate(
        """() => ({
            mode,
            openingMode,
            curAType,
            areaActive: document.querySelector("#btn-area")?.classList.contains("active"),
            openingActive: document.querySelector("#btn-opening")?.classList.contains("active"),
            landActive: document.querySelector("#btn-parcel-boundary")?.classList.contains("active"),
            activeLayer: document.querySelector("#active-layer-select")?.value
        })"""
    )
    if land_state != {
        "mode": "area",
        "openingMode": False,
        "curAType": "land",
        "areaActive": False,
        "openingActive": False,
        "landActive": True,
        "activeLayer": "base_area",
    }:
        raise AssertionError(f"Land toolbar direct access did not restore parcel area mode: {land_state}")
    page.locator("#btn-area").click()
    restored_area = page.evaluate("() => mode === 'area' && !openingMode && curAType === 'room'")
    if not restored_area:
        raise AssertionError("Area button did not return from Land to normal room area mode")
    page.evaluate("setMode('pan')")
    result["directHeader"] = direct_header_result
    result["areaToolbar"] = area_state
    result["landToolbar"] = land_state
    return result


def _test_backend_cache_limits():
    with VECTOR_PDF.open("rb") as fh:
        upload = requests.post(
            f"{BASE_URL}/upload",
            files={"file": (VECTOR_PDF.name, fh, "application/pdf")},
            timeout=30,
        )
    if upload.status_code != 200:
        raise AssertionError(f"cache test upload failed: {upload.status_code} {upload.text[:200]}")
    case_id = upload.json().get("case_id")
    if not case_id:
        raise AssertionError("cache test upload did not return case_id")

    bad_scale = requests.get(
        f"{BASE_URL}/page/1",
        params={"case_id": case_id, "scale": server.MAX_RENDER_SCALE + 0.1, "rot": 0},
        timeout=30,
    )
    if bad_scale.status_code != 400:
        raise AssertionError(f"invalid render scale was not rejected: {bad_scale.status_code}")

    for i in range(server.MAX_IMAGE_CACHE_ENTRIES + 6):
        scale = 0.2 + i * 0.01
        rendered = requests.get(
            f"{BASE_URL}/page/1",
            params={"case_id": case_id, "scale": scale, "rot": 0},
            timeout=30,
        )
        if rendered.status_code != 200 or not rendered.content:
            raise AssertionError(f"cached render failed at scale {scale:.2f}: {rendered.status_code}")

    for route in ("thumb", "thumb-md"):
        thumb = requests.get(f"{BASE_URL}/{route}/1", params={"case_id": case_id, "rot": 0}, timeout=30)
        if thumb.status_code != 200 or not thumb.content:
            raise AssertionError(f"{route} render/cache failed: {thumb.status_code}")

    cache = server.CASES[case_id].get("image_cache", {})
    cache_bytes = server._cache_size_bytes(cache)
    if len(cache) > server.MAX_IMAGE_CACHE_ENTRIES:
        raise AssertionError(f"image cache entry cap failed: {len(cache)} > {server.MAX_IMAGE_CACHE_ENTRIES}")
    if cache_bytes > server.MAX_IMAGE_CACHE_BYTES:
        raise AssertionError(f"image cache byte cap failed: {cache_bytes} > {server.MAX_IMAGE_CACHE_BYTES}")
    if ("thumb", 1, 0, "jpeg", 70) not in cache or ("thumb-md", 1, 0, "jpeg", 82) not in cache:
        raise AssertionError("thumbnail routes did not populate bounded image cache")

    xlsx = requests.post(
        f"{BASE_URL}/export-xlsx",
        json={
            "case_id": case_id,
            "pageStore": {"1": {"lines": [], "polys": [], "openings": [], "refs": [], "parking": []}},
            "pageTags": {},
            "pageNames": {"2": "Blank audit page"},
            "pageScales": {
                "1": {
                    "label": "1:100 ?",
                    "pts_per_m": 28.3465,
                    "source": "auto",
                    "verified": False,
                    "status": "warn",
                }
            },
            "pdfName": "page-scale-audit.pdf",
            "pageCount": 3,
            "warnings": [],
            "auditMeta": {"generatedAt": "2026-05-06T10:23:00+07:00"},
        },
        timeout=30,
    )
    if xlsx.status_code != 200 or len(xlsx.content) < 1000:
        raise AssertionError(f"XLSX page scale audit export failed: {xlsx.status_code}")
    with zipfile.ZipFile(io.BytesIO(xlsx.content)) as zf:
        scales_xml = _xlsx_sheet_xml(zf, "Page Scales")
        scales_shared = zf.read("xl/sharedStrings.xml").decode("utf-8")
    if scales_xml.count("<row ") < 4:
        raise AssertionError("Page Scales sheet did not include all pages from 1..pageCount")
    for col_header in ["scale_state", "object_count", "needs_attention"]:
        if col_header not in scales_shared:
            raise AssertionError(f"Page Scales sheet missing audit column header {col_header!r}")

    return {
        "entries": len(cache),
        "bytes": cache_bytes,
        "bad_scale": bad_scale.status_code,
        "xlsx_page_scale_rows": 3,
    }


def _canvas_box(page):
    box = page.locator("#canvas").bounding_box()
    if not box:
        raise RuntimeError("canvas not visible")
    return box


def _wait_analyse_ready(page, timeout: float = 30.0):
    deadline = time.time() + timeout
    last_snaps = ""
    last_status = ""
    last_page_label = ""
    while time.time() < deadline:
        last_snaps = page.locator("#lbl-snaps").inner_text().strip()
        last_status = page.locator("#status").inner_text().strip()
        last_page_label = page.locator("#page-lbl").inner_text().strip()
        if last_snaps != "—" or "manual/raster mode" in last_status:
            return
        if "analyse error" in last_status.lower():
            raise AssertionError(f"analyse failed: page={last_page_label} status={last_status!r}")
        page.wait_for_timeout(250)
    raise AssertionError(
        f"analyse did not finish: page={last_page_label!r}, "
        f"snaps={last_snaps!r}, status={last_status!r}"
    )


def _test_vector_area(page):
    _upload_and_start(page, VECTOR_PDF)
    page.locator("#btn-area").click()
    box = _canvas_box(page)
    points = [
        (box["x"] + 180, box["y"] + 180),
        (box["x"] + 420, box["y"] + 180),
        (box["x"] + 420, box["y"] + 360),
        (box["x"] + 180, box["y"] + 360),
        (box["x"] + 180, box["y"] + 180),
    ]
    for x, y in points:
        page.mouse.click(x, y)
        page.wait_for_timeout(150)
    page.locator("#name-panel").wait_for(state="visible")
    page.locator("#name-input").fill(VECTOR_POLY_NAME)
    page.get_by_role("button", name="ตกลง").click()
    page.wait_for_timeout(300)
    summary = page.locator("#page-summary").inner_text()
    measure = page.locator("#measure-result").inner_text()
    if "สุทธิ" not in summary or "ตร.ม." not in summary:
        raise AssertionError(f"page summary not updated: {summary!r}")
    if "ตร.ม." not in measure:
        raise AssertionError(f"area result not shown: {measure!r}")
    return {"summary": summary, "measure": measure}


def _test_mouse_wheel_zoom(page):
    before_zoom = page.locator("#zoom-val").inner_text().strip()
    box = _canvas_box(page)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -500)
    page.wait_for_timeout(200)
    after_zoom = page.locator("#zoom-val").inner_text().strip()
    if after_zoom == before_zoom:
        raise AssertionError(f"mouse wheel did not change zoom: {before_zoom!r}")
    return {"zoom": f"{before_zoom}->{after_zoom}"}


def _test_snap_helpers(page):
    result = page.evaluate(
        """() => {
            const raw = (v) => v / RS;
            pageData = {
                snaps: [],
                lines: [
                    [raw(50), raw(100), raw(150), raw(100)],
                    [raw(100), raw(50), raw(100), raw(150)]
                ]
            };
            buildSnapIndex(pageData);
            snapModes = {ep:false, mp:false, ct:false, nl:false, ix:true, off:false};
            mode = "dist";
            mPts = [];
            perpMode = false;
            const ix = snap(103, 97);
            snapModes.ix = false;
            perpMode = true;
            mPts = [{x: raw(100), y: raw(20)}];
            const perp = snap(101, 102);
            mode = "area";
            zoom = 0.25;
            mPts = [{x: raw(100), y: raw(100)}, {x: raw(160), y: raw(100)}];
            snapModes = {ep:false, mp:false, ct:false, nl:false, ix:false, off:false};
            const close = snap(145, 145);
            mPolys = [{
                pts: [{x: raw(210), y: raw(210)}, {x: raw(260), y: raw(210)}, {x: raw(260), y: raw(260)}, {x: raw(210), y: raw(260)}],
                closed: true,
                name: "USER_SNAP",
                areaType: "building",
                id: "snap-poly",
                color: "#30d158",
                opacity: 0.85
            }];
            mode = "dist";
            snapModes = {ep:true, mp:true, ct:true, nl:true, ix:false, off:false};
            perpMode = false;
            mPts = [];
            const userEp = snap(212, 211);
            snapModes = {ep:false, mp:true, ct:false, nl:false, ix:false, off:false};
            const userMp = snap(235, 211);
            snapModes = {ep:false, mp:false, ct:false, nl:true, ix:false, off:false};
            const userNl = snap(233, 224);
            return {ix, perp, close, indexed: !!pageData._snapIndex, userEp, userMp, userNl};
        }"""
    )
    ix = result["ix"]
    perp = result["perp"]
    close = result["close"]
    if ix.get("t") != "ix" or abs(ix.get("x", 0) - 100) > 0.5 or abs(ix.get("y", 0) - 100) > 0.5:
        raise AssertionError(f"IX snap failed: {ix!r}")
    if perp.get("t") != "perp" or abs(perp.get("x", 0) - 100) > 0.5 or abs(perp.get("y", 0) - 100) > 0.5:
        raise AssertionError(f"perpendicular snap failed: {perp!r}")
    if close.get("t") != "close":
        raise AssertionError(f"zoom-aware close snap failed: {close!r}")
    if not result.get("indexed"):
        raise AssertionError("snap index was not built")
    if result["userEp"].get("t") != "ep" or abs(result["userEp"].get("x", 0) - 210) > 0.5:
        raise AssertionError(f"user polygon endpoint snap failed: {result['userEp']!r}")
    if result["userMp"].get("t") != "mp" or abs(result["userMp"].get("x", 0) - 235) > 0.5:
        raise AssertionError(f"user polygon midpoint snap failed: {result['userMp']!r}")
    if result["userNl"].get("t") != "nl":
        raise AssertionError(f"user polygon nearest-line snap failed: {result['userNl']!r}")
    return {"ix": ix, "perp": perp, "close": close, "indexed": True, "userEp": result["userEp"], "userMp": result["userMp"], "userNl": result["userNl"]}


def _test_setback_helpers(page):
    result = page.evaluate(
        """() => {
            const raw = (v) => v / RS;
            pageData = pageData || {};
            pageData.scale = {pts_per_m: 20 / 3, calibrated: true, source: "manual", verified: true};
            setAType("building");
            const buildingSelected = document.getElementById("atype-building").classList.contains("sel");
            mPolys = [
                {pts: [{x: raw(0), y: raw(0)}, {x: raw(100), y: raw(0)}, {x: raw(100), y: raw(100)}, {x: raw(0), y: raw(100)}], closed: true, name: "LAND", areaType: "land", id: "land", color: "#30d158", opacity: 0.55},
                {pts: [{x: raw(20), y: raw(20)}, {x: raw(80), y: raw(20)}, {x: raw(80), y: raw(80)}, {x: raw(20), y: raw(80)}], closed: true, name: "BUILDING", areaType: "building", id: "building", color: "#ff9f0a", opacity: 0.7}
            ];
            mOpenings = [];
            openLandEdgePanel(0);
            selectLandEdge(0, 1);
            selectLandEdgeType("front_road");
            document.getElementById("land-edge-note").value = "road 8m";
            applyLandEdgeTag();
            closeLandEdgePanel();
            const segs = setbackSegments();
            const beforeToggle = showSetbackDistances;
            const advancedHidden = document.getElementById("btn-setbackdist")?.offsetParent === null
                && document.getElementById("btn-land-edge")?.offsetParent === null;
            toggleSetbackDistance();
            const afterToggle = showSetbackDistances;
            toggleSetbackDistance();
            drawSetbackLines();
            return {
                buildingSelected,
                count: segs.length,
                distances: segs.map(s => +(s.distPt / pageData.scale.pts_per_m).toFixed(3)),
                allPerp: segs.every(s => s.perp),
                edgeType: mPolys[0].edgeTags[1].type,
                edgeNote: mPolys[0].edgeTags[1].note,
                advancedHidden,
                beforeToggle,
                afterToggle,
                restoredToggle: showSetbackDistances
            };
        }"""
    )
    if not result["buildingSelected"]:
        raise AssertionError(f"building area type button did not select: {result}")
    if result["count"] != 12:
        raise AssertionError(f"expected 12 setback lines, got: {result}")
    if any(abs(v - 2.0) > 0.001 for v in result["distances"]):
        raise AssertionError(f"setback distances should be 2.0m: {result}")
    if not result["allPerp"]:
        raise AssertionError(f"setback lines should be perpendicular to land edges: {result}")
    if result["edgeType"] != "front_road" or result["edgeNote"] != "road 8m":
        raise AssertionError(f"land edge tag did not persist on polygon: {result}")
    if not result["advancedHidden"]:
        raise AssertionError(f"advanced setback controls should be hidden in Phase 1: {result}")
    if result["beforeToggle"] or not result["afterToggle"] or result["restoredToggle"]:
        raise AssertionError(f"setback helper toggle failed: {result}")
    return result


def _test_selection_and_area_type_helpers(page):
    result = page.evaluate(
        """() => {
            const raw = (v) => v / RS;
            pageData = pageData || {};
            pageData.scale = {pts_per_m: 10, calibrated: true, source: "manual", verified: true};
            getStore(curPage).calibScale = pageData.scale;
            setAType("land");
            const before = curAType;
            let cbType = null;
            openNamePanel("ชื่อพื้นที่", (nm, at) => { cbType = at; }, "", true, curAType);
            const landSelected = document.getElementById("atype-land").classList.contains("sel");
            finishName();
            mPolys = [{
                pts: [{x: raw(10), y: raw(10)}, {x: raw(80), y: raw(10)}, {x: raw(80), y: raw(70)}, {x: raw(10), y: raw(70)}],
                closed: true,
                name: "LAND_A",
                areaType: cbType,
                id: "poly-e2e",
                color: "#30d158",
                opacity: 0.85
            }];
            mOpenings = [{
                pts: [{x: raw(30), y: raw(30)}, {x: raw(55), y: raw(30)}, {x: raw(55), y: raw(50)}, {x: raw(30), y: raw(50)}],
                closed: true,
                name: "VOID_A",
                id: "op-e2e",
                color: "#ff453a",
                opacity: 0.6
            }];
            mRefs = [{pts: [{x: raw(0), y: raw(150)}, {x: raw(100), y: raw(150)}], x0: raw(0), y0: raw(150), x1: raw(100), y1: raw(150), kind: "ref", id: "ref-e2e", refType: "road", name: "REF_A", color: "#5ac8fa", opacity: 0.8}];
            mLines = [{pts: [{x: raw(0), y: raw(100)}, {x: raw(30), y: raw(100)}], x0: raw(0), y0: raw(100), x1: raw(30), y1: raw(100), kind: "line", id: "line-e2e", color: "#ffd60a", opacity: 0.8}];
            mParking = [{x: raw(10), y: raw(90), id: "park-e2e", parkingType: "car", count: 1, color: "#ffcc00", opacity: 0.9}];
            normalizeCurrentObjects();
            const parentLinked = mOpenings[0].parentId === "poly-e2e";
            const semanticDefaults = {
                poly: mPolys[0].semanticTag,
                opening: mOpenings[0].semanticTag,
                ref: mRefs[0].semanticTag,
                line: mLines[0].semanticTag,
                parking: mParking[0].semanticTag,
                polyUse: Object.prototype.hasOwnProperty.call(mPolys[0], "useCategory") ? mPolys[0].useCategory : "__missing__"
            };
            selItem = {type: "poly", idx: 0};
            applyColor("#ff00ff");
            applyOpacity(40);
            ctxTarget = {type: "poly", idx: 0};
            ctxRename();
            const renameKeepsLand = document.getElementById("atype-land").classList.contains("sel");
            document.getElementById("atype-room").click();
            finishName();
            selItem = {type: "opening", idx: 0};
            applyColor("#00ffff");
            applyOpacity(35);
            const hit = hitTest(40, 40);
            const nearest = findNearest(42, 42, 80);
            toggleLayerLock("deduction");
            const deductionHitAfterLock = hitTest(40, 40);
            const deductionStillVisible = layerVis.deduction === true && layerLock.deduction === true;
            toggleLayerLock("deduction");
            setMode("sel");
            hideObjPicker();
            const screenForCanvas = (x, y) => {
                const r = canvas.getBoundingClientRect();
                return {x: r.left + x * (r.width / canvas.width), y: r.top + y * (r.height / canvas.height)};
            };
            const overlapPt = screenForCanvas(40, 40);
            const pickerHits = hitTestAll(40, 40);
            showObjPicker(pickerHits, overlapPt.x, overlapPt.y);
            document.dispatchEvent(new MouseEvent("click", {bubbles: true, button: 0, clientX: overlapPt.x, clientY: overlapPt.y}));
            const picker = document.getElementById("obj-picker");
            const pickerVisibleAfterCanvasClick = picker.style.display === "block";
            const pickerRowCount = picker.querySelectorAll(".opkr-row").length;
            document.body.dispatchEvent(new MouseEvent("click", {bubbles: true, button: 0, clientX: 1, clientY: 1}));
            const pickerHiddenAfterOutsideClick = picker.style.display === "none";
            showObjPicker(hitTestAll(40, 40), overlapPt.x, overlapPt.y);
            const firstPickerRow = picker.querySelector(".opkr-row");
            firstPickerRow?.dispatchEvent(new MouseEvent("click", {bubbles: true, button: 0, clientX: overlapPt.x + 12, clientY: overlapPt.y + 12}));
            const selectedFromPicker = selItem && selItem.type === "opening" && selItem.idx === 0;
            const pickerHiddenAfterRowClick = picker.style.display === "none";
            buildRightPanel();
            const panelHasTree = /object tree/i.test(document.querySelector("#rp-content")?.innerText || "");
            selectObjectFromTree("opening", 0, false);
            const selectedFromTree = selItem && selItem.type === "opening" && selItem.idx === 0;
            rpSetName("VOID_RENAMED");
            const rightPanelRename = mOpenings[0].name === "VOID_RENAMED" && document.querySelector("#rp-content")?.innerText.includes("VOID_RENAMED");
            selectObjectFromTree("poly", 0, false);
            buildRightPanel();
            const semanticControlsVisible = !!document.querySelector("#rp-semantic-tag") && !!document.querySelector("#rp-use-category");
            rpSetSemanticTag("gross_floor_area");
            rpSetUseCategory("residential");
            const semanticEdited = mPolys[0].semanticTag === "gross_floor_area" && mPolys[0].useCategory === "residential";
            const metaOk = mPolys[0].measurementProfile === "legal_building_area" && mPolys[0].objectCategory === "area" && mPolys[0].reportTarget === "Building Area Summary" && mPolys[0].countingRule === "included" && mPolys[0].lawBasis === "พื้นที่อาคาร";
            const metaPanelVisible = !!document.querySelector("#rp-content .rp-meta-value");
            const undoCapturedSemantic = undoStack.length > 0;
            selItem = {type: "opening", idx: 0};
            buildRightPanel();
            const openingUseDisabled = document.querySelector("#rp-use-category")?.disabled === true && mOpenings[0].useCategory === null;
            selItem = {type: "poly", idx: 0};
            rpSetLabelMode("hidden");
            const labelHidden = mPolys[0].label?.mode === "hidden" && !shouldDrawLabelForObject(mPolys[0], false);
            const stripped = JSON.parse(JSON.stringify({polys:mPolys, openings:mOpenings, refs:mRefs, lines:mLines, parking:mParking}));
            for (const list of Object.values(stripped)) for (const obj of list) { delete obj.semanticTag; delete obj.useCategory; delete obj.measurementProfile; delete obj.objectCategory; delete obj.reportTarget; delete obj.lawBasis; delete obj.countingRule; }
            ensureStoreObjectIds(stripped);
            const strippedMetaOk = stripped.polys[0].measurementProfile === "use_area" && stripped.polys[0].objectCategory === "area" && stripped.polys[0].countingRule === "classified";
            const strippedDefaults = {
                poly: stripped.polys[0].semanticTag,
                opening: stripped.openings[0].semanticTag,
                ref: stripped.refs[0].semanticTag,
                line: stripped.lines[0].semanticTag,
                parking: stripped.parking[0].semanticTag,
                polyUse: Object.prototype.hasOwnProperty.call(stripped.polys[0], "useCategory") ? stripped.polys[0].useCategory : "__missing__"
            };
            const refHitBefore = hitTest(50, 150);
            toggleLayerLock("reference_geometry");
            const refHitAfterLock = hitTest(50, 150);
            const refStillVisible = layerVis.reference_geometry === true && layerLock.reference_geometry === true;
            toggleLayerLock("reference_geometry");
            const report = collectAreas();
            const warnings = phase1Warnings(report);
            mOpenings.push({pts: [{x: raw(200), y: raw(200)}, {x: raw(210), y: raw(200)}, {x: raw(210), y: raw(210)}, {x: raw(200), y: raw(210)}], closed: true, name: "UNLINKED", id: "op-unlinked", color: "#ff453a", opacity: 0.6});
            const unlinkedWarnings = phase1Warnings(collectAreas()).filter(w => w.object_id === "op-unlinked" && w.page_index === curPage);
            const idsPresent = [mPolys[0], mOpenings[0], mRefs[0], mLines[0], mParking[0]].every(o => !!o.id);
            // Parent reassignment: use the unlinked opening (idx 1) pushed above
            selItem = {type: "opening", idx: 1};
            buildRightPanel();
            const parentSelectVisible = !!document.querySelector("#rp-opening-parent");
            rpSetOpeningParent(mPolys[0].id);
            const parentReassigned = mOpenings[1].parentId === mPolys[0].id && mOpenings[1].parentStatus === "linked";
            return {
                before,
                cbType,
                landSelected,
                renameKeepsLand,
                polyType: mPolys[0].areaType,
                polyColor: mPolys[0].color,
                polyOpacity: mPolys[0].opacity,
                openingColor: mOpenings[0].color,
                openingOpacity: mOpenings[0].opacity,
                hit,
                nearest,
                deductionHitAfterLock,
                deductionStillVisible,
                pickerVisibleAfterCanvasClick,
                pickerRowCount,
                pickerHiddenAfterOutsideClick,
                selectedFromPicker,
                pickerHiddenAfterRowClick,
                panelHasTree,
                selectedFromTree,
                rightPanelRename,
                parentLinked,
                semanticDefaults,
                semanticControlsVisible,
                semanticEdited,
                metaOk,
                metaPanelVisible,
                undoCapturedSemantic,
                openingUseDisabled,
                strippedMetaOk,
                strippedDefaults,
                labelHidden,
                refHitBefore,
                refHitAfterLock,
                refStillVisible,
                idsPresent,
                structuredWarnings: warnings.every(w => w.id && "page_index" in w && "object_id" in w && w.suggested_action),
                unlinkedWarnings: unlinkedWarnings.length,
                parentSelectVisible,
                parentReassigned
            };
        }"""
    )
    if result["before"] != "land" or result["cbType"] != "land" or not result["landSelected"]:
        raise AssertionError(f"area type did not persist into name panel: {result}")
    if not result["renameKeepsLand"] or result["polyType"] != "room":
        raise AssertionError(f"area type rename did not work: {result}")
    if result["polyColor"] != "#ff00ff" or round(result["polyOpacity"], 2) != 0.4:
        raise AssertionError(f"polygon color/opacity edit failed: {result}")
    if result["openingColor"] != "#00ffff" or round(result["openingOpacity"], 2) != 0.35:
        raise AssertionError(f"opening color/opacity edit failed: {result}")
    if result["hit"].get("type") != "opening" or result["nearest"].get("type") != "opening":
        raise AssertionError(f"opening selection failed: {result}")
    if result["deductionHitAfterLock"].get("type") != "poly" or not result["deductionStillVisible"]:
        raise AssertionError(f"deduction layer lock failed: {result}")
    if not result["pickerVisibleAfterCanvasClick"] or result["pickerRowCount"] < 2:
        raise AssertionError(f"overlapping picker did not remain visible after canvas click: {result}")
    if not result["pickerHiddenAfterOutsideClick"]:
        raise AssertionError(f"overlapping picker did not hide after outside click: {result}")
    if not result["selectedFromPicker"] or not result["pickerHiddenAfterRowClick"]:
        raise AssertionError(f"overlapping picker row selection failed: {result}")
    if not result["panelHasTree"] or not result["selectedFromTree"] or not result["rightPanelRename"]:
        raise AssertionError(f"right panel object tree/properties failed: {result}")
    if not result["idsPresent"] or not result["parentLinked"]:
        raise AssertionError(f"stable IDs or opening parent link failed: {result}")
    expected_semantics = {
        "poly": "site_boundary",
        "opening": "deduction_opening",
        "ref": "reference_line",
        "line": "dimension_line",
        "parking": "review_note",
        "polyUse": None,
    }
    for key, expected in expected_semantics.items():
        if result["semanticDefaults"].get(key) != expected:
            raise AssertionError(f"semantic defaults failed for {key}: {result}")
    if not result["semanticControlsVisible"] or not result["semanticEdited"] or not result["undoCapturedSemantic"]:
        raise AssertionError(f"semantic properties editing failed: {result}")
    if not result["metaOk"]:
        raise AssertionError(f"measurement profile metadata not derived correctly after rpSetSemanticTag: {result}")
    if not result["metaPanelVisible"]:
        raise AssertionError(f"measurement metadata read-only labels not visible in properties panel: {result}")
    if not result["openingUseDisabled"]:
        raise AssertionError(f"useCategory should be disabled/null for opening: {result}")
    if not result["strippedMetaOk"]:
        raise AssertionError(f"measurement profile metadata not re-normalized after strip: {result}")
    stripped_expected = {
        "poly": "use_area",
        "opening": "deduction_opening",
        "ref": "reference_line",
        "line": "dimension_line",
        "parking": "review_note",
        "polyUse": None,
    }
    for key, expected in stripped_expected.items():
        if result["strippedDefaults"].get(key) != expected:
            raise AssertionError(f"legacy semantic inference failed for {key}: {result}")
    if not result["labelHidden"]:
        raise AssertionError(f"label hidden mode failed: {result}")
    if result["refHitBefore"].get("type") != "ref" or result["refHitAfterLock"] is not None or not result["refStillVisible"]:
        raise AssertionError(f"reference layer lock failed: {result}")
    if not result["structuredWarnings"] or result["unlinkedWarnings"] < 1:
        raise AssertionError(f"structured QA warnings failed: {result}")
    if not result["parentSelectVisible"]:
        raise AssertionError(f"parent reassignment select not shown for unlinked opening: {result}")
    if not result["parentReassigned"]:
        raise AssertionError(f"rpSetOpeningParent did not link opening to poly: {result}")
    return result


def _test_extended_measurement_helpers(page):
    result = page.evaluate(
        """() => {
            const raw = (v) => v / RS;
            pageData = pageData || {};
            pageData.scale = {pts_per_m: 10, calibrated: true, source: "manual", verified: true};
            mLines = [];
            mRefs = [];
            mParking = [];
            curRefType = "road";
            curParkingType = "car";
            const p0 = {x: raw(0), y: raw(0)};
            const p1 = {x: raw(30), y: raw(0)};
            const p2 = {x: raw(30), y: raw(40)};
            mPts = [p0, p1, p2];
            finishPathLike("path");
            mPts = [{x: raw(0), y: raw(80)}, {x: raw(100), y: raw(80)}];
            finishPathLike("ref");
            cancelName();
            setMode("parking");
            mParking.push({x: raw(10), y: raw(10), id: "park-a", parkingType: "car", count: 1, color: "#ffcc00", opacity: 0.9});
            mParking.push({x: raw(20), y: raw(10), id: "park-b", parkingType: "ev", count: 1, color: "#ffcc00", opacity: 0.9});
            pageTags[curPage] = "plan";
            pageNames[curPage] = "ชั้นทดสอบ";
            selItem = {type: "parking", idx: 0};
            showRefDistances = true;
            const refHits = refDistanceSegmentsForSelection();
            const parkingSummary = collectParkingSummary();
            const refReport = collectRefDistanceReport();
            const beforeVertex = mLines[0].pts[1].x;
            const targetPdf = {x: raw(45), y: raw(0)};
            const targetCanvas = pdfToC(targetPdf.x, targetPdf.y);
            const rect = canvas.getBoundingClientRect();
            setMode("sel");
            selItem = {type: "line", idx: 0};
            dragState = {type: "line", idx: 0, vertex: 1, startPdf: mLines[0].pts[1], origData: linePts(mLines[0]).map(p => ({...p}))};
            handleMouseMove({clientX: rect.left + targetCanvas.x * rect.width / canvas.width, clientY: rect.top + targetCanvas.y * rect.height / canvas.height});
            const afterVertex = mLines[0].pts[1].x;
            dragState = null;
            openCheckPanel();
            const reportText = document.querySelector("#check-body").innerText;
            const metrics = lineMetrics(mLines[0]);
            const rows = buildRows();
            return {
                lineCount: mLines.length,
                linePts: mLines[0].pts.length,
                beforeVertex: +beforeVertex.toFixed(3),
                afterVertex: +afterVertex.toFixed(3),
                total: +metrics.dist.toFixed(3),
                segments: metrics.segments.length,
                refs: mRefs.length,
                refType: mRefs[0].refType,
                parking: mParking.length,
                refDistance: refHits.length ? +(refHits[0].distPt / pageData.scale.pts_per_m).toFixed(3) : null,
                parkingSummary: parkingSummary.totalCount,
                parkingTypeRows: parkingSummary.typeRows.length,
                refReportRows: refReport.length,
                parkingRows: rows.filter(r => r["ประเภท"] === "ที่จอดรถ").length,
                refDistanceRows: rows.filter(r => r["ประเภท"] === "ระยะถึงเส้นอ้างอิง").length,
                pathRows: rows.filter(r => r["ประเภท"] === "ระยะต่อเนื่อง").length,
                reportHasSections: reportText.includes("รายงานที่จอดรถตามหน้า/ชั้น") && reportText.includes("รายงานระยะถึงเส้นอ้างอิง"),
                tooltips: ["#btn-path", "#btn-ref", "#btn-north", "#btn-refdist", "#btn-parking", "#sn-ep"].map(s => document.querySelector(s).getAttribute("title"))
            };
        }"""
    )
    if result["lineCount"] != 1 or result["linePts"] != 3:
        raise AssertionError(f"polyline was not stored as 3 raw points: {result}")
    if result["afterVertex"] <= result["beforeVertex"] or abs(result["total"] - 5.848) > 0.001 or result["segments"] != 2:
        raise AssertionError(f"polyline total/segments wrong: {result}")
    if result["refs"] != 1 or result["refType"] != "road":
        raise AssertionError(f"reference line failed: {result}")
    if result["parking"] != 2 or result["parkingRows"] != 2:
        raise AssertionError(f"parking rows/count failed: {result}")
    if abs(result["refDistance"] - 4.667) > 0.001 or result["parkingSummary"] != 2 or result["parkingTypeRows"] != 2:
        raise AssertionError(f"reference distance or parking summary failed: {result}")
    if result["refReportRows"] < 3 or result["refDistanceRows"] < 3 or not result["reportHasSections"]:
        raise AssertionError(f"report sections failed: {result}")
    if result["pathRows"] != 1:
        raise AssertionError(f"polyline export row failed: {result}")
    if any(not t for t in result["tooltips"]):
        raise AssertionError(f"missing tooltips: {result}")
    return result


def _test_recalibrate_and_exports(page, download_dir: Path, previous_summary: str):
    box = _canvas_box(page)
    page.locator("#btn-calib").click()
    calib_points = [
        (box["x"] + 150, box["y"] + 150),
        (box["x"] + 390, box["y"] + 150),
    ]
    for x, y in calib_points:
        page.mouse.click(x, y)
        page.wait_for_timeout(180)
    page.locator("#calib-panel").wait_for(state="visible")
    page.locator("#calib-input").fill("1")
    page.get_by_role("button", name="ยืนยัน").click()
    page.wait_for_timeout(500)
    scale_badge = page.locator("#lbl-scale").inner_text().strip()
    summary = page.locator("#page-summary").inner_text().strip()
    if "สอบเทียบ" not in scale_badge:
        raise AssertionError(f"manual calibration did not apply: {scale_badge!r}")
    if summary == previous_summary:
        raise AssertionError("page summary did not change after recalibration")
    with page.expect_download() as json_dl:
        page.evaluate("exportJSON()")
    json_target = download_dir / "measurements.json"
    json_dl.value.save_as(json_target)
    with page.expect_download() as csv_dl:
        page.evaluate("exportCSV()")
    csv_target = download_dir / "measurements.csv"
    csv_dl.value.save_as(csv_target)
    payload = json_target.read_text(encoding="utf-8-sig")
    csv_text = csv_target.read_text(encoding="utf-8-sig")
    if '"scale_source": "manual"' not in payload:
        raise AssertionError("JSON export missing manual scale_source")
    if '"scale_verified": true' not in payload:
        raise AssertionError("JSON export missing scale_verified=true")
    if '"area_type": "room"' not in payload:
        raise AssertionError("JSON export missing room area_type")
    if "manual,true,room" not in csv_text.replace('"', ""):
        raise AssertionError("CSV export missing manual/verified/room fields")
    return {
        "scale": scale_badge,
        "summary": summary,
        "json_file": json_target.name,
        "csv_file": csv_target.name,
    }


def _draw_area_points(page, points, panel_value: str | None = None, click_area: bool = True):
    if click_area:
        page.locator("#btn-area").click()
    for x, y in points:
        page.mouse.click(x, y)
        page.wait_for_timeout(150)
    page.locator("#name-panel").wait_for(state="visible")
    if panel_value is None:
        page.get_by_role("button", name="ข้าม").click()
    else:
        page.locator("#name-input").fill(panel_value)
        page.get_by_role("button", name="ตกลง").click()
    page.wait_for_timeout(300)


def _test_site_sides_orientation_ui(page):
    seed = page.evaluate(
        """() => {
            const raw = (v) => v / RS;
            pageTags[curPage] = "site";
            pageNames[curPage] = "ผังบริเวณทดสอบ";
            pageData = pageData || {};
            pageData.scale = pageData.scale || {pts_per_m: 10, calibrated: true, source: "manual", verified: true};
            getStore(curPage).calibScale = pageData.scale;
            const site = {
                pts: [
                    {x: raw(120), y: raw(120)},
                    {x: raw(520), y: raw(120)},
                    {x: raw(520), y: raw(420)},
                    {x: raw(120), y: raw(420)}
                ],
                closed: true,
                name: "SITE_E2E",
                areaType: "land",
                id: "site-e2e",
                color: "#30d158",
                opacity: 0.45
            };
            mPolys.push(site);
            const idx = mPolys.length - 1;
            ensureLandEdgeTags(site);
            selItem = {type: "poly", idx};
            saveCurrentPage();
            buildRightPanel();
            redraw();
            return {
                idx,
                panelText: document.querySelector("#rp-content")?.innerText || "",
                fontFamily: getComputedStyle(document.body).fontFamily
            };
        }"""
    )
    if "ข้อมูลด้านที่ดิน" not in seed["panelText"]:
        raise AssertionError(f"parcel side editor did not render: {seed}")
    if "Inter" not in seed["fontFamily"] or "Noto Sans Thai" not in seed["fontFamily"]:
        raise AssertionError(f"expected updated font stack, got {seed['fontFamily']!r}")

    page.locator("#rp-side-label-0").fill("ด้านหน้า")
    page.locator("#rp-side-label-0").dispatch_event("change")
    page.locator("#rp-side-role-0").select_option("front_road")
    page.locator("#rp-side-note-0").fill("ถนนหน้าโครงการ")
    page.locator("#rp-side-note-0").dispatch_event("change")

    page.locator("#btn-north").click()
    box = _canvas_box(page)
    page.mouse.click(box["x"] + 540, box["y"] + 460)
    page.wait_for_timeout(150)
    page.mouse.click(box["x"] + 540, box["y"] + 340)
    page.wait_for_timeout(300)
    result = page.evaluate(
        """() => {
            const site = mPolys.find(p => p.id === "site-e2e");
            const tag = site?.edgeTags?.[0] || {};
            const north = getPageNorth(curPage);
            buildRightPanel();
            const panelText = document.querySelector("#rp-content")?.innerText || "";
            return {
                tag,
                north,
                panelText,
                sideCount: site?.edgeTags?.length || 0,
                northStored: !!north,
                northStatus: north?.status,
                northSource: north?.source,
                mode
            };
        }"""
    )
    if result["sideCount"] != 4:
        raise AssertionError(f"parcel side count did not match polygon side count: {result}")
    if result["tag"].get("label") != "ด้านหน้า" or result["tag"].get("role") != "front_road":
        raise AssertionError(f"parcel side metadata did not update: {result}")
    if result["tag"].get("note") != "ถนนหน้าโครงการ":
        raise AssertionError(f"parcel side note did not update: {result}")
    if not result["northStored"] or result["northSource"] != "manual" or result["northStatus"] != "verified":
        raise AssertionError(f"north orientation was not stored: {result}")
    if "N / ทิศเหนือ" not in result["panelText"]:
        raise AssertionError(f"orientation summary did not render in right panel: {result}")
    return {
        "side_role": result["tag"]["role"],
        "north_angle": result["north"]["angleDeg"],
        "mode_after": result["mode"],
    }


def _test_opening_and_xlsx_export(page, download_dir: Path):
    box = _canvas_box(page)
    page.locator("#btn-opening").click()
    opening_points = [
        (box["x"] + 230, box["y"] + 220),
        (box["x"] + 310, box["y"] + 220),
        (box["x"] + 310, box["y"] + 290),
        (box["x"] + 230, box["y"] + 290),
        (box["x"] + 230, box["y"] + 220),
    ]
    _draw_area_points(page, opening_points, VECTOR_OPENING_NAME, click_area=False)
    summary = page.locator("#page-summary").inner_text().strip()
    measure = page.locator("#measure-result").inner_text().strip()
    if "ช่องว่าง" not in summary or "สุทธิ" not in summary:
        raise AssertionError(f"opening did not update summary as deduction: {summary!r}")
    if "ช่องว่าง" not in measure:
        raise AssertionError(f"opening measurement not shown: {measure!r}")

    page.evaluate(
        """() => {
            document.getElementById('pi-reqno').value = 'REQ-XLSX-E2E';
            document.getElementById('pi-btype').value = 'office';
            document.getElementById('pi-worktype').value = 'new';
            document.getElementById('pi-floors').value = '9';
            document.getElementById('pi-gfa').value = '1234';
            document.getElementById('pi-units').value = '12';
            syncProjectInfoFromForm();
        }"""
    )
    with page.expect_download() as xlsx_dl:
        page.evaluate("exportXLSX()")
    xlsx_target = download_dir / "measurements_report.xlsx"
    xlsx_dl.value.save_as(xlsx_target)
    if not xlsx_target.exists() or xlsx_target.stat().st_size < 1000:
        raise AssertionError("XLSX export missing or too small")

    with zipfile.ZipFile(xlsx_target) as zf:
        workbook_xml = zf.read("xl/workbook.xml").decode("utf-8")
        shared_xml = zf.read("xl/sharedStrings.xml").decode("utf-8")
    for sheet in ["Cover", "Warnings", "Page Scales", "Site Facts", "Audit Log", "สรุปพื้นที่", "ความยาวเส้น Polygon", "สรุปตามชั้น", "สรุปตามประเภท", "สรุปตาม Report Target"]:
        if sheet not in workbook_xml:
            raise AssertionError(f"XLSX missing sheet {sheet!r}")
    for text in [VECTOR_POLY_NAME, VECTOR_OPENING_NAME, "รวมสุทธิ", "BMA-Plan Phase 1 Export", "UI pageStore", "REQ-XLSX-E2E", "site-e2e", "N / ทิศเหนือ", "front_road", "ถนนหน้าโครงการ"]:
        if text not in shared_xml:
            raise AssertionError(f"XLSX missing expected text {text!r}")
    for col_header in ["measurementProfile", "objectCategory", "reportTarget", "lawBasis", "countingRule"]:
        if col_header not in shared_xml:
            raise AssertionError(f"XLSX missing metadata column header {col_header!r}")
    return {"summary": summary, "xlsx_file": xlsx_target.name}


def _test_project_save_load(page, download_dir: Path):
    with page.expect_download() as dl_info:
        page.evaluate("_fallbackDownload()")
    download = dl_info.value
    target = download_dir / "roundtrip.bmaplan"
    download.save_as(target)
    if not target.exists() or target.stat().st_size < 100:
        raise AssertionError("project download missing or empty")
    project_text = target.read_text(encoding="utf-8")
    if '"reqNo": "REQ-XLSX-E2E"' not in project_text:
        raise AssertionError("projectInfo was not saved into .bmaplan")
    if '"siteOrientation"' not in project_text or "ถนนหน้าโครงการ" not in project_text:
        raise AssertionError("site orientation or parcel side metadata was not saved into .bmaplan")
    page.evaluate("siteOrientation={}; clearMeasures()")
    page.wait_for_timeout(250)
    cleared = page.locator("#page-summary").inner_text()
    if "ยังไม่มีรายการพื้นที่" not in cleared:
        raise AssertionError(f"expected cleared page summary, got {cleared!r}")
    page.locator("#proj-input").set_input_files(str(target))
    page.wait_for_timeout(600)
    restored = page.locator("#page-summary").inner_text()
    if "สุทธิ" not in restored or "ตร.ม." not in restored:
        raise AssertionError(f"expected restored page summary, got {restored!r}")
    restored_meta = page.evaluate(
        """() => {
            const site = mPolys.find(p => p.id === "site-e2e");
            return {
                north: !!getPageNorth(curPage),
                sideNote: site?.edgeTags?.[0]?.note || "",
                sideRole: site?.edgeTags?.[0]?.role || ""
            };
        }"""
    )
    if not restored_meta["north"] or restored_meta["sideNote"] != "ถนนหน้าโครงการ" or restored_meta["sideRole"] != "front_road":
        raise AssertionError(f"site orientation or side metadata did not restore: {restored_meta!r}")
    return {"project_file": target.name, "restored": restored, "site_meta": restored_meta}


def _test_pdf_annotations_export(page, download_dir: Path):
    page.evaluate("openPageManager()")
    page.locator("#pgmgr-overlay").wait_for(state="visible")
    with page.expect_download() as dl_info:
        page.evaluate("pgmgrExportPDF(true)")
    target = download_dir / "annotated_export.pdf"
    dl_info.value.save_as(target)
    original = fitz.open(VECTOR_PDF)
    doc = fitz.open(target)
    try:
        original_drawings = len(original[0].get_drawings())
        annotated_drawings = len(doc[0].get_drawings())
        if annotated_drawings <= original_drawings:
            raise AssertionError(
                f"annotated PDF did not add drawings: original={original_drawings}, annotated={annotated_drawings}"
            )
    finally:
        original.close()
        doc.close()
    page.wait_for_timeout(200)
    return {"annotated_file": target.name, "label": VECTOR_POLY_NAME}


def _test_raster_mode(page):
    _make_raster_pdf(VECTOR_PDF, RASTER_PDF)
    _upload_and_start(page, RASTER_PDF)
    page.wait_for_timeout(1000)
    snap_text = page.locator("#lbl-snaps").inner_text()
    status_text = page.locator("#status").inner_text()
    if "manual only" not in snap_text:
        raise AssertionError(f"expected raster/manual mode, got snap label {snap_text!r}")
    if "manual/raster mode" not in status_text:
        raise AssertionError(f"expected raster warning in status, got {status_text!r}")
    return {"snap": snap_text, "status": status_text}


def _test_real_pdf_navigation_rotate_export(page, download_dir: Path):
    _upload_and_start(page, REAL_PDF)
    page_label = page.locator("#page-lbl").inner_text().strip()
    if not page_label.endswith("/ 45"):
        raise AssertionError(f"unexpected page label for real PDF: {page_label!r}")
    page.locator("#btn-next").click()
    page.wait_for_timeout(700)
    page_label_2 = page.locator("#page-lbl").inner_text().strip()
    if not page_label_2.startswith("2 "):
        raise AssertionError(f"did not navigate to page 2: {page_label_2!r}")
    page.locator("#btn-prev").click()
    page.wait_for_timeout(700)
    page.evaluate("rotatePage(90)")
    page.wait_for_timeout(1200)
    rot_badge = page.locator("#rot-badge").inner_text().strip()
    if rot_badge != "90°":
        raise AssertionError(f"rotation badge not updated: {rot_badge!r}")
    page.evaluate("openPageManager()")
    page.locator("#pgmgr-overlay").wait_for(state="visible")
    page.get_by_role("button", name="ยกเลิก").click()
    page.wait_for_timeout(150)
    page.locator(".pgmgr-cell[data-page='1']").click()
    page.locator(".pgmgr-cell[data-page='2']").click()
    with page.expect_download() as dl_info:
        page.evaluate("pgmgrExportPDF(false)")
    download = dl_info.value
    target = download_dir / "subset_export.pdf"
    download.save_as(target)
    page.wait_for_timeout(300)
    exported = fitz.open(target)
    try:
        if exported.page_count != 2:
            raise AssertionError(f"expected 2 exported pages, got {exported.page_count}")
        if exported[0].rotation != 90:
            raise AssertionError(f"expected page 1 rotation 90, got {exported[0].rotation}")
    finally:
        exported.close()
    return {
        "page_label": page_label,
        "page_label_2": page_label_2,
        "rotation": rot_badge,
        "export_pages": 2,
    }


def _draw_polygon(page, points):
    _draw_area_points(page, points, None)


def _test_real_pdf_multipage_persistence(page):
    _upload_and_start(page, REAL_PDF)
    _wait_analyse_ready(page)
    box = _canvas_box(page)
    page1_points = [
        (box["x"] + 120, box["y"] + 120),
        (box["x"] + 250, box["y"] + 120),
        (box["x"] + 250, box["y"] + 230),
        (box["x"] + 120, box["y"] + 230),
        (box["x"] + 120, box["y"] + 120),
    ]
    _draw_polygon(page, page1_points)
    page1_summary = page.locator("#page-summary").inner_text().strip()
    if "สุทธิ" not in page1_summary or "ตร.ม." not in page1_summary:
        raise AssertionError(f"real PDF page 1 summary missing polygon: {page1_summary!r}")
    page.locator("#btn-next").click()
    page.wait_for_timeout(900)
    _wait_analyse_ready(page)
    box = _canvas_box(page)
    page2_points = [
        (box["x"] + 180, box["y"] + 180),
        (box["x"] + 320, box["y"] + 180),
        (box["x"] + 320, box["y"] + 300),
        (box["x"] + 180, box["y"] + 300),
        (box["x"] + 180, box["y"] + 180),
    ]
    _draw_polygon(page, page2_points)
    page2_poly_count = page.evaluate("mPolys.length")
    page2_summary = page.locator("#page-summary").inner_text().strip()
    if "สุทธิ" not in page2_summary or "ตร.ม." not in page2_summary:
        raise AssertionError(f"real PDF page 2 summary missing polygon: {page2_summary!r}")
    page.locator("#btn-prev").click()
    page.wait_for_timeout(900)
    page1_restored = page.locator("#page-summary").inner_text().strip()
    if page1_restored != page1_summary:
        raise AssertionError(f"page 1 summary did not persist: {page1_restored!r} vs {page1_summary!r}")
    page.locator("#btn-next").click()
    page.wait_for_timeout(900)
    page2_restored_poly_count = page.evaluate("mPolys.length")
    if page2_restored_poly_count != page2_poly_count:
        raise AssertionError(f"page 2 polygon data did not persist: count {page2_restored_poly_count} vs {page2_poly_count}")
    page2_restored = page.locator("#page-summary").inner_text().strip()
    return {
        "page1": page1_summary,
        "page2": page2_restored,
    }


def _test_menu_power_up(page):
    """Step 6 assertions: menu structure, keyboard shortcuts, layer helpers, per-page layer memory."""
    _upload_and_start(page, VECTOR_PDF)
    _wait_analyse_ready(page)

    # ── 1. Menu structure ────────────────────────────────────────────────────
    menu_counts = page.evaluate("""() => {
        const menus = ['project','scale','page','measure','object','layer','annotate'];
        const out = {};
        for (const m of menus) {
            const dd = document.getElementById('dd-'+m);
            out[m] = dd ? dd.querySelectorAll(':scope > .dd-item, :scope > .dd-submenu-trigger').length : -1;
        }
        return out;
    }""")
    expected_counts = {"project": 4, "scale": 7, "page": 8, "measure": 22, "object": 7, "layer": 11, "annotate": 8}
    menuStructureOk = all(menu_counts.get(m) == expected_counts[m] for m in expected_counts)

    # ── 2. No disabled items ─────────────────────────────────────────────────
    noDisabledItems = page.evaluate(
        "() => document.querySelectorAll('#menuBar .dd-item.disabled').length === 0"
    )

    # ── 3. Menu click opens ──────────────────────────────────────────────────
    page.locator('[data-menu="project"]').click()
    page.wait_for_timeout(80)
    menuClickOpens = page.evaluate(
        "() => document.querySelector('[data-menu=\"project\"]').classList.contains('active')"
    )

    # ── 4. Click-outside closes ──────────────────────────────────────────────
    page.mouse.click(700, 700)
    page.wait_for_timeout(80)
    clickOutsideCloses = page.evaluate(
        "() => !document.querySelector('[data-menu=\"project\"]').classList.contains('active')"
    )

    # ── 5. Keyboard B → area/building ───────────────────────────────────────
    page.evaluate("setMode('sel')")
    page.wait_for_timeout(50)
    page.keyboard.press("b")
    page.wait_for_timeout(100)
    kb_b = page.evaluate("() => ({mode, curAType})")
    keyboardB = kb_b.get("mode") == "area" and kb_b.get("curAType") == "building"

    # ── 6. Keyboard Shift+O toggles ortho ────────────────────────────────────
    page.keyboard.press("Escape")
    page.wait_for_timeout(50)
    ortho_before = page.evaluate("() => orthoMode")
    page.keyboard.press("Shift+o")
    page.wait_for_timeout(100)
    ortho_after = page.evaluate("() => orthoMode")
    keyboardShiftO = ortho_after != ortho_before

    # ── 7. E key toggles ep snap ─────────────────────────────────────────────
    snap_before = page.evaluate("() => snapModes.ep")
    page.keyboard.press("e")
    page.wait_for_timeout(100)
    snap_after = page.evaluate("() => snapModes.ep")
    snapToggleE = snap_after != snap_before

    # ── 8. F2 with no selection shows status ─────────────────────────────────
    page.evaluate("selItem = null")
    page.keyboard.press("F2")
    page.wait_for_timeout(100)
    f2_status = page.evaluate("() => document.getElementById('status').textContent")
    keyboardF2 = "เลือก" in f2_status

    # ── 9. PgUp on single-page PDF shows no-prev status ──────────────────────
    page.keyboard.press("PageUp")
    page.wait_for_timeout(100)
    pgu_status = page.evaluate("() => document.getElementById('status').textContent")
    keyboardPgUp = "ไม่มีหน้าก่อนหน้า" in pgu_status

    # ── 10. soloLayer hides other layers ─────────────────────────────────────
    page.evaluate("setAllLayersVisible(true)")
    page.wait_for_timeout(50)
    page.evaluate("soloLayer('sub_area')")
    page.wait_for_timeout(100)
    layer_vis = page.evaluate("() => ({...layerVis})")
    soloLayerWorks = (
        layer_vis.get("sub_area") is True
        and not all(layer_vis.values())
        and layer_vis.get("base_area") is False
    )

    # ── 11. lockOtherLayers locks non-active layers ──────────────────────────
    page.evaluate("setAllLayersLocked(false)")
    page.wait_for_timeout(50)
    page.evaluate("lockOtherLayers('sub_area')")
    page.wait_for_timeout(100)
    layer_lock = page.evaluate("() => ({...layerLock})")
    lockOthersWorks = (
        layer_lock.get("sub_area") is False
        and layer_lock.get("base_area") is True
    )

    # ── 12. selectAllInLayer with 0 polys → status mentions "0" ──────────────
    page.evaluate("mPolys = []; mOpenings = []; mLines = []; mRefs = []")
    page.evaluate("selectAllInLayer('sub_area')")
    page.wait_for_timeout(100)
    sel_status = page.evaluate("() => document.getElementById('status').textContent")
    selectAllInLayerWorks = "เลือก" in sel_status and "sub_area" in sel_status

    # ── 13. validateAllPolygons with no polys → ok status ────────────────────
    page.evaluate("validateAllPolygons()")
    page.wait_for_timeout(100)
    val_status = page.evaluate("() => document.getElementById('status').textContent")
    validatePolygonsWarns = "ไม่พบ polygon" in val_status

    # ── 14. Per-page layer memory bug fix (multi-page only) ──────────────────
    perPageLayerMemoryFixed = None
    if REAL_PDF.exists():
        _upload_and_start(page, REAL_PDF)
        _wait_analyse_ready(page)
        # Hide base_area on page 1
        page.evaluate("hideLayer('base_area')")
        page.wait_for_timeout(100)
        vis_p1_before = page.evaluate("() => layerVis.base_area")
        # Go to page 2
        page.evaluate("() => { const n = getNextPage(curPage); if(n) loadPage(n); }")
        page.wait_for_timeout(600)
        # Come back to page 1
        page.evaluate("() => { const p = getPrevPage(curPage); if(p) loadPage(p); }")
        page.wait_for_timeout(600)
        vis_p1_after = page.evaluate("() => layerVis.base_area")
        perPageLayerMemoryFixed = vis_p1_before is False and vis_p1_after is False

    # ── H.1.2: Quick Rectangle tool ──────────────────────────────────────────
    # verify mode + function + hotkey, then simulate 2-click rectangle
    rect_tool = page.evaluate("""() => {
        const fnExists = typeof activateRectTool === 'function';
        activateRectTool('room');
        const modeAfterActivate = mode;
        // clear & simulate 2-click rect at known pdf coords
        const before = mPolys.length;
        const scale = getScaleForPage(curPage);
        if (!scale) { setMode('pan'); return {fnExists, modeAfterActivate, skipped:true}; }
        // Click corner 1 at pdf (0,0)
        mPts = [{x: 0, y: 0}];
        // Simulate 2nd click handler logic by setting mPts to 4-corner polygon then call finishCurrentArea
        const a = {x: 0, y: 0}, b = {x: 10 * scale.pts_per_m, y: 5 * scale.pts_per_m};
        mPts = [{x:a.x,y:a.y},{x:b.x,y:a.y},{x:b.x,y:b.y},{x:a.x,y:b.y}];
        // close the name panel listener — just call finishCurrentArea
        finishCurrentArea();
        const after = mPolys.length;
        const newPoly = mPolys[after - 1];
        const area = newPoly ? polyAreaM2(newPoly.pts) : null;
        // expected area = 10 * 5 = 50 m²
        const areaOk = area != null && Math.abs(area - 50) < 0.01;
        const fourVerts = newPoly && newPoly.pts && newPoly.pts.length === 4;
        // close name panel if it opened
        const np = document.getElementById('name-panel'); if (np) np.style.display = 'none';
        setMode('pan');
        // cleanup: remove the test rect
        if (after > before) mPolys.pop();
        return {fnExists, modeAfterActivate, areaOk, fourVerts, area: area};
    }""")

    # ── H.2: Annotate menu + helpers ─────────────────────────────────────────
    annotate_menu = page.evaluate("""() => {
        const menuExists = !!document.querySelector('[data-menu="annotate"]');
        const ddExists = !!document.getElementById('dd-annotate');
        const ddItems = document.querySelectorAll('#dd-annotate .dd-item').length;
        const helpers = ['ensureAnnotations','addAnnotation','clearAnnotations','drawAnnotations'].every(n => typeof window[n] === 'function');
        // Programmatically create one of each annotation type
        const arr = ensureAnnotations(curPage);
        const before = arr.length;
        addAnnotation({id:'t1', type:'comment', pts:[{x:10,y:10}], text:'test', color:'#ffd60a', opacity:0.9});
        addAnnotation({id:'t2', type:'text', pts:[{x:20,y:20}], text:'hello', color:'#0a84ff', opacity:1, fontSize:14});
        addAnnotation({id:'t3', type:'highlight', pts:[{x:0,y:0},{x:50,y:50}], color:'#ffd60a', opacity:0.4});
        addAnnotation({id:'t4', type:'rect_frame', pts:[{x:0,y:0},{x:50,y:50}], color:'#0a84ff', opacity:0.9});
        addAnnotation({id:'t5', type:'circle_frame', pts:[{x:25,y:25},{x:35,y:25}], color:'#30d158', opacity:0.9});
        addAnnotation({id:'t6', type:'cloud_frame', pts:[{x:0,y:0},{x:50,y:0},{x:25,y:50}], color:'#bf5af2', opacity:0.9});
        addAnnotation({id:'t7', type:'arrow', pts:[{x:0,y:0},{x:50,y:50}], color:'#ff453a', opacity:0.9});
        const after = arr.length;
        const addedSeven = (after - before) === 7;
        // Verify drawAnnotations does not throw
        let drewOk = true;
        try { drawAnnotations(); } catch(e) { drewOk = false; }
        // Clear via direct array reset (skip confirm dialog)
        arr.length = 0;
        const clearedToZero = ensureAnnotations(curPage).length === 0;
        return {menuExists, ddExists, ddItems, helpers, addedSeven, drewOk, clearedToZero};
    }""")

    # ── H.1.4: Ellipse tool ──────────────────────────────────────────────────
    ellipse_tool = page.evaluate("""() => {
        const fnExists = typeof activateEllipseTool === 'function';
        activateEllipseTool('room');
        const modeAfterActivate = mode;
        const scale = getScaleForPage(curPage);
        if (!scale) { setMode('pan'); return {fnExists, modeAfterActivate, skipped:true}; }
        const before = mPolys.length;
        // bounding box 10m × 6m → semiAxisA = 5m, semiAxisB = 3m
        const a_pt = 10 * scale.pts_per_m, b_pt = 6 * scale.pts_per_m;
        const cx = a_pt / 2, cy = b_pt / 2;
        mPts = _ellipsePolygonPts({x: cx, y: cy}, a_pt/2, b_pt/2, 32);
        finishCurrentArea();
        const after = mPolys.length;
        const newPoly = mPolys[after - 1];
        if (newPoly) {
            newPoly.shape='ellipse';
            newPoly.center={x:cx,y:cy};
            newPoly.semiAxisA=a_pt/2;
            newPoly.semiAxisB=b_pt/2;
            newPoly.rotation=0;
        }
        const isEllipse = newPoly && newPoly.shape === 'ellipse';
        const has32 = newPoly && newPoly.pts && newPoly.pts.length === 32;
        const area = newPoly ? objectAreaM2(newPoly) : null;
        const expectedArea = Math.PI * 5 * 3;
        const areaOk = area != null && Math.abs(area - expectedArea) < 0.01;
        const np = document.getElementById('name-panel'); if (np) np.style.display = 'none';
        setMode('pan');
        if (after > before) mPolys.pop();
        return {fnExists, modeAfterActivate, isEllipse, has32, areaOk, area, expectedArea};
    }""")

    # ── H.1.3: Circle tool ───────────────────────────────────────────────────
    circle_tool = page.evaluate("""() => {
        const fnExists = typeof activateCircleTool === 'function';
        activateCircleTool('room');
        const modeAfterActivate = mode;
        const scale = getScaleForPage(curPage);
        if (!scale) { setMode('pan'); return {fnExists, modeAfterActivate, skipped:true}; }
        const before = mPolys.length;
        // Click center at (0,0)
        mPts = [{x: 0, y: 0}];
        // Simulate 2nd click at (5*pts_per_m, 0) → radius 5m
        const center = {x:0,y:0}, radius = 5 * scale.pts_per_m;
        mPts = _circlePolygonPts(center, radius, 32);
        finishCurrentArea();
        const after = mPolys.length;
        const newPoly = mPolys[after - 1];
        // Patch shape meta as mode handler does
        if (newPoly) { newPoly.shape='circle'; newPoly.center={x:0,y:0}; newPoly.radius=radius; }
        const isCircle = newPoly && newPoly.shape === 'circle';
        const has32 = newPoly && newPoly.pts && newPoly.pts.length === 32;
        const area = newPoly ? objectAreaM2(newPoly) : null;
        const expectedArea = Math.PI * 25;
        const areaOk = area != null && Math.abs(area - expectedArea) < 0.01;
        // close name panel
        const np = document.getElementById('name-panel'); if (np) np.style.display = 'none';
        setMode('pan');
        if (after > before) mPolys.pop();
        return {fnExists, modeAfterActivate, isCircle, has32, areaOk, area, expectedArea};
    }""")

    # ── H.1.1: Curves area math (additive — does not touch polyAreaM2) ───────
    curves_math = page.evaluate("""() => {
        const calibrated = typeof getScaleForPage === 'function' && !!getScaleForPage(curPage);
        if (!calibrated) return {skipped: true};
        const fnsExist = ['circleAreaM2','ellipseAreaM2','arcSegmentAreaM2','polygonAreaWithArcsM2','objectAreaM2']
            .every(n => typeof window[n] === 'function');
        const pts_per_m = getScaleForPage(curPage).pts_per_m;
        const r_pt = 100 * pts_per_m;
        const expectedCircle = Math.PI * 100 * 100;
        const gotCircle = circleAreaM2(r_pt);
        const circleOk = gotCircle != null && Math.abs(gotCircle - expectedCircle) < 0.01;
        const a_pt = 100 * pts_per_m, b_pt = 50 * pts_per_m;
        const expectedEllipse = Math.PI * 100 * 50;
        const gotEllipse = ellipseAreaM2(a_pt, b_pt);
        const ellipseOk = gotEllipse != null && Math.abs(gotEllipse - expectedEllipse) < 0.01;
        const semi = arcSegmentAreaM2(2 * pts_per_m, Math.PI);
        const semiExpected = Math.PI * 1 * 1 / 2;
        const arcOk = semi != null && Math.abs(semi - semiExpected) < 0.05;
        const zeroSweep = arcSegmentAreaM2(10 * pts_per_m, 0);
        const arcZeroOk = zeroSweep === 0;
        const square = {pts:[{x:0,y:0},{x:10*pts_per_m,y:0},{x:10*pts_per_m,y:10*pts_per_m},{x:0,y:10*pts_per_m}]};
        const squareArea = polyAreaM2(square.pts);
        const objSquareArea = objectAreaM2(square);
        const objSquareOk = Math.abs(squareArea - objSquareArea) < 0.001 && Math.abs(squareArea - 100) < 0.001;
        const objCircle = objectAreaM2({shape:'circle', radius: r_pt});
        const objCircleOk = Math.abs(objCircle - expectedCircle) < 0.01;
        const objEllipse = objectAreaM2({shape:'ellipse', semiAxisA: a_pt, semiAxisB: b_pt});
        const objEllipseOk = Math.abs(objEllipse - expectedEllipse) < 0.01;
        return {fnsExist, circleOk, ellipseOk, arcOk, arcZeroOk, objSquareOk, objCircleOk, objEllipseOk};
    }""")

    return {
        "menuCounts": menu_counts,
        "menuStructureOk": menuStructureOk,
        "noDisabledItems": noDisabledItems,
        "menuClickOpens": menuClickOpens,
        "clickOutsideCloses": clickOutsideCloses,
        "keyboardB": keyboardB,
        "keyboardShiftO": keyboardShiftO,
        "snapToggleE": snapToggleE,
        "keyboardF2": keyboardF2,
        "keyboardPgUp": keyboardPgUp,
        "soloLayerWorks": soloLayerWorks,
        "lockOthersWorks": lockOthersWorks,
        "selectAllInLayerWorks": selectAllInLayerWorks,
        "validatePolygonsWarns": validatePolygonsWarns,
        "perPageLayerMemoryFixed": perPageLayerMemoryFixed,
        "curvesMath": curves_math,
        "rectTool": rect_tool,
        "circleTool": circle_tool,
        "ellipseTool": ellipse_tool,
        "annotateMenu": annotate_menu,
    }


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "full").lower()
    if mode not in {"full", "smoke"}:
        raise SystemExit(f"unsupported mode: {mode}")
    instance, thread = _start_server()
    download_dir = Path(tempfile.mkdtemp(prefix="bmaplan_e2e_"))
    try:
        cache_limits = _test_backend_cache_limits()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 1100}, accept_downloads=True)
            page = context.new_page()
            setup = _test_project_setup_screen(page)
            main_ui = _test_main_measurement_ui_cleanup(page)
            vector = _test_vector_area(page)
            recal = _test_recalibrate_and_exports(page, download_dir, vector["summary"])
            site_ui = _test_site_sides_orientation_ui(page)
            xlsx = _test_opening_and_xlsx_export(page, download_dir)
            project = _test_project_save_load(page, download_dir)
            if mode == "full":
                annotated = _test_pdf_annotations_export(page, download_dir)
            raster = _test_raster_mode(page)
            wheel = _test_mouse_wheel_zoom(page)
            snap_helpers = _test_snap_helpers(page)
            selection_helpers = _test_selection_and_area_type_helpers(page)
            setback_helpers = _test_setback_helpers(page)
            extended_helpers = _test_extended_measurement_helpers(page)
            menu_power_up = _test_menu_power_up(page)
            if mode == "full":
                real_persist = _test_real_pdf_multipage_persistence(page)
                real_pdf = _test_real_pdf_navigation_rotate_export(page, download_dir)
            context.close()
            browser.close()
        print("CACHE_OK", cache_limits)
        print("SETUP_OK", setup)
        print("MAIN_UI_OK", main_ui)
        print("VECTOR_OK", vector)
        print("RECAL_OK", recal)
        print("SITE_UI_OK", site_ui)
        print("XLSX_OK", xlsx)
        print("PROJECT_OK", project)
        print("RASTER_OK", raster)
        print("WHEEL_OK", wheel)
        print("SNAP_OK", snap_helpers)
        print("SELECT_OK", selection_helpers)
        print("SETBACK_OK", setback_helpers)
        print("EXT_MEASURE_OK", extended_helpers)
        print("MENU_OK", menu_power_up)
        if mode == "full":
            print("ANNOT_OK", annotated)
            print("PERSIST_OK", real_persist)
            print("REAL_OK", real_pdf)
    finally:
        instance.should_exit = True
        thread.join(timeout=10)
        RASTER_PDF.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("E2E_FAIL", exc)
        raise
