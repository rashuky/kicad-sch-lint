"""PCB reference fixer on a board built by KiCad's own python: references piled on pads and on each other."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kschlint import pcb  # noqa: E402

BUILD = textwrap.dedent(
    r"""
    import sys, pcbnew
    out, fpdir = sys.argv[1], sys.argv[2]
    MM = pcbnew.FromMM
    b = pcbnew.BOARD()
    for i, (x0, y0, x1, y1) in enumerate([(0, 0, 40, 0), (40, 0, 40, 30), (40, 30, 0, 30), (0, 30, 0, 0)]):
        s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pcbnew.VECTOR2I(MM(x0), MM(y0))); s.SetEnd(pcbnew.VECTOR2I(MM(x1), MM(y1)))
        s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(MM(0.1)); b.Add(s)
    for n, (x, y) in enumerate([(15, 15), (19, 15), (23, 15), (38.5, 5)], 1):
        fp = pcbnew.FootprintLoad(fpdir + "/Resistor_SMD.pretty", "R_0603_1608Metric")
        fp.SetReference(f"R{n}"); fp.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
        b.Add(fp)
        # pile every reference onto the middle part
        fp.Reference().SetPosition(pcbnew.VECTOR2I(MM(19), MM(15)))
    # R4 reference sticks out of the board
    b.FindFootprintByReference("R4").Reference().SetPosition(pcbnew.VECTOR2I(MM(40.5), MM(5)))
    b.Save(out)
    """
)


class PcbFixTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.board = os.path.join(self.td, "t.kicad_pcb")
        script = os.path.join(self.td, "build.py")
        with open(script, "w") as fh:
            fh.write(BUILD)
        fpdir = os.path.join(os.path.dirname(os.path.dirname(pcb.kicad.kicad_cli())), "share", "kicad", "footprints")
        r = subprocess.run([pcb.kicad_python(), script, self.board, fpdir], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def positions(self):
        return {r: (b.x0, b.y0) for r, b in pcb.footprint_boxes(self.board).items()}

    def test_fix(self):
        before = pcb.lint(self.board, silk_only=True)
        self.assertGreater(len(before["findings"]), 0)
        pos0 = self.positions()
        res = pcb.fix(self.board, write=True)
        self.assertTrue(res["written"], res)
        self.assertEqual(res["unresolved"], [])
        after = pcb.lint(self.board, silk_only=True)
        self.assertEqual(after["findings"], [], after)
        self.assertEqual(self.positions(), pos0)  # footprints never move
        self.assertIn("R4", [m["ref"] for m in res["moves"]])  # off-board reference caught


if __name__ == "__main__":
    unittest.main()
