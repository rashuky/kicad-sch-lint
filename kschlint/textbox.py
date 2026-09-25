"""Text extents as KiCad draws them.

All numbers were measured from kicad-cli PDF output (KiCad 10, default
stroke font) and are checked by ``tests/test_textbox.py``. Offsets are in mm
for 1.27 mm text and scale with the text size.
"""

from __future__ import annotations

import math
import re

from .geom import Box

# advance width per character for 1.27 mm text (KiCad newstroke font)
ADV = {
    ' ': 0.9677, '!': 0.6045, '"': 0.9665, '#': 1.27, '$': 1.209, '%': 1.4503, '&': 1.5723,
    "'": 0.6045, '(': 0.8458, ')': 0.8458, '*': 0.9665, '+': 1.5723, ',': 0.6045, '-': 1.5723,
    '.': 0.6045, '/': 1.3297, '0': 1.209, '1': 1.209, '2': 1.209, '3': 1.209, '4': 1.209,
    '5': 1.209, '6': 1.209, '7': 1.209, '8': 1.209, '9': 1.209, ':': 0.6045, ';': 0.6045,
    '<': 1.5723, '=': 1.5723, '>': 1.5723, '?': 1.0884, '@': 1.632, 'A': 1.0884, 'B': 1.27,
    'C': 1.27, 'D': 1.27, 'E': 1.1481, 'F': 1.0884, 'G': 1.27, 'H': 1.3297, 'I': 0.6045,
    'J': 0.9665, 'K': 1.27, 'L': 1.0274, 'M': 1.4503, 'N': 1.3297, 'O': 1.3297, 'P': 1.27,
    'Q': 1.3297, 'R': 1.27, 'S': 1.209, 'T': 0.9665, 'U': 1.3297, 'V': 1.0884, 'W': 1.4503,
    'X': 1.209, 'Y': 1.0884, 'Z': 1.209, '[': 0.8458, '\\': 0.8458, ']': 0.8458, '^': 0.7252,
    '_': 0.9665, '`': 0.4826, 'a': 1.1481, 'b': 1.1481, 'c': 1.0884, 'd': 1.1481, 'e': 1.0884,
    'f': 0.7252, 'g': 1.1481, 'h': 1.1481, 'i': 0.6045, 'j': 0.6045, 'k': 1.0274, 'l': 0.6642,
    'm': 1.6929, 'n': 1.1481, 'o': 1.1481, 'p': 1.1481, 'q': 1.1481, 'r': 0.7861, 's': 1.0274,
    't': 0.7252, 'u': 1.1481, 'v': 0.9665, 'w': 1.3297, 'x': 1.0274, 'y': 0.9665, 'z': 1.0274,
    '{': 0.8458, '|': 1.209, '}': 0.8458, '~': 0.9068,
}
ADV_DEFAULT = 1.209  # non-ASCII glyphs: typical digit width
REF = 1.27
TRAIL = 0.24  # end-of-line spacing KiCad leaves when right/center justifying

_MARKUP = re.compile(r"[~_^]\{([^{}]*)\}")


def plain(text: str) -> str:
    """Strip KiCad markup: ~{overbar}, _{sub}, ^{super}."""
    prev = None
    while prev != text:
        prev = text
        text = _MARKUP.sub(r"\1", text)
    return text


def lines(text: str) -> list[str]:
    return plain(text).split("\n")


def advance(text: str, size_w: float = REF, bold: bool = False) -> float:
    w = max((sum(ADV.get(c, ADV_DEFAULT) for c in ln) for ln in lines(text)), default=0.0)
    w *= size_w / REF
    if bold:
        w *= 1.08
    return w


# vertical extents (top, bottom) relative to the anchor for one line, per kind and vjust
_V_FIELD = {"top": (-0.102, 1.804), "center": (-0.827, 1.079), "bottom": (-1.552, 0.354)}
_V_TEXT = {"top": (-0.334, 1.572), "center": (-1.077, 0.829), "bottom": (-1.82, 0.086)}
_V_LABEL = (-1.919, -0.013)
_V_HLABEL = (-0.827, 1.079)
_V_GLABEL = (-0.736, 1.17)
LINE_PITCH = 1.905  # extra lines for multi-line text, per 1.27 mm

# where label text starts, measured from the anchor along the reading direction
_HLABEL_OFF = 1.434
_GLABEL_OFF = {"input": 1.402, "bidirectional": 1.402, "tri_state": 1.402, "output": 0.449, "passive": 0.449}


def _local_box(width: float, hjust: str, vr: tuple, s: float, n_lines: int, start: float = 0.0, vjust: str = "center"):
    """Box in the reading frame (x along the text, y down), anchor at 0,0."""
    extra = (n_lines - 1) * LINE_PITCH * s
    top, bot = vr[0] * s, vr[1] * s
    # multi-line text grows away from the anchor side
    if vjust == "bottom":
        top -= extra
    elif vjust == "top":
        bot += extra
    else:
        top -= extra / 2
        bot += extra / 2
    t = TRAIL * s
    if hjust == "left":
        x0, x1 = start, start + width
    elif hjust == "right":
        x0, x1 = -start - width - t, -start - t
    else:
        x0, x1 = -width / 2 - t / 2, width / 2 - t / 2
    return x0, top, x1, bot


def _place(local, x: float, y: float, rot: float, mirror: str = "") -> Box:
    """Rotate the local box by ``rot`` (CCW on screen) about the anchor, then mirror."""
    x0, y0, x1, y1 = local
    a = math.radians(rot)
    ca, sa = round(math.cos(a), 12), round(math.sin(a), 12)
    pts = []
    for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        rx = px * ca + py * sa
        ry = -px * sa + py * ca
        if mirror == "x":
            ry = -ry
        elif mirror == "y":
            rx = -rx
        pts.append((x + rx, y + ry))
    return Box.of_points(pts)


def field_box(text: str, x: float, y: float, field_angle: float, style, sym_rot: float = 0.0, sym_mirror: str = "") -> Box:
    s = style.size_h / REF
    w = advance(text, style.size_w, style.bold)
    local = _local_box(w, style.hjust, _V_FIELD[style.vjust], s, len(lines(text)), vjust=style.vjust)
    return _place(local, x, y, field_angle + sym_rot, sym_mirror)


def _hv(angle: float) -> float:
    """Text and labels only have horizontal or vertical reading, the side comes from justify."""
    return 90.0 if round(angle) % 180 == 90 else 0.0


def text_box(text: str, x: float, y: float, angle: float, style) -> Box:
    s = style.size_h / REF
    w = advance(text, style.size_w, style.bold)
    local = _local_box(w, style.hjust, _V_TEXT[style.vjust], s, len(lines(text)), vjust=style.vjust)
    return _place(local, x, y, _hv(angle))


def _label_hjust(label) -> str:
    """Side of the label text. Uses the file justify, falls back to the angle."""
    if label.style.hjust in ("left", "right"):
        return label.style.hjust
    return "right" if round(label.angle) in (180, 270) else "left"


def label_box(label) -> Box:
    """Text plus decoration (flag or outline) of a label."""
    st = label.style
    s = st.size_h / REF
    w = advance(label.text, st.size_w, st.bold)
    hj = _label_hjust(label)
    rot = _hv(label.angle)
    if label.kind == "label":
        local = _local_box(w, hj, _V_LABEL, s, 1, vjust="bottom")
        return _place(local, label.x, label.y, rot)
    if label.kind == "hierarchical_label":
        x0, y0, x1, y1 = _local_box(w, hj, _V_HLABEL, s, 1, start=_HLABEL_OFF * s)
        # the flag sits between anchor and text
        if hj == "left":
            x0 = 0.0
        else:
            x1 = 0.0
        return _place((x0, min(y0, -0.635 * s), x1, max(y1, 0.635 * s)), label.x, label.y, rot)
    if label.kind == "global_label":
        off = _GLABEL_OFF.get(label.shape, 1.402) * s
        x0, y0, x1, y1 = _local_box(w, hj, _V_GLABEL, s, 1, start=off)
        # outline: from the anchor to past the text end, arrow for input/output
        tail = 0.6 * s + (0.95 * s if label.shape in ("output", "bidirectional", "tri_state") else 0.0)
        if hj == "left":
            x0, x1 = 0.0, x1 + tail
        else:
            x0, x1 = x0 - tail, 0.0
        return _place((x0, -0.95 * s, x1, 0.95 * s), label.x, label.y, rot)
    # netclass flag / directive label: small flag above or below the anchor
    return _place((-0.635 * s, -2.54 * s, 0.635 * s, 0.0), label.x, label.y, label.angle)


def sheet_pin_box(pin, sheet_box: Box) -> Box:
    """Sheet pin: flag outside the sheet edge, name inside."""
    st = pin.style
    s = st.size_h / REF
    w = advance(pin.name, st.size_w, st.bold)
    a = round(pin.angle) % 360
    # KiCad: angle 180 = pin on the left edge, text inside to the right
    inward = {180: 0.0, 0: 180.0, 90: 270.0, 270: 90.0}.get(a, 0.0)
    local = (-1.27 * s, -0.95 * s, _HLABEL_OFF * s + w, 0.95 * s)
    return _place(local, pin.x, pin.y, inward)


def pin_text_boxes(pin, show_names: bool, show_numbers: bool, name_offset: float) -> list[tuple[str, str, Box]]:
    """Name and number boxes of one placed pin: [(kind, text, box)]."""
    out = []
    lp = pin.lib
    dx, dy = pin.ex - pin.x, pin.ey - pin.y
    ln = math.hypot(dx, dy)
    if ln < 1e-6:
        ux, uy = 1.0, 0.0
    else:
        ux, uy = dx / ln, dy / ln
    vertical = abs(uy) > 0.5
    name = plain(lp.name) if lp.name != "~" else ""
    ns = lp.name_size / REF
    ms = lp.num_size / REF
    name_w = advance(name, lp.name_size)
    num_w = advance(lp.number, lp.num_size)

    def along_box(cx, cy, w, h_lo, h_hi, start_at_c: bool):
        """Box along the pin direction. Perpendicular extent h_lo..h_hi (screen, before rotation)."""
        if not vertical:
            if start_at_c:
                x0, x1 = (cx, cx + w) if ux > 0 else (cx - w, cx)
            else:
                x0, x1 = cx - w / 2, cx + w / 2
            return Box(x0, cy + h_lo, x1, cy + h_hi)
        if start_at_c:
            y0, y1 = (cy, cy + w) if uy > 0 else (cy - w, cy)
        else:
            y0, y1 = cy - w / 2, cy + w / 2
        return Box(cx + h_lo, y0, cx + h_hi, y1)

    mx, my = (pin.x + pin.ex) / 2, (pin.y + pin.ey) / 2
    if show_names and name and not lp.hidden:
        if name_offset > 0:
            cx, cy = pin.ex + ux * name_offset, pin.ey + uy * name_offset
            out.append(("pin_name", name, along_box(cx, cy, name_w, -0.8 * ns, 0.8 * ns, True)))
        else:
            out.append(("pin_name", name, along_box(mx, my, name_w, -1.75 * ns, -0.25 * ns, False)))
    if show_numbers and lp.number and not lp.hidden:
        if name_offset > 0 or not (show_names and name):
            out.append(("pin_number", lp.number, along_box(mx, my, num_w, -1.75 * ms, -0.25 * ms, False)))
        else:
            out.append(("pin_number", lp.number, along_box(mx, my, num_w, 0.25 * ms, 1.75 * ms, False)))
    return out
