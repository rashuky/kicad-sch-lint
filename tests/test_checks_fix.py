"""Checks find planted problems, the fixer removes text collisions without changing the netlist."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixtures as fx  # noqa: E402
from kschlint import api, sexpr  # noqa: E402
from kschlint.kicad import netlist_signature  # noqa: E402
from kschlint.model import load_project, parse_schematic  # noqa: E402


def build_messy(td: str) -> str:
    """Two resistors in a row with piled up fields, a wire through a body, a floating label, a dangling wire."""
    root = fx.uid()
    libs = [fx.stock_symbol("Device", "R"), fx.stock_symbol("Device", "C")]
    items = [
        # R1 vertical at x=50.8, pins at y=46.99 and 54.61. Value sits inside the body (stock position)
        fx.symbol("Device:R", "R1", "10k", 50.8, 50.8, 0, "", root, ref_at=(50.8, 50.8, 90, ""), val_at=(50.8, 50.8, 90, "")),
        # R2 right next to it, reference on top of R1's pin wire
        fx.symbol("Device:R", "R2", "4k7", 60.96, 50.8, 0, "", root, ref_at=(50.8, 44.45, 0, ""), val_at=(60.96, 50.8, 90, ""), autoplaced=True),
        fx.symbol("Device:C", "C1", "100n", 81.28, 50.8, 0, "", root),
        fx.wire((50.8, 46.99), (50.8, 38.1)),
        fx.wire((50.8, 38.1), (60.96, 38.1)),
        fx.wire((60.96, 38.1), (60.96, 46.99)),
        fx.wire((50.8, 54.61), (50.8, 63.5)),
        fx.label("BOTTOM", 50.8, 63.5, 0),
        # wire straight through C1's body
        fx.wire((73.66, 50.8), (88.9, 50.8)),
        # floating label
        fx.label("NOWHERE", 101.6, 76.2, 0),
        # dangling wire
        fx.wire((60.96, 54.61), (60.96, 66.04)),
    ]
    return fx.write_project(td, "m", fx.schematic(libs, items, root, paper="A4"))


class ChecksFixTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.path = build_messy(self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def codes(self, severity="info"):
        return {f["code"] for f in api.lint(self.path, severity=severity)["findings"]}

    def test_checks_find_planted_problems(self):
        c = self.codes()
        for code in ("text-overlap", "field-on-own-body", "text-over-wire", "wire-through-body", "label-floating", "dangling-wire"):
            self.assertIn(code, c)

    def test_fix_is_safe_and_effective(self):
        before_sig = netlist_signature(self.path)
        with open(self.path, encoding="utf-8", newline="") as fh:
            before_txt = fh.read()
        res = api.fix(self.path, write=True)
        self.assertTrue(res["written"], res)
        self.assertEqual(res.get("netlist"), "unchanged")
        self.assertEqual(netlist_signature(self.path), before_sig)
        with open(self.path, encoding="utf-8", newline="") as fh:
            after_txt = fh.read()
        # still CRLF, still parses, same number of top-level items
        self.assertNotIn("\n", after_txt.replace("\r\n", ""))
        a, b = parse_schematic(before_txt), parse_schematic(after_txt)
        self.assertEqual(len(a.root.children()), len(b.root.children()))
        # symbols, wires and labels untouched
        self.assertEqual([(s.x, s.y, s.rot) for s in a.symbols], [(s.x, s.y, s.rot) for s in b.symbols])
        self.assertEqual([w.pts for w in a.wires], [w.pts for w in b.wires])
        left = self.codes("warning")
        for code in ("text-overlap", "field-on-own-body", "text-over-wire"):
            self.assertNotIn(code, left, api.format_findings(api.lint(self.path)))
        # KiCad must not re-autoplace moved fields
        r2 = next(s for s in b.symbols if s.field("Reference").value == "R2")
        self.assertIsNone(r2.node.child("fields_autoplaced"))
        # problems the fixer must not touch are still reported
        self.assertIn("wire-through-body", left)
        self.assertIn("label-floating", left)

    def test_dry_run_writes_nothing(self):
        with open(self.path, encoding="utf-8", newline="") as fh:
            t0 = fh.read()
        res = api.fix(self.path)
        self.assertFalse(res["written"])
        self.assertTrue(res["moves"])
        with open(self.path, encoding="utf-8", newline="") as fh:
            self.assertEqual(fh.read(), t0)

    def test_inspect_pin_tips(self):
        r = api.inspect(self.path, "/", ["R1"])
        pins = {p["number"]: p["at"] for p in r["symbols"][0]["pins"]}
        self.assertEqual(pins["1"], [50.8, 46.99])
        self.assertEqual(pins["2"], [50.8, 54.61])

    def test_free_space(self):
        r = api.free_space(self.path, "/", 20, 10, [120, 100])
        self.assertTrue(r["free"])


class SexprTest(unittest.TestCase):
    def test_roundtrip_and_patch(self):
        t = '(a (b "x \\"q\\"" 1)\r\n\t(at 1 2 0))'
        n = sexpr.parse(t)
        self.assertEqual(n.child("b").arg(0), 'x "q"')
        at = n.child("at")
        p = sexpr.Patcher(t)
        p.replace(at.start, at.end, "(at 3 4 90)")
        self.assertEqual(p.apply(), '(a (b "x \\"q\\"" 1)\r\n\t(at 3 4 90))')
        self.assertEqual(sexpr.fmt_num(1.2700), "1.27")
        self.assertEqual(sexpr.fmt_num(-0.00001), "0")


class McpTest(unittest.TestCase):
    def test_protocol(self):
        from kschlint.mcp_server import handle

        r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "kschlint")
        self.assertIsNone(handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertIn("sch_lint", names)
        r = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "sch_lint", "arguments": {"project": "does/not/exist.kicad_sch"}}})
        self.assertTrue(r["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
