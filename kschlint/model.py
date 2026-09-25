"""Schematic model built from ``.kicad_sch`` files (KiCad 7 to 10 format).

Only what layout checks need: geometry of symbols, pins, wires, labels,
texts and sheets, plus the source nodes so a fixer can patch them.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from . import sexpr
from .geom import Box
from .sexpr import Node

DEFAULT_TEXT = 1.27


# --------------------------------------------------------------------------
# small parse helpers
# --------------------------------------------------------------------------
def _f(node: Node | None, i: int = 0, default: float = 0.0) -> float:
    if node is None:
        return default
    v = node.arg(i)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _xy(node: Node | None) -> tuple[float, float]:
    return (_f(node, 0), _f(node, 1))


def _is_hidden(node: Node) -> bool:
    """KiCad 6/7 use a bare ``hide`` flag, KiCad 8+ use ``(hide yes)``."""
    if node.has_flag("hide"):
        return True
    h = node.child("hide")
    if h is not None and h.arg(0, "yes") != "no":
        return True
    eff = node.child("effects")
    if eff is not None:
        if eff.has_flag("hide"):
            return True
        h = eff.child("hide")
        if h is not None and h.arg(0, "yes") != "no":
            return True
    return False


@dataclass(eq=False)
class TextStyle:
    size_w: float = DEFAULT_TEXT
    size_h: float = DEFAULT_TEXT
    thickness: float | None = None
    bold: bool = False
    italic: bool = False
    hjust: str = "center"  # left | center | right
    vjust: str = "center"  # top | center | bottom
    mirror: bool = False

    @staticmethod
    def from_node(node: Node | None) -> "TextStyle":
        st = TextStyle()
        if node is None:
            return st
        eff = node.child("effects") if node.name != "effects" else node
        if eff is None:
            return st
        font = eff.child("font")
        if font is not None:
            size = font.child("size")
            if size is not None:
                st.size_h = _f(size, 0, DEFAULT_TEXT)
                st.size_w = _f(size, 1, DEFAULT_TEXT)
            th = font.child("thickness")
            if th is not None:
                st.thickness = _f(th)
            st.bold = font.has_flag("bold") or (font.child("bold") is not None and font.child("bold").arg(0, "yes") == "yes")
            st.italic = font.has_flag("italic") or (font.child("italic") is not None and font.child("italic").arg(0, "yes") == "yes")
        just = eff.child("justify")
        if just is not None:
            for a in just.atoms():
                if a in ("left", "right"):
                    st.hjust = a
                elif a in ("top", "bottom"):
                    st.vjust = a
                elif a == "mirror":
                    st.mirror = True
        return st


# --------------------------------------------------------------------------
# library symbols
# --------------------------------------------------------------------------
@dataclass(eq=False)
class Prim:
    kind: str  # rect | poly | circle | arc | text
    pts: list  # lib coords (y up)
    radius: float = 0.0
    filled: bool = False


@dataclass(eq=False)
class LibPin:
    number: str
    name: str
    etype: str
    x: float
    y: float
    angle: float
    length: float
    hidden: bool
    unit: int
    style: int
    name_size: float = DEFAULT_TEXT
    num_size: float = DEFAULT_TEXT


@dataclass(eq=False)
class LibSymbol:
    lib_id: str
    prims: list = field(default_factory=list)  # (unit, style, Prim)
    pins: list = field(default_factory=list)
    pin_name_offset: float = 0.508
    pin_names_hidden: bool = False
    pin_numbers_hidden: bool = False
    power: bool = False
    field_defaults: dict = field(default_factory=dict)

    @property
    def unit_count(self) -> int:
        units = {u for (u, _s, _p) in self.prims} | {p.unit for p in self.pins}
        return max(units | {1})

    def unit_prims(self, unit: int, style: int) -> list[Prim]:
        return [p for (u, s, p) in self.prims if u in (0, unit) and s in (0, style)]

    def unit_pins(self, unit: int, style: int) -> list[LibPin]:
        return [p for p in self.pins if p.unit in (0, unit) and p.style in (0, style)]


def _parse_unit_name(name: str) -> tuple[int, int]:
    parts = name.rsplit("_", 2)
    try:
        return int(parts[-2]), int(parts[-1])
    except (ValueError, IndexError):
        return 0, 0


def _parse_prim(g: Node) -> Prim | None:
    fill = g.child("fill")
    filled = False
    if fill is not None:
        t = fill.child("type")
        filled = t is not None and t.arg(0) not in (None, "none")
    k = g.name
    if k == "rectangle":
        s, e = _xy(g.child("start")), _xy(g.child("end"))
        return Prim("rect", [s, e], filled=filled)
    if k in ("polyline", "bezier"):
        pts = g.child("pts")
        if pts is None:
            return None
        return Prim("poly", [_xy(p) for p in pts.children("xy")], filled=filled)
    if k == "circle":
        c = _xy(g.child("center"))
        r = _f(g.child("radius"))
        return Prim("circle", [c], radius=r, filled=filled)
    if k == "arc":
        pts = [_xy(g.child(n)) for n in ("start", "mid", "end") if g.child(n) is not None]
        return Prim("arc", pts, filled=filled)
    if k == "text":
        return None  # graphic text inside symbols is ignored for the body box
    return None


def parse_lib_symbol(node: Node) -> LibSymbol:
    ls = LibSymbol(lib_id=node.arg(0, ""))
    pn = node.child("pin_names")
    if pn is not None:
        off = pn.child("offset")
        if off is not None:
            ls.pin_name_offset = _f(off)
        ls.pin_names_hidden = _is_hidden(pn)
    pnum = node.child("pin_numbers")
    if pnum is not None:
        ls.pin_numbers_hidden = _is_hidden(pnum)
    ls.power = node.child("power") is not None
    for prop in node.children("property"):
        ls.field_defaults[prop.arg(0, "")] = prop.arg(1, "")

    def walk(n: Node, unit: int, style: int) -> None:
        for c in n.children():
            if c.name == "symbol":
                u, s = _parse_unit_name(c.arg(0, ""))
                walk(c, u, s)
            elif c.name == "pin":
                at = c.child("at")
                name_n = c.child("name")
                num_n = c.child("number")
                ls.pins.append(
                    LibPin(
                        number=num_n.arg(0, "") if num_n else "",
                        name=name_n.arg(0, "") if name_n else "",
                        etype=c.arg(0, ""),
                        x=_f(at, 0),
                        y=_f(at, 1),
                        angle=_f(at, 2),
                        length=_f(c.child("length")),
                        hidden=_is_hidden(c),
                        unit=unit,
                        style=style,
                        name_size=TextStyle.from_node(name_n).size_h if name_n else DEFAULT_TEXT,
                        num_size=TextStyle.from_node(num_n).size_h if num_n else DEFAULT_TEXT,
                    )
                )
            else:
                p = _parse_prim(c)
                if p is not None:
                    ls.prims.append((unit, style, p))

    walk(node, 0, 0)
    return ls


# --------------------------------------------------------------------------
# placed items
# --------------------------------------------------------------------------
@dataclass(eq=False)
class Field:
    name: str
    value: str
    x: float
    y: float
    angle: float
    style: TextStyle
    hidden: bool
    node: Node
    show_name: bool = False
    owner: object = None

    @property
    def shown_text(self) -> str:
        return f"{self.name}: {self.value}" if self.show_name else self.value


@dataclass(eq=False)
class Symbol:
    lib_id: str
    x: float
    y: float
    rot: float
    mirror: str  # "" | "x" | "y"
    unit: int
    style: int
    uuid: str
    fields: list
    node: Node
    lib: LibSymbol | None
    refs: dict = field(default_factory=dict)  # instance path -> reference
    dnp: bool = False

    def field(self, name: str) -> Field | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    @property
    def is_power(self) -> bool:
        return bool(self.lib and self.lib.power) or self.lib_id.startswith("power:")

    def reference(self, path: str | None = None) -> str:
        if path and path in self.refs:
            return self.refs[path]
        f = self.field("Reference")
        return f.value if f else "?"

    # ---- transform: library coords (y up) to schematic coords (y down) ----
    def xf(self, x: float, y: float) -> tuple[float, float]:
        # lib y up -> screen y down
        px, py = x, -y
        a = math.radians(self.rot)
        ca, sa = round(math.cos(a), 12), round(math.sin(a), 12)
        # positive angle = counter-clockwise on screen (y down)
        rx = px * ca + py * sa
        ry = -px * sa + py * ca
        # KiCad rotates first, then mirrors in the screen frame (checked against kicad-cli)
        if self.mirror == "x":
            ry = -ry
        elif self.mirror == "y":
            rx = -rx
        return (round(self.x + rx, 4), round(self.y + ry, 4))

    def xf_dir(self, angle_deg: float) -> float:
        """Transform a direction given in lib degrees (y up) into screen degrees (y up convention)."""
        a = math.radians(angle_deg)
        dx, dy = math.cos(a), math.sin(a)
        x0, y0 = self.xf(0, 0)
        x1, y1 = self.xf(dx, dy)
        return math.degrees(math.atan2(-(y1 - y0), x1 - x0)) % 360


@dataclass(eq=False)
class Pin:
    symbol: Symbol
    lib: LibPin
    x: float  # connection point, schematic coords
    y: float
    ex: float  # inner end of the pin line
    ey: float

    @property
    def number(self) -> str:
        return self.lib.number

    @property
    def name(self) -> str:
        return self.lib.name


@dataclass(eq=False)
class Wire:
    kind: str  # wire | bus | polyline (graphic)
    pts: list
    uuid: str
    node: Node


@dataclass(eq=False)
class Label:
    kind: str  # label | global_label | hierarchical_label | netclass_flag
    text: str
    x: float
    y: float
    angle: float
    style: TextStyle
    shape: str
    uuid: str
    node: Node
    fields: list = field(default_factory=list)


@dataclass(eq=False)
class Text:
    kind: str  # text | text_box
    text: str
    x: float
    y: float
    angle: float
    style: TextStyle
    uuid: str
    node: Node
    size: tuple | None = None  # text_box only


@dataclass(eq=False)
class SheetPin:
    name: str
    shape: str
    x: float
    y: float
    angle: float
    style: TextStyle
    node: Node


@dataclass(eq=False)
class SheetBox:
    x: float
    y: float
    w: float
    h: float
    uuid: str
    name: str
    file: str
    fields: list
    pins: list
    node: Node

    @property
    def box(self) -> Box:
        return Box(self.x, self.y, self.x + self.w, self.y + self.h)


@dataclass(eq=False)
class Schematic:
    path: str
    text: str
    root: Node
    uuid: str
    paper: tuple  # (w, h) mm
    libs: dict
    symbols: list
    wires: list
    junctions: list
    no_connects: list
    labels: list
    texts: list
    sheets: list
    bus_entries: list

    def pins(self) -> list[Pin]:
        out = []
        for s in self.symbols:
            if s.lib is None:
                continue
            for lp in s.lib.unit_pins(s.unit, s.style):
                x, y = s.xf(lp.x, lp.y)
                a = math.radians(lp.angle)
                ex, ey = s.xf(lp.x + lp.length * math.cos(a), lp.y + lp.length * math.sin(a))
                out.append(Pin(s, lp, x, y, ex, ey))
        return out


PAPER = {
    "A5": (210, 148),
    "A4": (297, 210),
    "A3": (420, 297),
    "A2": (594, 420),
    "A1": (841, 594),
    "A0": (1189, 841),
    "A": (279.4, 215.9),
    "B": (431.8, 279.4),
    "C": (558.8, 431.8),
    "D": (863.6, 558.8),
    "E": (1117.6, 863.6),
    "USLetter": (279.4, 215.9),
    "USLegal": (355.6, 215.9),
    "USLedger": (431.8, 279.4),
}


def _paper(root: Node) -> tuple[float, float]:
    p = root.child("paper")
    if p is None:
        return PAPER["A4"]
    a = p.atoms()
    if a and a[0] == "User" and len(a) >= 3:
        w, h = float(a[1]), float(a[2])
    else:
        w, h = PAPER.get(a[0] if a else "A4", PAPER["A4"])
    if "portrait" in a:
        w, h = min(w, h), max(w, h)
    return (w, h)


def _fields(node: Node, owner) -> list[Field]:
    out = []
    for prop in node.children("property"):
        at = prop.child("at")
        sn = prop.child("show_name")
        out.append(
            Field(
                name=prop.arg(0, ""),
                value=prop.arg(1, ""),
                x=_f(at, 0),
                y=_f(at, 1),
                angle=_f(at, 2),
                style=TextStyle.from_node(prop),
                hidden=_is_hidden(prop),
                node=prop,
                show_name=sn is not None and sn.arg(0, "yes") == "yes",
                owner=owner,
            )
        )
    return out


def load(path: str) -> Schematic:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    return parse_schematic(text, path)


def parse_schematic(text: str, path: str = "<memory>") -> Schematic:
    root = sexpr.parse(text)
    libs: dict[str, LibSymbol] = {}
    ls = root.child("lib_symbols")
    if ls is not None:
        for s in ls.children("symbol"):
            lib = parse_lib_symbol(s)
            libs[lib.lib_id] = lib

    sch = Schematic(
        path=path,
        text=text,
        root=root,
        uuid=(root.child("uuid").arg(0, "") if root.child("uuid") else ""),
        paper=_paper(root),
        libs=libs,
        symbols=[],
        wires=[],
        junctions=[],
        no_connects=[],
        labels=[],
        texts=[],
        sheets=[],
        bus_entries=[],
    )

    for c in root.children():
        k = c.name
        uuid_n = c.child("uuid")
        uid = uuid_n.arg(0, "") if uuid_n else ""
        if k == "symbol":
            at = c.child("at")
            mir = c.child("mirror")
            lib_name = c.child("lib_name")
            lib_id = c.child("lib_id").arg(0, "") if c.child("lib_id") else ""
            key = lib_name.arg(0, "") if lib_name is not None else lib_id
            sym = Symbol(
                lib_id=lib_id,
                x=_f(at, 0),
                y=_f(at, 1),
                rot=_f(at, 2) % 360,
                mirror=mir.arg(0, "") if mir is not None else "",
                unit=int(_f(c.child("unit"), 0, 1)),
                style=int(_f(c.child("body_style") or c.child("convert"), 0, 1)),
                uuid=uid,
                fields=[],
                node=c,
                lib=libs.get(key),
            )
            dnp = c.child("dnp")
            sym.dnp = dnp is not None and dnp.arg(0, "yes") == "yes"
            sym.fields = _fields(c, sym)
            inst = c.child("instances")
            if inst is not None:
                for proj in inst.children("project"):
                    for p in proj.children("path"):
                        r = p.child("reference")
                        if r is not None:
                            sym.refs[p.arg(0, "")] = r.arg(0, "")
            sch.symbols.append(sym)
        elif k in ("wire", "bus", "polyline"):
            pts = c.child("pts")
            sch.wires.append(Wire(k, [_xy(p) for p in pts.children("xy")] if pts else [], uid, c))
        elif k == "junction":
            sch.junctions.append(_xy(c.child("at")))
        elif k == "no_connect":
            sch.no_connects.append(_xy(c.child("at")))
        elif k == "bus_entry":
            x, y = _xy(c.child("at"))
            sz = c.child("size")
            sch.bus_entries.append(((x, y), (x + _f(sz, 0), y + _f(sz, 1))))
        elif k in ("label", "global_label", "hierarchical_label", "netclass_flag", "directive_label"):
            at = c.child("at")
            shape = c.child("shape")
            lab = Label(
                kind=k,
                text=c.arg(0, ""),
                x=_f(at, 0),
                y=_f(at, 1),
                angle=_f(at, 2) % 360,
                style=TextStyle.from_node(c),
                shape=shape.arg(0, "") if shape is not None else "",
                uuid=uid,
                node=c,
            )
            lab.fields = _fields(c, lab)
            sch.labels.append(lab)
        elif k in ("text", "text_box"):
            at = c.child("at")
            sz = c.child("size")
            sch.texts.append(
                Text(
                    kind=k,
                    text=c.arg(0, ""),
                    x=_f(at, 0),
                    y=_f(at, 1),
                    angle=_f(at, 2) % 360,
                    style=TextStyle.from_node(c),
                    uuid=uid,
                    node=c,
                    size=(_f(sz, 0), _f(sz, 1)) if sz is not None else None,
                )
            )
        elif k == "sheet":
            at = c.child("at")
            sz = c.child("size")
            fields = _fields(c, None)
            name = next((f.value for f in fields if f.name in ("Sheetname", "Sheet name")), "")
            file = next((f.value for f in fields if f.name in ("Sheetfile", "Sheet file")), "")
            pins = []
            for p in c.children("pin"):
                pat = p.child("at")
                pins.append(SheetPin(p.arg(0, ""), p.arg(1, ""), _f(pat, 0), _f(pat, 1), _f(pat, 2), TextStyle.from_node(p), p))
            sb = SheetBox(_f(at, 0), _f(at, 1), _f(sz, 0), _f(sz, 1), uid, name, file, fields, pins, c)
            for f in fields:
                f.owner = sb
            sch.sheets.append(sb)
    return sch


# --------------------------------------------------------------------------
# project hierarchy
# --------------------------------------------------------------------------
@dataclass(eq=False)
class Page:
    """One instance of a sheet in the hierarchy (one page of the PDF)."""

    sch: Schematic
    path: str  # uuid path, e.g. /root-uuid/sheet-uuid
    name_path: str  # human path, e.g. /Actuators/HighSideSwitch12V_PumpMain/
    page_no: str = ""

    def ref(self, sym: Symbol) -> str:
        return sym.reference(self.path)


@dataclass(eq=False)
class Project:
    root_path: str
    files: dict  # abs path -> Schematic
    pages: list  # Page, hierarchy order

    def pages_for(self, sch_path: str) -> list[Page]:
        ap = os.path.normcase(os.path.abspath(sch_path))
        return [p for p in self.pages if os.path.normcase(os.path.abspath(p.sch.path)) == ap]


def find_root(path: str) -> str:
    """Accept a .kicad_pro, a root .kicad_sch or a directory."""
    if os.path.isdir(path):
        pros = [f for f in os.listdir(path) if f.endswith(".kicad_pro")]
        if not pros:
            raise FileNotFoundError(f"no .kicad_pro in {path}")
        path = os.path.join(path, pros[0])
    if path.endswith(".kicad_pro"):
        path = path[: -len(".kicad_pro")] + ".kicad_sch"
    return path


def load_project(path: str) -> Project:
    root_path = os.path.abspath(find_root(path))
    files: dict[str, Schematic] = {}
    pages: list[Page] = []

    def get(p: str) -> Schematic:
        ap = os.path.abspath(p)
        if ap not in files:
            files[ap] = load(ap)
        return files[ap]

    # page numbers live in the sheet instances of the parent
    def walk(sch_file: str, upath: str, npath: str, page_no: str, depth: int) -> None:
        if depth > 32:
            return
        sch = get(sch_file)
        pages.append(Page(sch, upath, npath, page_no))
        base = os.path.dirname(sch_file)
        for sh in sch.sheets:
            child_path = f"{upath}/{sh.uuid}"
            pno = ""
            inst = sh.node.child("instances")
            if inst is not None:
                for proj in inst.children("project"):
                    for p in proj.children("path"):
                        if p.arg(0, "") == upath or p.arg(0, "") == upath + "/":
                            pg = p.child("page")
                            if pg is not None:
                                pno = pg.arg(0, "")
            walk(os.path.join(base, sh.file), child_path, f"{npath}{sh.name}/", pno, depth + 1)

    root = get(root_path)
    walk(root_path, f"/{root.uuid}", "/", "1", 0)
    return Project(root_path, files, pages)
