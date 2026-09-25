"""Small 2D geometry helpers. Units are mm, schematic frame (y points down)."""

from __future__ import annotations

import math
from dataclasses import dataclass

EPS = 1e-4


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @staticmethod
    def of_points(pts) -> "Box":
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return Box(min(xs), min(ys), max(xs), max(ys))

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def grow(self, d: float) -> "Box":
        return Box(self.x0 - d, self.y0 - d, self.x1 + d, self.y1 + d)

    def union(self, o: "Box") -> "Box":
        return Box(min(self.x0, o.x0), min(self.y0, o.y0), max(self.x1, o.x1), max(self.y1, o.y1))

    def inter(self, o: "Box") -> "Box | None":
        b = Box(max(self.x0, o.x0), max(self.y0, o.y0), min(self.x1, o.x1), min(self.y1, o.y1))
        if b.x1 - b.x0 <= EPS or b.y1 - b.y0 <= EPS:
            return None
        return b

    def overlaps(self, o: "Box") -> bool:
        return self.inter(o) is not None

    def contains_pt(self, x: float, y: float, tol: float = EPS) -> bool:
        return self.x0 - tol <= x <= self.x1 + tol and self.y0 - tol <= y <= self.y1 + tol

    def strictly_contains_pt(self, x: float, y: float, tol: float = EPS) -> bool:
        return self.x0 + tol < x < self.x1 - tol and self.y0 + tol < y < self.y1 - tol

    def as_list(self, nd: int = 3) -> list[float]:
        return [round(self.x0, nd), round(self.y0, nd), round(self.x1, nd), round(self.y1, nd)]


def seg_box_overlap(a, b, box: Box) -> float:
    """Length of segment a-b that lies strictly inside ``box`` (Liang-Barsky clip)."""
    x0, y0 = a
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - box.x0), (dx, box.x1 - x0), (-dy, y0 - box.y0), (dy, box.y1 - y0)):
        if abs(p) < 1e-12:
            if q <= EPS:
                return 0.0
        else:
            r = q / p
            if p < 0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
    if t1 - t0 <= 0:
        return 0.0
    return (t1 - t0) * math.hypot(dx, dy)


def point_on_seg(p, a, b, tol: float = 1e-3) -> bool:
    """True if point p lies on segment a-b (endpoints included)."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    if not (min(ax, bx) - tol <= px <= max(ax, bx) + tol and min(ay, by) - tol <= py <= max(ay, by) + tol):
        return False
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    ln = math.hypot(bx - ax, by - ay)
    if ln < tol:
        return math.hypot(px - ax, py - ay) <= tol
    return abs(cross) / ln <= tol


def point_inside_seg(p, a, b, tol: float = 1e-3) -> bool:
    """True if p is on a-b but not at either endpoint."""
    return point_on_seg(p, a, b, tol) and not same_pt(p, a, tol) and not same_pt(p, b, tol)


def same_pt(p, q, tol: float = 1e-3) -> bool:
    return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol


def collinear_overlap(a, b, c, d, tol: float = 1e-3) -> float:
    """Overlap length of two collinear segments, 0 if not collinear."""
    if not (point_on_line(c, a, b, tol) and point_on_line(d, a, b, tol)):
        return 0.0
    ux, uy = b[0] - a[0], b[1] - a[1]
    ln = math.hypot(ux, uy)
    if ln < tol:
        return 0.0
    ux, uy = ux / ln, uy / ln
    t = sorted([0.0, ln])
    s = sorted([(c[0] - a[0]) * ux + (c[1] - a[1]) * uy, (d[0] - a[0]) * ux + (d[1] - a[1]) * uy])
    return max(0.0, min(t[1], s[1]) - max(t[0], s[0]))


def point_on_line(p, a, b, tol: float = 1e-3) -> bool:
    ln = math.hypot(b[0] - a[0], b[1] - a[1])
    if ln < tol:
        return same_pt(p, a, tol)
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    return abs(cross) / ln <= tol


def segs_cross(a, b, c, d) -> bool:
    """Proper crossing (interiors intersect at a single point)."""

    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return o1 * o2 < 0 and o3 * o4 < 0


def on_grid(v: float, grid: float, tol: float = 1e-3) -> bool:
    r = v / grid
    return abs(r - round(r)) * grid <= tol


def snap(v: float, grid: float) -> float:
    return round(round(v / grid) * grid, 4)
