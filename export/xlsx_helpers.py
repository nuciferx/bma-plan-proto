"""Pure geometry and calculation helpers for BMA-Plan XLSX export."""
import math


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    if len(h) != 6: return (1, 1, 0)
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))


def _poly_area_pt2(pts: list) -> float:
    if len(pts) < 3:
        return 0.0
    area = 0.0
    for i, p1 in enumerate(pts):
        p2 = pts[(i + 1) % len(pts)]
        area += p1["x"] * p2["y"] - p2["x"] * p1["y"]
    return abs(area) / 2.0


def _line_points(obj: dict) -> list:
    pts = obj.get("pts")
    if isinstance(pts, list) and len(pts) >= 2:
        return pts
    if all(k in obj for k in ("x0", "y0", "x1", "y1")):
        return [{"x": obj["x0"], "y": obj["y0"]}, {"x": obj["x1"], "y": obj["y1"]}]
    return []


def _line_length_pt(pts: list) -> float:
    total = 0.0
    for p1, p2 in zip(pts, pts[1:]):
        total += math.hypot(p2["x"] - p1["x"], p2["y"] - p1["y"])
    return total


def _nearest_on_segment(px, py, ax, ay, bx, by):
    dx = bx - ax
    dy = by - ay
    denom = dx * dx + dy * dy
    if denom <= 1e-9:
        return ax, ay, math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    x = ax + t * dx
    y = ay + t * dy
    return x, y, math.hypot(px - x, py - y)


def _object_points_for_ref_report(kind: str, obj: dict) -> list:
    if kind == "parking":
        return [(obj.get("x", 0), obj.get("y", 0), "marker")]
    if kind in ("line", "ref"):
        return [(p["x"], p["y"], f"จุด {i+1}") for i, p in enumerate(_line_points(obj))]
    pts = obj.get("pts") or []
    out = [(p["x"], p["y"], f"มุม {i+1}") for i, p in enumerate(pts)]
    if pts:
        out.append((sum(p["x"] for p in pts) / len(pts), sum(p["y"] for p in pts) / len(pts), "กึ่งกลาง"))
    return out


def _distance_to_ref(pt, ref: dict):
    best = None
    for p1, p2 in zip(_line_points(ref), _line_points(ref)[1:]):
        x, y, d = _nearest_on_segment(pt[0], pt[1], p1["x"], p1["y"], p2["x"], p2["y"])
        if best is None or d < best["dist_pt"]:
            best = {"x": x, "y": y, "dist_pt": d, "point_role": pt[2]}
    return best


def _m2_to_rwu(m2):
    if m2 <= 0:
        return (0, 0, 0)
    rai = int(m2 // 1600)
    rem = m2 % 1600
    ngan = int(rem // 400)
    sqwa = round((rem % 400) / 4, 2)
    return (rai, ngan, sqwa)
