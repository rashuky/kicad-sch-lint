"""Move texts out of collisions without touching connectivity.

What it may change:
- symbol fields (Reference, Value, other visible fields): position, angle
  (always made horizontal on screen) and justification
- local labels: slide along the wire segment they sit on, or flip side

What it never changes: symbols, pins, wires, junctions, hierarchical and
global labels, sheets. After writing, the CLI compares the kicad-cli netlist
before and after and restores the files on any difference.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from . import textbox as tb
from .checks import TEXT_KINDS, BORDER, TITLE_H, TITLE_W, lint_page
from .geom import Box, point_on_seg, same_pt, seg_box_overlap, snap
from .model import Field, Label, Page, Project, Schematic, Symbol, TextStyle
from .scene import Item, Scene, build, field_text
from .sexpr import Node, Patcher, fmt_num

MOVE_CODES = {"text-overlap", "text-over-body", "field-on-own-body", "text-over-wire", "outside-frame"}
FIELD_GAP = 0.635
POS_STEP = 0.127  # field anchors snap to 5 mil


@dataclass(eq=False)
class Move:
    kind: str  # field | label
    obj: object  # Field or Label
    owner: object  # Symbol or Label
    x: float
    y: float
    angle: float
    hjust: str
    vjust: str
    before: Box
    after: Box
    desc: str = ""


@dataclass(eq=False)
class Plan:
    file: str
    moves: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)  # descriptions


# --------------------------------------------------------------------------
# obstacle model
# --------------------------------------------------------------------------
class Obstacles:
    def __init__(self, scene: Scene, clearance: float):
        self.sc = scene
        self.clear = clearance
        w, h = scene.sch.paper
        self.frame = Box(BORDER + 1, BORDER + 1, w - BORDER - 1, h - BORDER - 1)
        self.title = Box(w - BORDER - TITLE_W, h - BORDER - TITLE_H, w - BORDER, h - BORDER)
        self.boxes: list[tuple[Box, object, str]] = []  # (box, item/owner, kind)
        self.segs: list[tuple[tuple, tuple, object]] = []
        for it in scene.items:
            self.boxes.append((it.box, it, it.kind))
        for s in scene.wires:
            self.segs.append((s.a, s.b, s))
        for s in scene.pin_segs:
            self.segs.append((s.a, s.b, s))

    def near(self, region: Box) -> "Obstacles":
        """Copy holding only obstacles that touch ``region`` (speeds up candidate search)."""
        o = Obstacles.__new__(Obstacles)
        o.sc, o.clear, o.frame, o.title = self.sc, self.clear, self.frame, self.title
        o.boxes = [b for b in self.boxes if b[0].overlaps(region)]
        o.segs = [s for s in self.segs if seg_box_overlap(s[0], s[1], region) > 0 or region.contains_pt(*s[0])]
        return o

    def remove(self, pred) -> None:
        self.boxes = [b for b in self.boxes if not pred(b[1])]

    def add_box(self, box: Box, tag) -> None:
        self.boxes.append((box, tag, "moved"))

    def cost(self, box: Box, ignore=()) -> tuple[float, int]:
        """(penalty, hard hits) of placing text at ``box``."""
        hard = 0
        pen = 0.0
        g = box.grow(self.clear)
        core = box.grow(-0.05)
        if not (self.frame.x0 <= box.x0 and self.frame.y0 <= box.y0 and box.x1 <= self.frame.x1 and box.y1 <= self.frame.y1):
            hard += 1
            pen += 100
        if box.overlaps(self.title):
            hard += 1
            pen += 100
        for b, tag, kind in self.boxes:
            if tag in ignore:
                continue
            inter = core.inter(b)
            if inter is not None:
                hard += 1
                pen += 50 + 20 * inter.area
            elif g.overlaps(b):
                pen += 2
        for a, bb, tag in self.segs:
            if tag in ignore:
                continue
            ln = seg_box_overlap(a, bb, core)
            if ln > 0:
                hard += 1
                pen += 40 + 10 * ln
            elif seg_box_overlap(a, bb, g) > 0:
                pen += 1.5
        return pen, hard


# --------------------------------------------------------------------------
# field placement
# --------------------------------------------------------------------------
def _horizontal_angle(sym: Symbol) -> float:
    return 90.0 if round(sym.rot) % 180 == 90 else 0.0


def _screen_box(text: str, style: TextStyle, x: float, y: float, angle: float, sym: Symbol) -> Box:
    return tb.field_box(text, x, y, angle, style, sym.rot, sym.mirror)


def _with_just(style: TextStyle, hjust: str, vjust: str = "center") -> TextStyle:
    return TextStyle(style.size_w, style.size_h, style.thickness, style.bold, style.italic, hjust, vjust, style.mirror)


def _solve_anchor(text: str, style: TextStyle, angle: float, sym: Symbol, want: str, tx: float, ty: float):
    """Pick file justify and anchor so the screen box is ``want``-aligned at (tx, ty).

    want: left (box starts at tx), right (box ends at tx), center (box centred on tx).
    ty is the vertical centre of the box. Returns (x, y, hjust, box).
    """
    best = None
    for hj in ("left", "center", "right"):
        st = _with_just(style, hj)
        b0 = _screen_box(text, st, 0.0, 0.0, angle, sym)
        # screen alignment this justify gives
        if abs(b0.x0) < 0.3:
            got = "left"
        elif abs(b0.x1) < 0.3:
            got = "right"
        else:
            got = "center"
        if got != want:
            continue
        if want == "left":
            ax = tx - b0.x0
        elif want == "right":
            ax = tx - b0.x1
        else:
            ax = tx - b0.cx
        ay = ty - b0.cy
        ax, ay = snap(ax, POS_STEP), snap(ay, POS_STEP)
        best = (ax, ay, hj, _screen_box(text, st, ax, ay, angle, sym))
        break
    if best is None:  # fall back to centre
        st = _with_just(style, "center")
        b0 = _screen_box(text, st, 0.0, 0.0, angle, sym)
        ax, ay = snap(tx - b0.cx, POS_STEP), snap(ty - b0.cy, POS_STEP)
        best = (ax, ay, "center", _screen_box(text, st, ax, ay, angle, sym))
    return best


def _pin_sides(sc: Scene, sym: Symbol, body: Box) -> set:
    sides = set()
    for p in sc.pins:
        if p.symbol is not sym:
            continue
        dx, dy = p.x - p.ex, p.y - p.ey
        if abs(dx) > abs(dy):
            sides.add("right" if dx > 0 else "left")
        elif abs(dy) > 0:
            sides.add("bottom" if dy > 0 else "top")
    return sides


def _extent(sc: Scene, sym: Symbol) -> tuple[Box, Box]:
    body = next((i.box for i in sc.items if i.kind == "body" and i.owner is sym), None)
    pts = [(p.x, p.y) for p in sc.pins if p.symbol is sym] + [(p.ex, p.ey) for p in sc.pins if p.symbol is sym]
    if body is None:
        body = Box.of_points(pts) if pts else Box(sym.x - 1, sym.y - 1, sym.x + 1, sym.y + 1)
    ext = body
    if pts:
        ext = ext.union(Box.of_points(pts))
    return body, ext


def plan_symbol_fields(sc: Scene, obs: Obstacles, sym: Symbol, texts: dict, force: bool = False):
    """Return (moves, cost_after) for the visible fields of ``sym`` or (None, cost_now) if no better spot."""
    fields = [f for f in sym.fields if not f.hidden and f.value]
    if not fields:
        return None, 0
    order = {"Reference": 0, "Value": 1}
    fields.sort(key=lambda f: order.get(f.name, 2))
    ignore = {i for i in sc.items if i.kind == "field" and i.owner is sym}
    body0, ext0 = _extent(sc, sym)
    reach = max(tb.advance(texts[f], f.style.size_w) for f in fields) + 20
    obs = obs.near(ext0.grow(reach))

    # current cost
    now_pen, now_hard = 0.0, 0
    cur_boxes = {}
    for f in fields:
        b = _screen_box(texts[f], f.style, f.x, f.y, f.angle, sym)
        cur_boxes[f.name] = b
        p, h = obs.cost(b, ignore)
        now_pen += p
        now_hard += h
    cb = list(cur_boxes.values())
    for i, a in enumerate(cb):
        for b in cb[i + 1 :]:
            if a.grow(-0.08).overlaps(b.grow(-0.08)):
                now_hard += 1
                now_pen += 50
    if now_hard == 0 and not force:
        return None, now_pen

    body, ext = _extent(sc, sym)
    pin_sides = _pin_sides(sc, sym, body)
    angle = _horizontal_angle(sym)
    sizes = []
    for f in fields:
        b = _screen_box(texts[f], _with_just(f.style, "left"), 0, 0, angle, sym)
        sizes.append((b.w, b.h))
    s_max = max(f.style.size_h for f in fields) / 1.27
    pitch = 2.54 * s_max
    gh = pitch * (len(fields) - 1) + max(h for _w, h in sizes)
    gw = max(w for w, _h in sizes)

    cands = []
    for side in ("right", "top", "left", "bottom"):
        for extra in (0.0, 1.27, 2.54):
            gap = FIELD_GAP + extra
            if side in ("right", "left"):
                # fields beside the body, clear of pin tips on that side only when pins are there
                edge_box = ext if side in pin_sides else body
                for k in (0, 1, -1, 2, -2, 3, -3, 4, -4, 6, -6):
                    cy = body.cy + k * 1.27
                    if side == "right":
                        x_left = edge_box.x1 + gap
                        cands.append((side, extra, k, "left", x_left, cy))
                    else:
                        x_right = edge_box.x0 - gap
                        cands.append((side, extra, k, "right", x_right, cy))
            else:
                edge_box = ext if side in pin_sides else body
                for k in (0, 1, -1, 2, -2, 3, -3, 4, -4, 6, -6):
                    cx = body.cx + k * 1.27
                    if side == "top":
                        cy = edge_box.y0 - gap - gh / 2
                    else:
                        cy = edge_box.y1 + gap + gh / 2
                    cands.append((side, extra, k, "center", cx, cy))

    best = None
    for side, extra, k, want, tx, gcy in cands:
        moves = []
        pen = 0.0
        hard = 0
        y_first = gcy - gh / 2 + sizes[0][1] / 2
        for idx, f in enumerate(fields):
            ty = y_first + idx * pitch
            ax, ay, hj, box = _solve_anchor(texts[f], f.style, angle, sym, want, tx, ty)
            p, h = obs.cost(box, ignore)
            # fields of the same symbol must not collide with each other
            for m in moves:
                if box.grow(-0.05).overlaps(m.after):
                    h += 1
                    p += 50
            pen += p
            hard += h
            moves.append(Move("field", f, sym, ax, ay, angle, hj, "center", cur_boxes[f.name], box))
        pref = (0 if side not in pin_sides else 6) + {"right": 0, "top": 0.5, "left": 1, "bottom": 1.5}[side]
        pen += pref + abs(k) * 0.6 + extra * 1.5
        key = (hard, pen)
        if best is None or key < best[0]:
            best = (key, moves)
    (hard, pen), moves = best
    if hard >= now_hard and not force:
        return None, now_pen
    return moves, pen


# --------------------------------------------------------------------------
# label placement
# --------------------------------------------------------------------------
def _label_segments(sc: Scene, lab: Label) -> list:
    return [s for s in sc.wires if s.kind == "wire" and point_on_seg((lab.x, lab.y), s.a, s.b)]


def plan_label(sc: Scene, obs: Obstacles, lab: Label, item: Item):
    if lab.kind != "label":
        return None
    segs = _label_segments(sc, lab)
    if not segs:
        return None
    ignore = {item}
    span = Box.of_points([p for s in segs for p in (s.a, s.b)]).grow(tb.advance(lab.text) + 5)
    obs = obs.near(span)
    now_pen, now_hard = obs.cost(item.box, ignore)
    if now_hard == 0:
        return None
    # other connection points on the wire: keep them free so the label does not look like it belongs to a pin
    horiz = round(lab.angle) % 180 == 0
    best = None
    for s in segs:
        (ax, ay), (bx, by) = s.a, s.b
        ln = math.hypot(bx - ax, by - ay)
        n = int(ln / 1.27 + 1e-6)
        for i in range(n + 1):
            t = i * 1.27 / ln if ln > 0 else 0
            x, y = snap(ax + (bx - ax) * t, 1.27), snap(ay + (by - ay) * t, 1.27)
            if not point_on_seg((x, y), s.a, s.b):
                continue
            seg_h = abs(by - ay) < 1e-6
            for hj in ("left", "right"):
                if seg_h and horiz:
                    ang = 0.0 if hj == "left" else 180.0
                elif not seg_h and not horiz:
                    ang = 90.0 if hj == "left" else 270.0
                else:
                    ang = lab.angle if hj == lab.style.hjust else (lab.angle + 180) % 360
                st = _with_just(lab.style, hj, "bottom")
                probe = Label(lab.kind, lab.text, x, y, ang, st, lab.shape, lab.uuid, lab.node)
                box = tb.label_box(probe)
                pen, hard = obs.cost(box, ignore)
                pen += math.hypot(x - lab.x, y - lab.y) * 0.3
                key = (hard, pen)
                if best is None or key < best[0]:
                    best = (key, Move("label", lab, lab, x, y, ang, hj, "bottom", item.box, box))
    if best is None or best[0][0] >= now_hard:
        return None
    return best[1]


# --------------------------------------------------------------------------
# planning over a project
# --------------------------------------------------------------------------
def _longest_texts(project: Project, sch: Schematic) -> dict:
    """Field text per Field, widest over all instances of the sheet."""
    out: dict = {}
    pages = project.pages_for(sch.path) or [None]
    for s in sch.symbols:
        for f in s.fields:
            best = ""
            for pg in pages:
                t = field_text(f, s, pg)
                if tb.advance(t) > tb.advance(best):
                    best = t
            out[f] = best
    return out


def plan_file(project: Project, sch: Schematic, clearance: float = 0.25, codes: set | None = None, labels: bool = True, horizontal: bool = False) -> Plan:
    codes = codes or MOVE_CODES
    pages = project.pages_for(sch.path)
    page = pages[0] if pages else Page(sch, f"/{sch.uuid}", "/")
    sc = build(page)
    texts = _longest_texts(project, sch)
    # use the widest instance text for the obstacle boxes of fields too
    for it in sc.items:
        if it.kind == "field" and isinstance(it.owner, Symbol) and it.obj in texts:
            it.text = texts[it.obj]
            it.box = _screen_box(it.text, it.obj.style, it.obj.x, it.obj.y, it.obj.angle, it.owner)
    obs = Obstacles(sc, clearance)
    plan = Plan(sch.path)

    wanted_syms: list = []
    wanted_labels: list = []
    seen = set()
    for f in lint_page(page, clearance, codes | ({"vertical-field"} if horizontal else set())):
        for it in f.items:
            uid = it.get("uuid")
            if not uid or uid in seen:
                continue
            obj = next((i for i in sc.items if i.uid == uid and i.kind == it.get("kind")), None)
            if obj is None:
                continue
            if obj.kind == "field" and isinstance(obj.owner, Symbol):
                seen.add(uid)
                wanted_syms.append(obj.owner)
            elif obj.kind == "label" and labels and obj.obj.kind == "label":
                seen.add(uid)
                wanted_labels.append(obj)

    # symbols with most text first: they are hardest to place
    wanted_syms.sort(key=lambda s: -sum(len(texts.get(f, "")) for f in s.fields if not f.hidden))
    for sym in wanted_syms:
        force = horizontal and any(
            (not f.hidden and f.value and f.name in ("Reference", "Value") and _screen_box(texts[f], f.style, f.x, f.y, f.angle, sym).h > _screen_box(texts[f], f.style, f.x, f.y, f.angle, sym).w * 1.5)
            for f in sym.fields
        )
        moves, _pen = plan_symbol_fields(sc, obs, sym, texts, force=force)
        own = [i for i in sc.items if i.kind == "field" and i.owner is sym]
        if moves is None:
            if not force:
                p_h = sum(obs.cost(i.box, set(own))[1] for i in own)
                if p_h:
                    plan.unresolved.append(f"{page.ref(sym)}: no free spot for its fields")
            continue
        obs.remove(lambda t: t in own)
        for m in moves:
            m.desc = f"{page.ref(sym)} {m.obj.name}"
            obs.add_box(m.after, m)
            plan.moves.append(m)
    for item in wanted_labels:
        m = plan_label(sc, obs, item.obj, item)
        if m is None:
            if obs.cost(item.box, {item})[1]:
                plan.unresolved.append(f"label '{item.text}': no free spot on its wire")
            continue
        m.desc = f"label '{item.text}'"
        obs.remove(lambda t: t is item)
        obs.add_box(m.after, m)
        plan.moves.append(m)
    return plan


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


def _indent_of(text: str, pos: int) -> str:
    ls = _line_start(text, pos)
    i = ls
    while i < len(text) and text[i] in " \t":
        i += 1
    return text[ls:i]


def _justify_text(hjust: str, vjust: str, mirror: bool) -> str:
    parts = [p for p in (hjust if hjust != "center" else "", vjust if vjust != "center" else "", "mirror" if mirror else "") if p]
    return f"(justify {' '.join(parts)})" if parts else ""


def _patch_effects(p: Patcher, text: str, node: Node, hjust: str, vjust: str, mirror: bool) -> None:
    eff = node.child("effects")
    if eff is None:
        return
    new_j = _justify_text(hjust, vjust, mirror)
    just = eff.child("justify")
    nl = _newline(text)
    if just is not None:
        if new_j:
            p.replace(just.start, just.end, new_j)
        else:
            # remove the whole line
            ls = _line_start(text, just.start)
            end = just.end
            if text.startswith(nl, end):
                end += len(nl)
            p.replace(ls, end, "")
    elif new_j:
        font = eff.child("font")
        anchor = font.end if font is not None else eff.items[0].end
        p.insert(anchor, nl + _indent_of(text, font.start if font is not None else eff.start) + new_j)


def _remove_autoplaced(p: Patcher, text: str, sym_node: Node) -> None:
    fa = sym_node.child("fields_autoplaced")
    if fa is None:
        return
    nl = _newline(text)
    ls = _line_start(text, fa.start)
    end = fa.end
    if text.startswith(nl, end):
        end += len(nl)
    if text[ls:fa.start].strip() == "":
        p.replace(ls, end, "")
    else:
        p.replace(fa.start, fa.end, "")


def apply_plan(sch: Schematic, plan: Plan) -> str:
    text = sch.text
    p = Patcher(text)
    touched_syms = set()
    for m in plan.moves:
        node = m.obj.node
        at = node.child("at")
        if at is None:
            continue
        p.replace(at.start, at.end, f"(at {fmt_num(m.x)} {fmt_num(m.y)} {fmt_num(m.angle)})")
        _patch_effects(p, text, node, m.hjust, m.vjust, m.obj.style.mirror)
        if m.kind == "field" and isinstance(m.owner, Symbol) and id(m.owner) not in touched_syms:
            touched_syms.add(id(m.owner))
            _remove_autoplaced(p, text, m.owner.node)
    return p.apply()


def plan_project(project: Project, files: list[str] | None = None, **kw) -> list[Plan]:
    want = None
    if files:
        want = {os.path.normcase(os.path.abspath(f)) for f in files}
    plans = []
    for path, sch in project.files.items():
        if want is not None and os.path.normcase(os.path.abspath(path)) not in want:
            continue
        plans.append(plan_file(project, sch, **kw))
    return plans
