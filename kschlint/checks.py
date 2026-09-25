"""Layout and readability checks.

Each check yields Finding objects. Codes are stable so an AI or a CI job
can filter them. Severity: error (unreadable or misleading), warning
(poor style, may confuse), info (hint).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geom import Box, collinear_overlap, on_grid, point_inside_seg, point_on_seg, same_pt, seg_box_overlap
from .model import Page, Symbol
from .scene import Item, Scene, build

TEXT_KINDS = ("field", "pin_name", "pin_number", "label", "text", "sheet_field", "sheet_pin")
GRID = 1.27

# title block of the default KiCad drawing sheet, from the bottom-right corner
TITLE_W, TITLE_H, BORDER = 110.0, 34.0, 10.0


@dataclass(eq=False)
class Finding:
    code: str
    severity: str
    message: str
    page: str
    file: str
    at: tuple
    box: Box | None = None
    items: list = field(default_factory=list)  # [{"desc", "uuid", "kind"}]
    pages: list = field(default_factory=list)

    def key(self) -> tuple:
        return (self.code, self.file, tuple(sorted(i.get("uuid", "") + i.get("kind", "") + i.get("sub", "") for i in self.items)), round(self.at[0], 2), round(self.at[1], 2))

    def to_dict(self) -> dict:
        d = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "file": self.file,
            "pages": self.pages or [self.page],
            "at": [round(self.at[0], 3), round(self.at[1], 3)],
            "items": self.items,
        }
        if self.box is not None:
            d["box"] = self.box.as_list()
        return d


CHECKS = {
    "text-overlap": ("error", "Two texts overlap"),
    "text-over-body": ("error", "Text lies on a symbol body"),
    "field-on-own-body": ("warning", "Field drawn across its own symbol outline"),
    "text-over-wire": ("error", "A wire or pin runs through text"),
    "body-overlap": ("error", "Two symbol bodies overlap"),
    "wire-through-body": ("error", "Wire passes through a symbol body"),
    "wire-overlap": ("error", "Collinear wires overlap"),
    "label-floating": ("error", "Label is not on a wire or pin"),
    "wire-end-on-pin-line": ("error", "Wire ends on a pin line, not on the pin tip"),
    "pin-on-wire-middle": ("warning", "Pin tip touches the middle of a wire"),
    "missing-junction": ("warning", "T connection without a junction dot"),
    "four-way-junction": ("warning", "Four wires meet at one point"),
    "dangling-wire": ("warning", "Wire end is not connected"),
    "off-grid": ("warning", "Connection point off the 1.27 mm grid"),
    "outside-frame": ("warning", "Item outside the drawing frame or on the title block"),
    "text-too-close": ("info", "Text closer than the clearance to another item"),
    "vertical-field": ("info", "Reference or value is vertical"),
    "power-symbol-rotated": ("info", "Power symbol not upright"),
    "unneeded-junction": ("info", "Junction dot where only two wires meet"),
}


def _it(item: Item, page: Page, sub: str = "") -> dict:
    d = {"desc": item.describe(page), "kind": item.kind, "uuid": item.uid}
    if item.kind == "field":
        d["field"] = item.obj.name
    if sub:
        d["sub"] = sub
    return d


class Linter:
    def __init__(self, scene: Scene, clearance: float = 0.25, enabled: set | None = None):
        self.sc = scene
        self.page = scene.page
        self.clear = clearance
        self.enabled = enabled
        self.out: list[Finding] = []

    def add(self, code: str, msg: str, at, box=None, items=()):
        if self.enabled is not None and code not in self.enabled:
            return
        sev = CHECKS[code][0]
        self.out.append(Finding(code, sev, msg, self.page.name_path, self.page.sch.path, (at[0], at[1]), box, list(items)))

    # ------------------------------------------------------------------
    def run(self) -> list[Finding]:
        self.conn_points()
        self.text_overlaps()
        self.text_vs_body()
        self.text_vs_wire()
        self.bodies()
        self.wires()
        self.labels()
        self.grid()
        self.frame()
        self.style()
        return self.out

    # connection points of the page
    def conn_points(self):
        sc = self.sc
        pts = []
        for p in sc.pins:
            pts.append(((p.x, p.y), "pin", p))
        for lab in sc.sch.labels:
            pts.append(((lab.x, lab.y), "label", lab))
        for sh in sc.sch.sheets:
            for sp in sh.pins:
                pts.append(((sp.x, sp.y), "sheet_pin", sp))
        for j in sc.junctions:
            pts.append((j, "junction", None))
        for n in sc.no_connects:
            pts.append((n, "nc", None))
        for a, b in sc.sch.bus_entries:
            pts.append((a, "bus_entry", None))
            pts.append((b, "bus_entry", None))
        self.points = pts

    # ------------------------------------------------------------------
    def text_items(self) -> list[Item]:
        return [i for i in self.sc.items if i.kind in TEXT_KINDS]

    def text_overlaps(self):
        items = self.text_items()
        shrink = 0.08
        boxes = [i.box.grow(-shrink) for i in items]
        n = len(items)
        order = sorted(range(n), key=lambda k: boxes[k].x0)
        for oi, a in enumerate(order):
            ba = boxes[a]
            for b in order[oi + 1 :]:
                bb = boxes[b]
                if bb.x0 > ba.x1:
                    break
                ia, ib = items[a], items[b]
                if ia.kind == "sheet_pin" and ib.kind == "sheet_pin":
                    pass
                inter = ba.inter(bb)
                if inter is None or inter.area < 0.05:
                    continue
                # pin name and number of the same pin never collide in KiCad's own layout
                if ia.obj is ib.obj and ia.kind != ib.kind and {ia.kind, ib.kind} == {"pin_name", "pin_number"}:
                    continue
                self.add(
                    "text-overlap",
                    f"{ia.describe(self.page)} overlaps {ib.describe(self.page)}",
                    (inter.cx, inter.cy),
                    inter,
                    [_it(ia, self.page), _it(ib, self.page)],
                )

    def text_vs_body(self):
        bodies = self.sc.of_kind("body", "sheet")
        for t in self.text_items():
            tbx = t.box.grow(-0.1)
            for b in bodies:
                inter = tbx.inter(b.box.grow(-0.15))
                if inter is None or inter.area < 0.1:
                    continue
                own = t.owner is b.owner
                if own and t.kind in ("pin_name", "pin_number"):
                    continue  # pin names sit inside their own body by design
                if b.kind == "sheet" and t.kind in ("sheet_pin", "sheet_field") and own:
                    continue
                if own and t.kind == "field" and isinstance(t.owner, Symbol):
                    # big symbols may keep fields inside (for example a module outline)
                    if b.box.w > 15 and b.box.h > 15 and inter.area >= t.box.area * 0.95:
                        continue
                self.add(
                    "field-on-own-body" if own and t.kind == "field" else "text-over-body",
                    f"{t.describe(self.page)} lies on {b.describe(self.page)}",
                    (inter.cx, inter.cy),
                    inter,
                    [_it(t, self.page), _it(b, self.page)],
                )

    def text_vs_wire(self):
        segs = [(s, "wire") for s in self.sc.wires] + [(s, "pin") for s in self.sc.pin_segs]
        for t in self.text_items():
            tbx = t.box.grow(-0.12)
            for s, kind in segs:
                if kind == "pin":
                    if t.kind in ("pin_name", "pin_number") and t.obj is s.obj:
                        continue
                    if s.owner.is_power:
                        continue
                ln = seg_box_overlap(s.a, s.b, tbx)
                if ln < 0.2:
                    continue
                # hierarchical and global labels own the wire stub they sit on
                if t.kind == "label" and t.obj.kind != "label" and kind == "wire" and (same_pt(s.a, (t.obj.x, t.obj.y)) or same_pt(s.b, (t.obj.x, t.obj.y))):
                    continue
                if t.kind == "sheet_pin" and kind == "wire" and (same_pt(s.a, (t.obj.x, t.obj.y)) or same_pt(s.b, (t.obj.x, t.obj.y))):
                    continue
                what = "wire" if kind == "wire" else f"pin {s.obj.number} of {self.page.ref(s.owner)}"
                self.add(
                    "text-over-wire",
                    f"{what} runs through {t.describe(self.page)}",
                    (t.box.cx, t.box.cy),
                    t.box,
                    [_it(t, self.page)],
                )

    def bodies(self):
        bodies = self.sc.of_kind("body", "sheet")
        for i, a in enumerate(bodies):
            for b in bodies[i + 1 :]:
                inter = a.box.grow(-0.1).inter(b.box.grow(-0.1))
                if inter is not None and inter.area > 0.05:
                    self.add("body-overlap", f"{a.describe(self.page)} overlaps {b.describe(self.page)}", (inter.cx, inter.cy), inter, [_it(a, self.page), _it(b, self.page)])
        for s in self.sc.wires:
            for b in bodies:
                bx = b.box.grow(-0.2)
                ln = seg_box_overlap(s.a, s.b, bx)
                if ln > 0.3:
                    self.add(
                        "wire-through-body",
                        f"wire {_fmt(s.a)}-{_fmt(s.b)} crosses {b.describe(self.page)} ({ln:.1f} mm inside)",
                        ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2),
                        b.box,
                        [_it(b, self.page)],
                    )

    def wires(self):
        segs = [s for s in self.sc.wires if s.kind == "wire"]
        # collinear overlaps
        for i, a in enumerate(segs):
            for b in segs[i + 1 :]:
                ov = collinear_overlap(a.a, a.b, b.a, b.b)
                if ov > 0.01:
                    self.add("wire-overlap", f"wires {_fmt(a.a)}-{_fmt(a.b)} and {_fmt(b.a)}-{_fmt(b.b)} overlap by {ov:.2f} mm", a.a, None, [])
        # degree of every wire end and junction point
        ends = {}
        for s in segs:
            for p in (s.a, s.b):
                ends.setdefault((round(p[0], 3), round(p[1], 3)), []).append(s)
        pin_tips = [((p.x, p.y), p) for p in self.sc.pins]
        juncs = [(round(j[0], 3), round(j[1], 3)) for j in self.sc.junctions]
        cand = set(ends) | set(juncs)
        for pt in cand:
            n_end = len(ends.get(pt, []))
            n_mid = sum(1 for s in segs if point_inside_seg(pt, s.a, s.b))
            n_pin = sum(1 for (q, _p) in pin_tips if same_pt(pt, q))
            degree = n_end + 2 * n_mid + n_pin
            has_j = any(same_pt(pt, j) for j in juncs)
            if degree >= 3 and not has_j and n_mid > 0:
                self.add("missing-junction", f"T connection at {_fmt(pt)} has no junction dot", pt)
            if degree >= 4 and n_end + 2 * n_mid >= 4:
                self.add("four-way-junction", f"{degree} connections meet at {_fmt(pt)}. Offset one branch to make two T junctions", pt)
            if has_j and degree <= 2:
                self.add("unneeded-junction", f"junction at {_fmt(pt)} joins only {degree} items", pt)
        # dangling ends
        others = [p for p, k, _o in self.points]
        for pt, ss in ends.items():
            if len(ss) >= 2:
                continue
            if any(same_pt(pt, q) for q in others):
                continue
            if any(point_on_seg(pt, s.a, s.b) and ss[0] is not s for s in segs):
                continue
            if any(point_on_seg(pt, s.a, s.b) for s in self.sc.wires if s.kind == "bus"):
                continue
            self.add("dangling-wire", f"wire end at {_fmt(pt)} is not connected", pt)
        # pin tip on a wire middle, wire end on a pin line
        for p in self.sc.pins:
            tip = (p.x, p.y)
            for s in segs:
                if point_inside_seg(tip, s.a, s.b):
                    self.add(
                        "pin-on-wire-middle",
                        f"pin {p.number} of {self.page.ref(p.symbol)} touches wire {_fmt(s.a)}-{_fmt(s.b)} in the middle. It connects. Split the wire at the pin if intended, move it if not",
                        tip,
                    )
            for pt in ends:
                if point_inside_seg(pt, tip, (p.ex, p.ey), tol=1e-3) and not same_pt(pt, (p.ex, p.ey)):
                    self.add(
                        "wire-end-on-pin-line",
                        f"wire end {_fmt(pt)} lies on pin {p.number} of {self.page.ref(p.symbol)} but not on its tip {_fmt(tip)}. KiCad does not connect it",
                        pt,
                    )

    def labels(self):
        segs = self.sc.wires
        tips = [(p.x, p.y) for p in self.sc.pins]
        for lab in self.sc.sch.labels:
            pt = (lab.x, lab.y)
            if any(point_on_seg(pt, s.a, s.b) for s in segs) or any(same_pt(pt, t) for t in tips):
                continue
            item = next(i for i in self.sc.items if i.obj is lab)
            self.add("label-floating", f"{lab.kind} '{lab.text}' at {_fmt(pt)} touches no wire or pin", pt, item.box, [_it(item, self.page)])

    def grid(self):
        seen = set()
        for p in self.sc.pins:
            if p.symbol.uuid in seen:
                continue
            if not (on_grid(p.x, GRID) and on_grid(p.y, GRID)):
                seen.add(p.symbol.uuid)
                self.add("off-grid", f"{self.page.ref(p.symbol)} pin {p.number} at {_fmt((p.x, p.y))} is off grid. Move the symbol to the grid", (p.x, p.y))
        for s in self.sc.wires:
            for q in (s.a, s.b):
                if not (on_grid(q[0], GRID) and on_grid(q[1], GRID)):
                    self.add("off-grid", f"wire end {_fmt(q)} is off grid", q)
        for lab in self.sc.sch.labels:
            if not (on_grid(lab.x, GRID) and on_grid(lab.y, GRID)):
                self.add("off-grid", f"{lab.kind} '{lab.text}' at {_fmt((lab.x, lab.y))} is off grid", (lab.x, lab.y))

    def frame(self):
        w, h = self.sc.sch.paper
        inner = Box(BORDER, BORDER, w - BORDER, h - BORDER)
        title = Box(w - BORDER - TITLE_W, h - BORDER - TITLE_H, w - BORDER, h - BORDER)
        for it in self.sc.items:
            if it.kind == "text" and it.box.inter(title) is not None and it.box.inter(title).area > 0.2:
                pass
            b = it.box
            if b.x0 < inner.x0 - 0.01 or b.y0 < inner.y0 - 0.01 or b.x1 > inner.x1 + 0.01 or b.y1 > inner.y1 + 0.01:
                self.add("outside-frame", f"{it.describe(self.page)} is outside the drawing frame", (b.cx, b.cy), b, [_it(it, self.page)])
                continue
            inter = b.inter(title)
            if inter is not None and inter.area > 0.2:
                self.add("outside-frame", f"{it.describe(self.page)} lies on the title block", (b.cx, b.cy), b, [_it(it, self.page)])

    def style(self):
        for it in self.sc.of_kind("field"):
            if not isinstance(it.owner, Symbol) or it.obj.name not in ("Reference", "Value"):
                continue
            if it.box.h > it.box.w * 1.5 and len(it.text) > 1:
                self.add("vertical-field", f"{it.describe(self.page)} is vertical. Horizontal text reads better", (it.box.cx, it.box.cy), it.box, [_it(it, self.page)])
        for s in self.sc.sch.symbols:
            if s.is_power and (s.rot != 0 or s.mirror):
                if s.lib_id.startswith("power:PWR_FLAG"):
                    continue
                self.add("power-symbol-rotated", f"power symbol {s.field('Value').value if s.field('Value') else s.lib_id} at {_fmt((s.x, s.y))} is rotated {s.rot:g}. GND points down, supplies point up", (s.x, s.y))
        # clearance: text close to other things (not overlapping)
        if self.clear > 0:
            items = self.text_items()
            others = items + self.sc.of_kind("body")
            for t in items:
                g = t.box.grow(self.clear - 0.08)
                for o in others:
                    if o is t or o.owner is t.owner:
                        continue
                    if o.box.grow(-0.08).overlaps(t.box.grow(-0.08)):
                        continue  # reported as overlap
                    if g.overlaps(o.box.grow(-0.08)):
                        if id(o) < id(t) and o.kind in TEXT_KINDS:
                            continue  # report each pair once
                        self.add("text-too-close", f"{t.describe(self.page)} is within {self.clear} mm of {o.describe(self.page)}", (t.box.cx, t.box.cy), t.box, [_it(t, self.page), _it(o, self.page)])


def _fmt(p) -> str:
    return f"({p[0]:g}, {p[1]:g})"


def lint_page(page: Page, clearance: float = 0.25, enabled: set | None = None) -> list[Finding]:
    return Linter(build(page), clearance, enabled).run()


def lint_project(project, files: list[str] | None = None, clearance: float = 0.25, enabled: set | None = None, min_severity: str = "info") -> list[Finding]:
    """Lint every page. Findings that repeat on each instance of a reused sheet are merged."""
    import os

    order = {"error": 0, "warning": 1, "info": 2}
    want = None
    if files:
        want = {os.path.normcase(os.path.abspath(f)) for f in files}
    merged: dict = {}
    for page in project.pages:
        if want is not None and os.path.normcase(os.path.abspath(page.sch.path)) not in want:
            continue
        for f in lint_page(page, clearance, enabled):
            if order[f.severity] > order[min_severity]:
                continue
            k = f.key()
            if k in merged:
                if f.page not in merged[k].pages:
                    merged[k].pages.append(f.page)
            else:
                f.pages = [f.page]
                merged[k] = f
    return sorted(merged.values(), key=lambda f: (order[f.severity], f.file, f.code, f.at))
