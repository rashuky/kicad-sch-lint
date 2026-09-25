"""Text boxes against kicad-cli PDF: fields in every symbol orientation, labels, text."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixtures as fx  # noqa: E402
from kschlint.kicad import export_pdf  # noqa: E402
from kschlint.model import load_project  # noqa: E402
from kschlint.pdftruth import compare  # noqa: E402


def js(h, v):
    return " ".join(x for x in (h, v) if x)


class TextBoxTest(unittest.TestCase):
    def test_against_pdf(self):
        root = fx.uid()
        items = []
        combos = [(a, h, v) for a in (0, 90) for h in ("left", "", "right") for v in ("top", "", "bottom")]
        n = 0
        for oi, (rot, mir) in enumerate([(r, m) for r in (0, 90, 180, 270) for m in ("", "x", "y")]):
            for ci in range(0, 18, 3):
                a, h, v = combos[ci]
                a2, h2, v2 = combos[(ci + 10) % 18]
                n += 1
                x, y = 20.32 + ci * 20.32, 20.32 + oi * 30.48
                items.append(fx.symbol("Device:R", f"R{n}", f"V{n}x", x, y, rot, mir, root, ref_at=(x + 2.54, y - 5.08, a, js(h, v)), val_at=(x - 2.54, y + 7.62, a2, js(h2, v2))))
        k = 0
        for ang in (0, 90, 180, 270):
            for hj in ("left", "right"):
                k += 1
                items.append(fx.label(f"NET_{k}", 400 + k * 2.54 * 0, 30 + k * 12.7, ang, f"{hj} bottom"))
                items.append(fx.text(f"Note {k} (ok)", 500, 30 + k * 12.7, ang, f"{hj} bottom"))
        with tempfile.TemporaryDirectory() as td:
            path = fx.write_project(td, "t", fx.schematic([fx.stock_symbol("Device", "R")], items, root, paper="A1"))
            pdf = export_pdf(path, os.path.join(td, "t.pdf"))
            r = compare(load_project(path), pdf)
        self.assertEqual(r["missing"], [])
        self.assertGreater(r["checked"], 150)
        worst = r["results"][0]
        self.assertLess(worst[0], 0.5, f"worst text box error {worst}")


if __name__ == "__main__":
    unittest.main()
