"""Move PCB reference designators off pads, silkscreen and other parts.

Runs inside KiCad's python (needs pcbnew). KiCad computes every box, this
script only searches for a free spot. Usage:

    <kicad python> pcb_silk.py BOARD.kicad_pcb [--write] [--refs R1,R2] [--min-size 0.8]

Prints JSON. Only Reference fields on silkscreen are moved (position, angle
0 or 90, optionally a smaller size). Footprints, pads, tracks and zones are
never touched.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

import pcbnew

TO = pcbnew.ToMM
MM = pcbnew.FromMM

PAD_CLEAR = 0.15  # silk to pad copper
SILK_CLEAR = 0.15  # silk to silk
EDGE_CLEAR = 0.5  # silk to board edge
COURT_CLEAR = 0.1  # to other parts' courtyards
ATTRIB = 0.3  # text must be this much nearer its own courtyard than any other
CRLF, LF = chr(13) + chr(10), chr(10)


def box(b) -> tuple:
    return (TO(b.GetX()), TO(b.GetY()), TO(b.GetRight()), TO(b.GetBottom()))


def grow(b, d):
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def box_dist(a, b) -> float:
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    return math.hypot(dx, dy)


def seg_hits_box(p, q, w, b) -> bool:
    """Segment p-q with line width w intersects box b."""
    b = grow(b, w / 2)
    x0, y0 = p
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, x0 - b[0]), (dx, b[2] - x0), (-dy, y0 - b[1]), (dy, b[3] - y0)):
        if abs(pp) < 1e-12:
            if qq < 0:
                return False
        else:
            r = qq / pp
            if pp < 0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
    return t1 >= t0


def pt(v):
    return (TO(v.x), TO(v.y))


def shape_segments(sh) -> list:
    """Silk shape as (p, q, width) segments, or [] with a box fallback handled by the caller."""
    w = TO(sh.GetWidth())
    t = sh.GetShape()
    if t == pcbnew.SHAPE_T_SEGMENT:
        return [(pt(sh.GetStart()), pt(sh.GetEnd()), w)]
    if t == pcbnew.SHAPE_T_RECT:
        corners = [pt(c) for c in sh.GetRectCorners()]
        return [(corners[i], corners[(i + 1) % 4], w) for i in range(4)]
    if t == pcbnew.SHAPE_T_CIRCLE:
        c = pt(sh.GetCenter())
        r = TO(sh.GetRadius())
        n = 24
        ps = [(c[0] + r * math.cos(2 * math.pi * k / n), c[1] + r * math.sin(2 * math.pi * k / n)) for k in range(n + 1)]
        return [(ps[k], ps[k + 1], w) for k in range(n)]
    if t == pcbnew.SHAPE_T_ARC:
        c = pt(sh.GetCenter())
        r = TO(sh.GetRadius())
        s = pt(sh.GetStart())
        a0 = math.atan2(s[1] - c[1], s[0] - c[0])
        span = math.radians(sh.GetArcAngle().AsDegrees())
        n = max(4, int(abs(span) / (math.pi / 12)))
        ps = [(c[0] + r * math.cos(a0 + span * k / n), c[1] + r * math.sin(a0 + span * k / n)) for k in range(n + 1)]
        return [(ps[k], ps[k + 1], w) for k in range(n)]
    return []


class Board:
    def __init__(self, path: str):
        self.path = path
        self.b = pcbnew.LoadBoard(path)
        self.edge = box(self.b.GetBoardEdgesBoundingBox())
        self.fps = list(self.b.GetFootprints())
        self._obs = {}

    def side_silk(self, fp) -> int:
        return pcbnew.B_SilkS if fp.GetLayer() == pcbnew.B_Cu else pcbnew.F_SilkS

    def courtyard(self, fp) -> tuple:
        for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            try:
                poly = fp.GetCourtyard(layer)
                if poly.OutlineCount():
                    return box(poly.BBox())
            except Exception:
                pass
        pads = [box(p.GetBoundingBox()) for p in fp.Pads()]
        if pads:
            return (min(p[0] for p in pads), min(p[1] for p in pads), max(p[2] for p in pads), max(p[3] for p in pads))
        x, y = pt(fp.GetPosition())
        return (x - 0.5, y - 0.5, x + 0.5, y + 0.5)

    def obstacles(self, silk_layer: int):
        """(pad boxes, silk segments, silk boxes with owner text object, courtyards by footprint). Cached."""
        if silk_layer not in self._obs:
            self._obs[silk_layer] = self._build(silk_layer)
        return self._obs[silk_layer]

    def moved(self, field) -> None:
        """Refresh the cached box of a field after it moved."""
        for pads, segs, boxes, courts in self._obs.values():
            for i, (b, obj) in enumerate(boxes):
                if uid(obj) == uid(field):
                    boxes[i] = (box(field.GetBoundingBox()), obj)

    def _build(self, silk_layer: int):
        cu = pcbnew.F_Cu if silk_layer == pcbnew.F_SilkS else pcbnew.B_Cu
        pads, segs, boxes = [], [], []
        for fp in self.fps:
            for p in fp.Pads():
                if p.IsOnLayer(cu):
                    pads.append(box(p.GetBoundingBox()))
            for it in fp.GraphicalItems():
                if it.GetLayer() != silk_layer:
                    continue
                if isinstance(it, pcbnew.PCB_SHAPE):
                    ss = shape_segments(it)
                    if ss:
                        segs.extend(ss)
                    else:
                        boxes.append((box(it.GetBoundingBox()), it))
                elif it.IsVisible() if hasattr(it, "IsVisible") else True:
                    boxes.append((box(it.GetBoundingBox()), it))
            for f in fp.GetFields():
                if f.GetLayer() == silk_layer and f.IsVisible():
                    boxes.append((box(f.GetBoundingBox()), f))
        for it in self.b.GetDrawings():
            if it.GetLayer() != silk_layer:
                continue
            if isinstance(it, pcbnew.PCB_SHAPE):
                ss = shape_segments(it)
                if ss:
                    segs.extend(ss)
                    continue
            boxes.append((box(it.GetBoundingBox()), it))
        courts = {fp.GetReference(): self.courtyard(fp) for fp in self.fps}
        return pads, segs, boxes, courts


def uid(obj) -> str:
    return obj.m_Uuid.AsString()


def hits(tb, fp_ref, own_uid, pads, segs, boxes, courts, edge) -> int:
    n = 0
    if not (edge[0] + EDGE_CLEAR <= tb[0] and edge[1] + EDGE_CLEAR <= tb[1] and tb[2] <= edge[2] - EDGE_CLEAR and tb[3] <= edge[3] - EDGE_CLEAR):
        n += 1
    g = grow(tb, PAD_CLEAR)
    n += sum(1 for p in pads if overlap(g, p))
    gs = grow(tb, SILK_CLEAR)
    n += sum(1 for (p, q, w) in segs if seg_hits_box(p, q, w, gs))
    n += sum(1 for (b, obj) in boxes if uid(obj) != own_uid and overlap(gs, b))
    own = courts[fp_ref]
    d_own = box_dist(tb, own)
    for ref, c in courts.items():
        if ref == fp_ref:
            continue
        if overlap(grow(tb, COURT_CLEAR), c):
            n += 1
        elif box_dist(tb, c) < d_own + ATTRIB:
            n += 1
    return n


def place(board: Board, fp, min_size: float) -> dict | None:
    f = fp.Reference()
    layer = board.side_silk(fp)
    pads, segs, boxes, courts = board.obstacles(layer)
    own_field = uid(f)
    ref = fp.GetReference()
    old = (pt(f.GetPosition()), f.GetTextAngleDegrees(), TO(f.GetTextHeight()), TO(f.GetTextWidth()), TO(f.GetTextThickness()))
    now = hits(box(f.GetBoundingBox()), ref, own_field, pads, segs, boxes, courts, board.edge)
    if now == 0:
        return None
    court = courts[ref]
    cx, cy = (court[0] + court[2]) / 2, (court[1] + court[3]) / 2
    sizes = [old[2]]
    if min_size and min_size < old[2]:
        sizes.append(min_size)
    best = None
    for si, size in enumerate(sizes):
        scale = size / old[2]
        f.SetTextHeight(MM(size))
        f.SetTextWidth(MM(old[3] * scale))
        f.SetTextThickness(MM(min(old[4], max(0.15, old[4] * scale))))
        for ang in (0.0, 90.0):
            f.SetTextAngleDegrees(ang)
            f.SetPosition(pcbnew.VECTOR2I(MM(cx), MM(cy)))
            tb0 = box(f.GetBoundingBox())
            w, h = tb0[2] - tb0[0], tb0[3] - tb0[1]
            for gap in (0.15, 0.4, 0.8):
                for k in (0, 1, -1, 2, -2, 3, -3, 4, -4, 6, -6):
                    d = k * 0.5
                    cands = [
                        ("top", cx + d, court[1] - gap - h / 2),
                        ("bottom", cx + d, court[3] + gap + h / 2),
                        ("right", court[2] + gap + w / 2, cy + d),
                        ("left", court[0] - gap - w / 2, cy + d),
                    ]
                    for side, x, y in cands:
                        tb = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
                        n = hits(tb, ref, own_field, pads, segs, boxes, courts, board.edge)
                        if n:
                            continue
                        pen = abs(k) * 0.5 + gap * 2 + {"top": 0, "bottom": 0.3, "right": 0.6, "left": 0.9}[side] + (2 if ang else 0) + si * 6
                        if best is None or pen < best[0]:
                            best = (pen, x, y, ang, size, side)
        if best is not None:
            break
    # restore, the caller applies the chosen spot
    f.SetTextHeight(MM(old[2]))
    f.SetTextWidth(MM(old[3]))
    f.SetTextThickness(MM(old[4]))
    f.SetTextAngleDegrees(old[1])
    f.SetPosition(pcbnew.VECTOR2I(MM(old[0][0]), MM(old[0][1])))
    if best is None:
        return {"ref": ref, "unresolved": True, "hits": now}
    _pen, x, y, ang, size, side = best
    scale = size / old[2]
    f.SetTextHeight(MM(size))
    f.SetTextWidth(MM(old[3] * scale))
    f.SetTextThickness(MM(min(old[4], max(0.15, old[4] * scale))))
    f.SetTextAngleDegrees(ang)
    f.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
    # the chosen spot was computed from the centre, KiCad's box may be offset by justification
    tb = box(f.GetBoundingBox())
    ox, oy = x - (tb[0] + tb[2]) / 2, y - (tb[1] + tb[3]) / 2
    if abs(ox) > 1e-3 or abs(oy) > 1e-3:
        f.SetPosition(pcbnew.VECTOR2I(MM(x + ox), MM(y + oy)))
    board.moved(f)
    return {
        "ref": ref,
        "from": [round(old[0][0], 3), round(old[0][1], 3), old[1], old[2]],
        "to": [round(x, 3), round(y, 3), ang, size],
        "side": side,
        "was_hitting": now,
    }


def write_minimal(bd: Board, path: str, refs: set) -> None:
    """Let KiCad save to a temp file, then copy only the moved Reference nodes into the original.

    A full KiCad save rewrites defaults all over the file. This keeps the diff to the moved fields.
    """
    import os
    import tempfile

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from kschlint import sexpr

    fd, tmp = tempfile.mkstemp(suffix=".kicad_pcb")
    os.close(fd)
    try:
        bd.b.Save(tmp)
        with open(tmp, encoding="utf-8", newline="") as fh:
            new_text = fh.read()
    finally:
        os.remove(tmp)
    with open(path, encoding="utf-8", newline="") as fh:
        old_text = fh.read()

    def ref_props(text):
        out = {}
        for fp in sexpr.parse(text).children("footprint"):
            for prop in fp.children("property"):
                if prop.arg(0) == "Reference" and prop.arg(1) in refs:
                    out[prop.arg(1)] = prop
        return out

    new_props = ref_props(new_text)
    p = sexpr.Patcher(old_text)
    for ref, prop in ref_props(old_text).items():
        np_ = new_props[ref]
        for name in ("at", "effects"):
            o, n = prop.child(name), np_.child(name)
            if o is None or n is None:
                continue
            new_node = new_text[n.start : n.end]
            if CRLF in old_text:
                new_node = new_node.replace(CRLF, LF).replace(LF, CRLF)
            if name == "effects" and "(size" in new_node:
                # keep the original effects unless the size changed (KiCad adds default thickness lines)
                if (o.child("font") and n.child("font") and o.child("font").child("size") and
                        old_text[o.child("font").child("size").start:o.child("font").child("size").end] ==
                        new_text[n.child("font").child("size").start:n.child("font").child("size").end]):
                    continue
            p.replace(o.start, o.end, new_node)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(p.apply())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--refs", default="")
    ap.add_argument("--min-size", type=float, default=0.0, help="allow shrinking reference text to this height (mm) when no spot fits")
    ap.add_argument("--offboard", action="store_true", help="also move references that stick out of the board outline")
    a = ap.parse_args(argv)
    bd = Board(a.board)
    want = {r for r in a.refs.split(",") if r}
    moves, unresolved = [], []
    # small parts first: they have the fewest options near them
    fps = sorted(bd.fps, key=lambda fp: (lambda c: (c[2] - c[0]) * (c[3] - c[1]))(bd.courtyard(fp)))
    for fp in fps:
        f = fp.Reference()
        if not f.IsVisible() or f.GetLayer() not in (pcbnew.F_SilkS, pcbnew.B_SilkS):
            continue
        if want and fp.GetReference() not in want:
            # DRC does not flag text past the board outline, the fab just drops it
            tb = box(f.GetBoundingBox())
            e = bd.edge
            if not (a.offboard and (tb[0] < e[0] or tb[1] < e[1] or tb[2] > e[2] or tb[3] > e[3])):
                continue
        r = place(bd, fp, a.min_size)
        if r is None:
            continue
        (unresolved if r.get("unresolved") else moves).append(r)
    if a.write and moves:
        write_minimal(bd, a.board, {m["ref"] for m in moves})
    print(json.dumps({"moves": moves, "unresolved": unresolved, "written": bool(a.write and moves)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
