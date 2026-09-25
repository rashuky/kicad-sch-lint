"""Render schematic pages to PNG, optionally with findings drawn on top.

KiCad draws the sheet (kicad-cli PDF export), pymupdf rasterises it. The
PNG is sized so an AI image reader keeps 1.27 mm text legible: use a
region, a symbol to zoom on, or tiles for full sheets.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile

from . import kicad
from .geom import Box
from .model import Project
from .pdftruth import _pymupdf, page_index

PT = 72.0 / 25.4
COLORS = {"error": (0.9, 0.0, 0.6), "warning": (1.0, 0.5, 0.0), "info": (0.1, 0.45, 1.0)}
DEBUG_COLORS = {
    "body": (0.8, 0.0, 0.0),
    "field": (0.0, 0.5, 0.0),
    "pin_name": (0.3, 0.3, 0.9),
    "pin_number": (0.5, 0.2, 0.8),
    "label": (0.9, 0.4, 0.0),
    "text": (0.0, 0.5, 0.6),
    "sheet": (0.5, 0.3, 0.0),
    "sheet_pin": (0.5, 0.3, 0.0),
    "sheet_field": (0.5, 0.3, 0.0),
}


def _cache_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "kschlint")
    os.makedirs(d, exist_ok=True)
    return d


def project_pdf(project: Project, fresh: bool = False) -> str:
    """PDF of the whole project, cached by file contents."""
    h = hashlib.sha1()
    for p in sorted(project.files):
        h.update(p.encode())
        h.update(project.files[p].text.encode("utf-8"))
    out = os.path.join(_cache_dir(), f"{h.hexdigest()[:16]}.pdf")
    if fresh or not os.path.exists(out):
        kicad.export_pdf(project.root_path, out)
    return out


def content_box(scene) -> Box | None:
    boxes = [i.box for i in scene.items]
    for s in scene.wires:
        boxes.append(Box.of_points([s.a, s.b]))
    if not boxes:
        return None
    b = boxes[0]
    for o in boxes[1:]:
        b = b.union(o)
    return b


def tiles_for(box: Box, tile_w: float = 110.0, tile_h: float = 80.0, overlap: float = 8.0) -> list[Box]:
    nx = max(1, math.ceil((box.w - overlap) / (tile_w - overlap)))
    ny = max(1, math.ceil((box.h - overlap) / (tile_h - overlap)))
    tw = (box.w + overlap * (nx - 1)) / nx
    th = (box.h + overlap * (ny - 1)) / ny
    out = []
    for j in range(ny):
        for i in range(nx):
            x0 = box.x0 + i * (tw - overlap)
            y0 = box.y0 + j * (th - overlap)
            out.append(Box(x0, y0, x0 + tw, y0 + th))
    return out


def render_page(
    project: Project,
    page,
    out_png: str,
    region: Box | None = None,
    findings: list | None = None,
    debug_boxes: bool = False,
    px_per_mm: float = 14.0,
    max_px: int = 1600,
    tiles: bool = False,
    grid: bool = False,
) -> list[dict]:
    """Render one page. Returns [{"png", "region"}] (several when tiling)."""
    from .scene import build

    pymupdf = _pymupdf()
    pdf = project_pdf(project)
    doc = pymupdf.open(pdf)
    pi = page_index(doc, page.name_path)
    if pi is None:
        raise RuntimeError(f"page {page.name_path} not found in PDF")
    pg = doc[pi]
    sc = build(page)
    w, h = page.sch.paper

    if debug_boxes:
        for it in sc.items:
            c = DEBUG_COLORS.get(it.kind, (0.4, 0.4, 0.4))
            pg.draw_rect(_r(it.box), color=c, width=0.25)
    if grid:
        step = 2.54
        x = 0.0
        while x <= w:
            pg.draw_line((x * PT, 0), (x * PT, h * PT), color=(0.85, 0.85, 0.85), width=0.1 if round(x / step) % 5 else 0.3)
            x += step
        y = 0.0
        while y <= h:
            pg.draw_line((0, y * PT), (w * PT, y * PT), color=(0.85, 0.85, 0.85), width=0.1 if round(y / step) % 5 else 0.3)
            y += step
    for n, f in enumerate(findings or [], 1):
        c = COLORS.get(f["severity"], (1, 0, 0))
        if f.get("box"):
            b = Box(*f["box"]).grow(0.4)
        else:
            x, y = f["at"]
            b = Box(x - 1.2, y - 1.2, x + 1.2, y + 1.2)
        pg.draw_rect(_r(b), color=c, width=0.6)
        tag = str(f.get("n", n))
        pg.insert_text(((b.x1 + 0.3) * PT, (b.y0 + 0.2) * PT), tag, fontsize=5.5, color=c)

    if region is None:
        cb = content_box(sc)
        region = cb.grow(5) if cb is not None else Box(0, 0, w, h)
    region = Box(max(0, region.x0), max(0, region.y0), min(w, region.x1), min(h, region.y1))
    boxes = tiles_for(region) if tiles else [region]
    outs = []
    base, ext = os.path.splitext(out_png)
    for k, r in enumerate(boxes):
        scale = min(px_per_mm, max_px / max(r.w, r.h))
        dpi = scale * 25.4
        pix = pg.get_pixmap(dpi=int(dpi), clip=_r(r))
        path = out_png if len(boxes) == 1 else f"{base}_{k + 1:02d}{ext}"
        pix.save(path)
        outs.append({"png": path, "region": r.as_list(1), "px_per_mm": round(scale, 1)})
    doc.close()
    return outs


def _r(b: Box):
    pymupdf = _pymupdf()
    return pymupdf.Rect(b.x0 * PT, b.y0 * PT, b.x1 * PT, b.y1 * PT)
