"""PCB side: DRC summary, PNG render with findings, reference designator fixer.

KiCad is the geometry engine here: kicad-cli DRC finds the problems,
pcbnew (KiCad's python) computes text boxes in ``pcb_silk.py``.
"""

from __future__ import annotations

import collections
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile

from . import kicad, sexpr
from .geom import Box

SILK_TYPES = ("silk_overlap", "silk_over_copper", "silk_edge_clearance")
PT = 72.0 / 25.4


def kicad_python() -> str:
    env = os.environ.get("KICAD_PYTHON")
    if env:
        return env
    base = os.path.dirname(kicad.kicad_cli())
    for cand in ("python.exe", "python3", "python"):
        p = os.path.join(base, cand)
        if os.path.exists(p):
            return p
    return shutil.which("python3") or "python3"  # Linux: system python with pcbnew


def drc(board: str) -> dict:
    """kicad-cli DRC as JSON. Runs in a copy of the project so KiCad cannot rewrite the .kicad_pro."""
    src = os.path.dirname(os.path.abspath(board))
    with tempfile.TemporaryDirectory() as td:
        for f in os.listdir(src):
            p = os.path.join(src, f)
            if os.path.isfile(p) and not f.endswith(".lck"):
                shutil.copy2(p, td)
        for d in ("lib",):
            if os.path.isdir(os.path.join(src, d)):
                shutil.copytree(os.path.join(src, d), os.path.join(td, d))
        out = os.path.join(td, "drc.json")
        kicad.run(["pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity", "-o", out, os.path.join(td, os.path.basename(board))])
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)


def _ref_of(desc: str) -> str | None:
    m = re.match(r"Reference field of (\S+)", desc)
    return m.group(1) if m else None


def lint(board: str, silk_only: bool = False) -> dict:
    d = drc(board)
    out = []
    for n, v in enumerate(d["violations"], 1):
        if silk_only and v["type"] not in SILK_TYPES:
            continue
        pos = v["items"][0].get("pos", {}) if v["items"] else {}
        out.append(
            {
                "n": n,
                "type": v["type"],
                "severity": v["severity"],
                "message": v["description"],
                "items": [i["description"] for i in v["items"]],
                "at": [pos.get("x", 0), pos.get("y", 0)],
                "refs": sorted({r for r in (_ref_of(i["description"]) for i in v["items"]) if r}),
            }
        )
    counts = dict(collections.Counter(v["type"] for v in d["violations"]))
    return {"board": board, "counts": counts, "unconnected": len(d.get("unconnected_items", [])), "parity": len(d.get("schematic_parity", [])), "findings": out}


def flagged_refs(d: dict) -> list[str]:
    refs = set()
    for v in d["violations"]:
        if v["type"] in SILK_TYPES:
            for i in v["items"]:
                r = _ref_of(i["description"])
                if r:
                    refs.add(r)
    return sorted(refs)


def _non_silk(d: dict) -> collections.Counter:
    return collections.Counter((v["type"], tuple(sorted(i["description"] for i in v["items"]))) for v in d["violations"] if v["type"] not in SILK_TYPES)


def fix(board: str, write: bool = False, min_size: float = 0.0, refs: list[str] | None = None) -> dict:
    before = drc(board)
    want = refs or flagged_refs(before) or ["-"]
    with open(board, encoding="utf-8", newline="") as fh:
        original = fh.read()
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pcb_silk.py")
    args = [kicad_python(), script, board, "--refs", ",".join(want), "--offboard"]
    if min_size:
        args += ["--min-size", str(min_size)]
    if write:
        args.append("--write")
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pcb_silk failed: {r.stderr[-2000:]}")
    res = json.loads(r.stdout.strip().splitlines()[-1])
    if not res.get("written"):
        return res
    after = drc(board)
    problems = []
    if _non_silk(after) != _non_silk(before):
        problems.append("non-silkscreen DRC results changed")
    if len(after.get("unconnected_items", [])) != len(before.get("unconnected_items", [])):
        problems.append("unconnected count changed")
    if len(after.get("schematic_parity", [])) != len(before.get("schematic_parity", [])):
        problems.append("schematic parity changed")
    silk_b = sum(1 for v in before["violations"] if v["type"] in SILK_TYPES)
    silk_a = sum(1 for v in after["violations"] if v["type"] in SILK_TYPES)
    if silk_a > silk_b:
        problems.append("more silkscreen findings than before")
    if problems:
        with open(board, "w", encoding="utf-8", newline="") as fh:
            fh.write(original)
        res["written"] = False
        res["error"] = "; ".join(problems) + ". Board restored"
        return res
    res["silk_findings"] = [silk_b, silk_a]
    return res


# --------------------------------------------------------------------------
def footprint_boxes(board: str) -> dict:
    """ref -> rough box (footprint position +- 3 mm), enough to aim a render."""
    with open(board, encoding="utf-8") as fh:
        root = sexpr.parse(fh.read())
    out = {}
    for fp in root.children("footprint"):
        at = fp.child("at")
        ref = next((p.arg(1) for p in fp.children("property") if p.arg(0) == "Reference"), None)
        if at is None or ref is None:
            continue
        x, y = float(at.arg(0)), float(at.arg(1))
        out[ref] = Box(x - 3, y - 3, x + 3, y + 3)
    return out


def render(board: str, out_png: str | None = None, region: list | None = None, around: str | None = None, margin: float = 6.0, findings: bool = True, layers: str = "F.Cu,F.Silkscreen,F.Courtyard,Edge.Cuts", px_per_mm: float = 20.0, max_px: int = 1600) -> dict:
    from .pdftruth import _pymupdf

    pymupdf = _pymupdf()
    with tempfile.TemporaryDirectory() as td:
        pdf = os.path.join(td, "b.pdf")
        r = kicad.run(["pcb", "export", "pdf", "--mode-single", "--layers", layers, "-o", pdf, board])
        if not os.path.exists(pdf):
            raise RuntimeError(r.stderr or r.stdout)
        doc = pymupdf.open(pdf)
        pg = doc[0]
        fl = lint(board, silk_only=True)["findings"] if findings else []
        if around:
            fb = footprint_boxes(board)
            bs = [fb[x.strip()] for x in around.split(",") if x.strip() in fb]
            if not bs:
                raise ValueError(f"{around} not found")
            b = bs[0]
            for o in bs[1:]:
                b = b.union(o)
            reg = b.grow(margin)
        elif region:
            reg = Box(*region)
        else:
            d = drc(board) if False else None
            reg = None
        drawn = 0
        for f in fl:
            x, y = f["at"]
            if reg is not None and not reg.grow(2).contains_pt(x, y):
                continue
            drawn += 1
            rect = pymupdf.Rect((x - 0.8) * PT, (y - 0.8) * PT, (x + 0.8) * PT, (y + 0.8) * PT)
            pg.draw_rect(rect, color=(1, 0, 1), width=0.4)
            pg.insert_text(((x + 0.9) * PT, (y - 0.5) * PT), str(f["n"]), fontsize=3.5, color=(1, 0, 1))
        if reg is None:
            rect = pg.rect
            reg = Box(rect.x0 / PT, rect.y0 / PT, rect.x1 / PT, rect.y1 / PT)
        scale = min(px_per_mm, max_px / max(reg.w, reg.h))
        pix = pg.get_pixmap(dpi=int(scale * 25.4), clip=pymupdf.Rect(reg.x0 * PT, reg.y0 * PT, reg.x1 * PT, reg.y1 * PT))
        if out_png is None:
            d = os.path.join(tempfile.gettempdir(), "kschlint")
            os.makedirs(d, exist_ok=True)
            out_png = os.path.join(d, "pcb.png")
        pix.save(out_png)
        doc.close()
    return {"png": out_png, "region": reg.as_list(1), "findings_drawn": drawn}
