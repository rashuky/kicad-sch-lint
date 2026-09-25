"""Functions shared by the CLI and the MCP server. All return JSON-able dicts."""

from __future__ import annotations

import math
import os
import shutil
import tempfile

from . import kicad
from .checks import CHECKS, lint_project
from .fix import apply_plan, plan_project
from .geom import Box, snap
from .model import Project, load_project
from .render import render_page
from .scene import build, field_text


# --------------------------------------------------------------------------
def resolve_pages(project: Project, sheet: str | None) -> list:
    """Pages matching a sheet spec: name path (/Power/), sheet or instance name, or file name."""
    if not sheet:
        return list(project.pages)
    s = sheet.strip()
    out = []
    for p in project.pages:
        base = os.path.basename(p.sch.path)
        names = {p.name_path, p.name_path.strip("/"), base, os.path.splitext(base)[0]}
        last = p.name_path.strip("/").split("/")[-1] if p.name_path != "/" else "root"
        names.add(last)
        if s in names or os.path.normcase(os.path.abspath(s)) == os.path.normcase(os.path.abspath(p.sch.path)):
            out.append(p)
    if not out:
        avail = ", ".join(p.name_path for p in project.pages)
        raise ValueError(f"no sheet matches {sheet!r}. Pages: {avail}")
    return out


def _files_of(pages) -> list[str]:
    seen = []
    for p in pages:
        if p.sch.path not in seen:
            seen.append(p.sch.path)
    return seen


# --------------------------------------------------------------------------
def lint(project_path: str, sheet: str | None = None, severity: str = "warning", codes: list[str] | None = None, clearance: float = 0.25) -> dict:
    prj = load_project(project_path)
    pages = resolve_pages(prj, sheet)
    enabled = set(codes) if codes else None
    fs = lint_project(prj, _files_of(pages), clearance, enabled, severity)
    out = []
    for n, f in enumerate(fs, 1):
        d = f.to_dict()
        d["n"] = n
        d["file"] = os.path.basename(d["file"])
        out.append(d)
    counts: dict = {}
    for f in out:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {"project": prj.root_path, "counts": counts, "findings": out}


def format_findings(res: dict, limit: int = 0) -> str:
    lines = []
    fs = res["findings"]
    for f in fs[: limit or len(fs)]:
        pages = f["pages"]
        where = pages[0] + (f" (+{len(pages) - 1} instances)" if len(pages) > 1 else "")
        lines.append(f"#{f['n']} {f['severity'][0].upper()} {f['code']} {f['file']} {where} @({f['at'][0]:g}, {f['at'][1]:g}): {f['message']}")
    if limit and len(fs) > limit:
        lines.append(f"... {len(fs) - limit} more")
    c = res["counts"]
    lines.append(f"{c.get('error', 0)} errors, {c.get('warning', 0)} warnings, {c.get('info', 0)} info")
    return "\n".join(lines)


# --------------------------------------------------------------------------
def render(
    project_path: str,
    sheet: str,
    out: str | None = None,
    region: list | None = None,
    around: str | None = None,
    margin: float = 15.0,
    findings: bool = True,
    severity: str = "warning",
    boxes: bool = False,
    tiles: bool = False,
    grid: bool = False,
) -> dict:
    prj = load_project(project_path)
    pages = resolve_pages(prj, sheet)
    page = pages[0]
    reg = Box(*region) if region else None
    if around:
        sc = build(page)
        refs = {r.strip() for r in around.split(",")}
        bs = [i.box for i in sc.items if i.kind in ("body", "field") and hasattr(i.owner, "refs") and page.ref(i.owner) in refs]
        if not bs:
            raise ValueError(f"{around} not on {page.name_path}")
        b = bs[0]
        for o in bs[1:]:
            b = b.union(o)
        reg = b.grow(margin)
    fl = []
    if findings:
        res = lint(project_path, page.name_path, severity)
        fl = [f for f in res["findings"] if page.name_path in f["pages"]]
    if out is None:
        d = os.path.join(tempfile.gettempdir(), "kschlint")
        os.makedirs(d, exist_ok=True)
        safe = page.name_path.strip("/").replace("/", "_") or "root"
        out = os.path.join(d, f"{safe}.png")
    imgs = render_page(prj, page, out, reg, fl, boxes, tiles=tiles, grid=grid)
    return {"page": page.name_path, "images": imgs, "findings_drawn": len(fl)}


# --------------------------------------------------------------------------
def fix(project_path: str, sheet: str | None = None, write: bool = False, labels: bool = True, horizontal: bool = False, verify: bool = True, clearance: float = 0.25) -> dict:
    prj = load_project(project_path)
    pages = resolve_pages(prj, sheet)
    before = lint_project(prj, _files_of(pages), clearance, None, "warning")
    plans = plan_project(prj, _files_of(pages), clearance=clearance, labels=labels, horizontal=horizontal)
    moves = []
    for pl in plans:
        for m in pl.moves:
            moves.append(
                {
                    "file": os.path.basename(pl.file),
                    "what": m.desc,
                    "to": [m.x, m.y, m.angle],
                    "justify": m.hjust,
                    "box_before": m.before.as_list(2),
                    "box_after": m.after.as_list(2),
                }
            )
    res = {"moves": moves, "unresolved": [u for pl in plans for u in pl.unresolved], "written": False}
    if not write or not moves:
        res["note"] = "dry run. Pass write=True to apply" if not write else "nothing to move"
        return res
    originals = {pl.file: prj.files[pl.file].text for pl in plans if pl.moves}
    sig0 = kicad.netlist_signature(prj.root_path) if verify else None
    for pl in plans:
        if not pl.moves:
            continue
        new = apply_plan(prj.files[pl.file], pl)
        with open(pl.file, "w", encoding="utf-8", newline="") as fh:
            fh.write(new)
    if verify:
        sig1 = kicad.netlist_signature(prj.root_path)
        diff = kicad.diff_signatures(sig0, sig1)
        if diff:
            for path, txt in originals.items():
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(txt)
            res["error"] = "netlist changed, all files restored"
            res["netlist_diff"] = diff[:50]
            return res
        res["netlist"] = "unchanged"
    res["written"] = True
    prj2 = load_project(project_path)
    after = lint_project(prj2, _files_of(resolve_pages(prj2, sheet)), clearance, None, "warning")
    res["findings_before"] = len(before)
    res["findings_after"] = len(after)
    return res


# --------------------------------------------------------------------------
def inspect(project_path: str, sheet: str, refs: list[str] | None = None) -> dict:
    """Exact geometry: symbols with body box, pin tips and field boxes. For placing wires."""
    prj = load_project(project_path)
    page = resolve_pages(prj, sheet)[0]
    sc = build(page)
    want = set(refs) if refs else None
    syms = []
    for s in page.sch.symbols:
        ref = page.ref(s)
        if want is not None and ref not in want:
            continue
        body = next((i.box.as_list(2) for i in sc.items if i.kind == "body" and i.owner is s), None)
        pins = []
        for p in sc.pins:
            if p.symbol is not s:
                continue
            dx, dy = p.x - p.ex, p.y - p.ey
            side = ("right" if dx > 0 else "left") if abs(dx) > abs(dy) else ("down" if dy > 0 else "up")
            pins.append({"number": p.number, "name": p.name, "at": [p.x, p.y], "points": side, "type": p.lib.etype})
        fields = [
            {"name": i.obj.name, "text": i.text, "box": i.box.as_list(2), "at": [i.obj.x, i.obj.y, i.obj.angle]}
            for i in sc.items
            if i.kind == "field" and i.owner is s
        ]
        syms.append({"ref": ref, "lib_id": s.lib_id, "at": [s.x, s.y, s.rot], "mirror": s.mirror, "unit": s.unit, "body": body, "pins": pins, "fields": fields})
    out = {"page": page.name_path, "file": os.path.basename(page.sch.path), "paper": page.sch.paper, "symbols": syms}
    if want is None:
        out["labels"] = [{"kind": l.kind, "text": l.text, "at": [l.x, l.y, l.angle]} for l in page.sch.labels]
        out["wires"] = [[list(a), list(b)] for s in sc.wires for a, b in [(s.a, s.b)]]
    return out


def free_space(project_path: str, sheet: str, w: float, h: float, near: list | None = None, margin: float = 2.54, count: int = 3) -> dict:
    """Free rectangles of w x h mm on the page (grid aligned), nearest to ``near`` first."""
    from .checks import BORDER, TITLE_H, TITLE_W
    from .geom import seg_box_overlap

    prj = load_project(project_path)
    page = resolve_pages(prj, sheet)[0]
    sc = build(page)
    pw, ph = page.sch.paper
    obst = [i.box.grow(margin) for i in sc.items]
    segs = [(s.a, s.b) for s in sc.wires] + [(s.a, s.b) for s in sc.pin_segs]
    title = Box(pw - BORDER - TITLE_W, ph - BORDER - TITLE_H, pw - BORDER, ph - BORDER).grow(margin)
    nx, ny = near if near else (pw / 2, ph / 2)
    found = []
    step = 2.54
    y = BORDER + margin
    cands = []
    while y + h <= ph - BORDER - margin:
        x = BORDER + margin
        while x + w <= pw - BORDER - margin:
            cands.append((math.hypot(x + w / 2 - nx, y + h / 2 - ny), x, y))
            x += step
        y += step
    cands.sort()
    for _d, x, y in cands:
        b = Box(snap(x, 1.27), snap(y, 1.27), snap(x, 1.27) + w, snap(y, 1.27) + h)
        if b.overlaps(title) or any(b.overlaps(o) for o in obst):
            continue
        if any(seg_box_overlap(a, c, b.grow(margin)) > 0 for a, c in segs):
            continue
        if any(b.grow(-0.01).overlaps(Box(*f)) for f in found):
            continue
        found.append(b.as_list(2))
        if len(found) >= count:
            break
    return {"page": page.name_path, "size": [w, h], "free": found}


def selftest(project_path: str) -> dict:
    """Check our text geometry against the kicad-cli PDF of this project."""
    from .pdftruth import compare
    from .render import project_pdf

    prj = load_project(project_path)
    r = compare(prj, project_pdf(prj))
    errs = [x[0] for x in r["results"]]
    worst = r["results"][:5]
    return {
        "checked": r["checked"],
        "not_found_in_pdf": len(r["missing"]),
        "max_error_mm": round(max(errs), 3) if errs else 0,
        "within_0.5mm": sum(1 for e in errs if e <= 0.5),
        "worst": [{"err": round(w[0], 3), "page": w[1], "item": w[2], "ours": w[3], "kicad": w[4]} for w in worst],
    }


def list_checks() -> dict:
    return {k: {"severity": v[0], "what": v[1]} for k, v in CHECKS.items()}
