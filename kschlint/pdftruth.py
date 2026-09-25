"""Compare our text geometry with what KiCad really plots (PDF text layer).

Used by ``kschlint selftest`` and the test suite. Needs pymupdf.
"""

from __future__ import annotations

import math
import os
import tempfile

from . import kicad
from .geom import Box
from .model import Project
from .scene import build

MM = 25.4 / 72.0


def _pymupdf():
    try:
        import pymupdf  # type: ignore
    except ImportError:  # pragma: no cover
        import fitz as pymupdf  # type: ignore
    return pymupdf


def pdf_lines(pdf_page) -> list[tuple[str, Box, tuple]]:
    """Text lines of one PDF page in mm: (text, box, direction)."""
    out = []
    for b in pdf_page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            if not txt:
                continue
            x0, y0, x1, y1 = ln["bbox"]
            out.append((txt, Box(x0 * MM, y0 * MM, x1 * MM, y1 * MM), tuple(ln["dir"])))
    return out


def page_index(doc, name_path: str) -> int | None:
    """Find the PDF page whose title block says ``Sheet: <name_path>``."""
    want = f"Sheet: {name_path}"
    for i in range(doc.page_count):
        for txt, _b, _d in pdf_lines(doc[i]):
            if txt == want:
                return i
    return None


def compare(project: Project, pdf_path: str | None = None, kinds=("field", "text", "pin_name", "pin_number", "label")) -> dict:
    pymupdf = _pymupdf()
    tmp = None
    if pdf_path is None:
        tmp = tempfile.TemporaryDirectory()
        pdf_path = kicad.export_pdf(project.root_path, os.path.join(tmp.name, "sch.pdf"))
    doc = pymupdf.open(pdf_path)
    results = []
    missing = []
    for page in project.pages:
        pi = page_index(doc, page.name_path)
        if pi is None:
            continue
        spans = pdf_lines(doc[pi])
        by_text: dict[str, list] = {}
        for t, b, d in spans:
            by_text.setdefault(t, []).append(b)
        sc = build(page)
        for it in sc.items:
            if it.kind not in kinds:
                continue
            if it.kind == "label" and it.obj.kind != "label":
                continue
            cands = by_text.get(it.text.strip())
            if not cands:
                if "\n" not in it.text:
                    missing.append((page.name_path, it.describe(page)))
                continue
            best = min(cands, key=lambda b: math.hypot(b.cx - it.box.cx, b.cy - it.box.cy))
            err = max(abs(best.x0 - it.box.x0), abs(best.y0 - it.box.y0), abs(best.x1 - it.box.x1), abs(best.y1 - it.box.y1))
            results.append((err, page.name_path, it.describe(page), it.box.as_list(2), best.as_list(2)))
    if tmp is not None:
        doc.close()
        tmp.cleanup()
    results.sort(key=lambda r: -r[0])
    return {"checked": len(results), "results": results, "missing": missing}
