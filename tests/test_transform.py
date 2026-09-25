"""Pin transform against KiCad: all 12 rotation/mirror combinations.

A label is placed where our model says each pin ends. KiCad's netlist must
put every pin on the net of its own label.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixtures as fx  # noqa: E402
from kschlint.model import parse_schematic  # noqa: E402

CASES = [
    ("Transistor_FET", "FDC6330L"),
    ("Amplifier_Operational", "LM2904"),
    ("Device", "Q_NPN"),
]


class TransformTest(unittest.TestCase):
    def test_all_orientations(self):
        root = fx.uid()
        libs = [fx.stock_symbol(lib, name) for lib, name in CASES]
        placed = []
        n = 0
        for ci, (lib, name) in enumerate(CASES):
            for ri, rot in enumerate((0, 90, 180, 270)):
                for mi, mir in enumerate(("", "x", "y")):
                    n += 1
                    x = 25.4 + 25.4 * (ri * 3 + mi)
                    y = 30.48 + 60.96 * ci
                    placed.append(fx.symbol(f"{lib}:{name}", f"U{n}", name, x, y, rot, mir, root))
        text = fx.schematic(libs, placed, root)
        sch = parse_schematic(text)
        labels = []
        for p in sch.pins():
            ref = p.symbol.field("Reference").value
            labels.append(fx.label(f"{ref}_P{p.number}", p.x, p.y))
        text = fx.schematic(libs, placed + labels, root)
        with tempfile.TemporaryDirectory() as td:
            path = fx.write_project(td, "t", text)
            nets = fx.netlist_nets(path, td)
        at = {}
        for p in sch.pins():
            ref = p.symbol.field("Reference").value
            at.setdefault((ref, round(p.x, 3), round(p.y, 3)), set()).add(f"{ref}_P{p.number}")
        checked = 0
        for p in sch.pins():
            ref = p.symbol.field("Reference").value
            key = (ref, p.number)
            if key not in nets:
                continue
            checked += 1
            ok = nets[key].lstrip("/") in at[(ref, round(p.x, 3), round(p.y, 3))]
            self.assertTrue(ok, f"{key}: rot {p.symbol.rot} mirror {p.symbol.mirror!r} got {nets[key]}")
        self.assertGreater(checked, 100)


if __name__ == "__main__":
    unittest.main()
