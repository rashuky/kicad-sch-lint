"""Everything drawn on one page, as boxes and segments."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import textbox as tb
from .geom import Box
from .model import Field, Page, Schematic, Symbol


@dataclass(eq=False)
class Item:
    kind: str  # body | field | pin_name | pin_number | label | text | sheet | sheet_field | sheet_pin
    box: Box
    text: str = ""
    obj: object = None  # model object (Symbol, Field, Label, Text, SheetBox ...)
    owner: object = None  # Symbol / SheetBox / Label the item belongs to
    uid: str = ""

    def describe(self, page: Page | None = None) -> str:
        o = self.owner
        if isinstance(o, Symbol):
            ref = page.ref(o) if page else o.reference()
            if self.kind == "body":
                return f"{ref} body"
            if self.kind == "field":
                return f"{ref} {self.obj.name} '{self.text}'"
            return f"{ref} {self.kind} '{self.text}'"
        if self.kind in ("label",):
            return f"{self.obj.kind} '{self.text}'"
        if self.kind == "text":
            t = self.text if len(self.text) <= 30 else self.text[:27] + "..."
            return f"text '{t}'"
        if self.kind.startswith("sheet"):
            return f"sheet {getattr(o, 'name', '')} {self.kind} '{self.text}'"
        return f"{self.kind} '{self.text}'"


@dataclass(eq=False)
class Seg:
    a: tuple
    b: tuple
    kind: str  # wire | bus | pin | graphic
    obj: object = None
    owner: object = None


@dataclass(eq=False)
class Scene:
    page: Page
    items: list = field(default_factory=list)
    wires: list = field(default_factory=list)  # Seg, wire and bus
    pin_segs: list = field(default_factory=list)  # Seg, kind pin
    pins: list = field(default_factory=list)
    junctions: list = field(default_factory=list)
    no_connects: list = field(default_factory=list)

    @property
    def sch(self) -> Schematic:
        return self.page.sch

    def of_kind(self, *kinds: str) -> list[Item]:
        return [i for i in self.items if i.kind in kinds]


def unit_suffix(unit: int) -> str:
    s = ""
    n = unit
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def field_text(f: Field, sym: Symbol, page: Page | None) -> str:
    if f.name == "Reference":
        ref = page.ref(sym) if page else sym.reference()
        if sym.lib is not None and sym.lib.unit_count > 1:
            ref += unit_suffix(sym.unit)
        return f"Reference: {ref}" if f.show_name else ref
    return f.shown_text


def body_box(sym: Symbol) -> Box | None:
    if sym.lib is None:
        return None
    pts = []
    for p in sym.lib.unit_prims(sym.unit, sym.style):
        if p.kind == "circle":
            (cx, cy), r = p.pts[0], p.radius
            pts += [sym.xf(cx - r, cy - r), sym.xf(cx + r, cy + r)]
        elif p.kind == "rect":
            (x0, y0), (x1, y1) = p.pts
            pts += [sym.xf(x0, y0), sym.xf(x1, y1), sym.xf(x0, y1), sym.xf(x1, y0)]
        else:
            pts += [sym.xf(x, y) for (x, y) in p.pts]
    if not pts:
        return None
    return Box.of_points(pts)


def field_box(f: Field, sym: Symbol | None, text: str) -> Box:
    if sym is None:
        return tb.field_box(text, f.x, f.y, f.angle, f.style)
    return tb.field_box(text, f.x, f.y, f.angle, f.style, sym.rot, sym.mirror)


def build(page: Page) -> Scene:
    sch = page.sch
    sc = Scene(page)
    pins = sch.pins()
    sc.pins = pins
    for s in sch.symbols:
        bb = body_box(s)
        if bb is not None:
            sc.items.append(Item("body", bb, s.lib_id, s, s, s.uuid))
        for f in s.fields:
            if f.hidden or not f.value:
                continue
            t = field_text(f, s, page)
            sc.items.append(Item("field", field_box(f, s, t), t, f, s, s.uuid))
    for p in pins:
        lib = p.symbol.lib
        sc.pin_segs.append(Seg((p.x, p.y), (p.ex, p.ey), "pin", p, p.symbol))
        if lib is None or p.symbol.is_power:
            continue
        for kind, t, box in tb.pin_text_boxes(p, not lib.pin_names_hidden, not lib.pin_numbers_hidden, lib.pin_name_offset):
            sc.items.append(Item(kind, box, t, p, p.symbol, p.symbol.uuid))
    for w in sch.wires:
        if w.kind == "polyline":
            continue
        for a, b in zip(w.pts, w.pts[1:]):
            sc.wires.append(Seg(a, b, w.kind, w, w))
    for lab in sch.labels:
        sc.items.append(Item("label", tb.label_box(lab), lab.text, lab, lab, lab.uuid))
        for f in lab.fields:
            if f.hidden or not f.value or f.name == "Intersheetrefs":
                continue
            sc.items.append(Item("field", field_box(f, None, f.shown_text), f.shown_text, f, lab, lab.uuid))
    for t in sch.texts:
        if t.kind == "text_box" and t.size is not None:
            box = Box(t.x, t.y, t.x + t.size[0], t.y + t.size[1])
        else:
            box = tb.text_box(t.text, t.x, t.y, t.angle, t.style)
        sc.items.append(Item("text", box, t.text, t, t, t.uuid))
    for sh in sch.sheets:
        sc.items.append(Item("sheet", sh.box, sh.name, sh, sh, sh.uuid))
        for f in sh.fields:
            if f.hidden or not f.value:
                continue
            txt = f.value if f.name != "Sheetfile" else f"File: {f.value}"
            sc.items.append(Item("sheet_field", field_box(f, None, txt), txt, f, sh, sh.uuid))
        for sp in sh.pins:
            sc.items.append(Item("sheet_pin", tb.sheet_pin_box(sp, sh.box), sp.name, sp, sh, sh.uuid))
    sc.junctions = list(sch.junctions)
    sc.no_connects = list(sch.no_connects)
    return sc
