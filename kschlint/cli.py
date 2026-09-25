"""Command line: ``python -m kschlint <command> PROJECT ...``"""

from __future__ import annotations

import argparse
import json
import sys

from . import api


def _floats(s: str) -> list[float]:
    return [float(v) for v in s.replace(" ", "").split(",")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kschlint", description="Readability linter, renderer and text fixer for KiCad schematics.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, sheet_required=False):
        p.add_argument("project", help=".kicad_pro, root .kicad_sch or project directory")
        p.add_argument("--sheet", "-s", required=sheet_required, help="page: /Power/, Power, HighSideSwitch12V_PumpMain or Power.kicad_sch")
        p.add_argument("--json", action="store_true", help="JSON output")

    p = sub.add_parser("lint", help="find collisions and readability problems")
    common(p)
    p.add_argument("--severity", default="warning", choices=["error", "warning", "info"], help="lowest severity to report")
    p.add_argument("--codes", help="comma separated check codes")
    p.add_argument("--clearance", type=float, default=0.25)
    p.add_argument("--limit", type=int, default=0)

    p = sub.add_parser("render", help="PNG of one page, findings drawn on top")
    common(p, True)
    p.add_argument("--out", "-o")
    p.add_argument("--region", help="x0,y0,x1,y1 in mm")
    p.add_argument("--around", help="zoom on references, e.g. U101,R101")
    p.add_argument("--margin", type=float, default=15.0)
    p.add_argument("--tiles", action="store_true", help="split into readable tiles")
    p.add_argument("--no-findings", action="store_true")
    p.add_argument("--severity", default="warning", choices=["error", "warning", "info"])
    p.add_argument("--boxes", action="store_true", help="draw every computed item box (debug)")
    p.add_argument("--grid", action="store_true", help="draw the 2.54 mm grid")

    p = sub.add_parser("fix", help="move fields and labels out of collisions (dry run unless --write)")
    common(p)
    p.add_argument("--write", action="store_true")
    p.add_argument("--no-labels", action="store_true", help="only move symbol fields")
    p.add_argument("--horizontal", action="store_true", help="also turn vertical Reference/Value text horizontal")
    p.add_argument("--no-verify", action="store_true", help="skip the netlist check (not recommended)")
    p.add_argument("--clearance", type=float, default=0.25)

    p = sub.add_parser("inspect", help="exact pin tips, body and field boxes of symbols")
    common(p, True)
    p.add_argument("--refs", help="comma separated references")

    p = sub.add_parser("free", help="find free space for a w x h block")
    common(p, True)
    p.add_argument("--size", required=True, help="w,h in mm")
    p.add_argument("--near", help="x,y in mm")
    p.add_argument("--count", type=int, default=3)

    p = sub.add_parser("selftest", help="compare text geometry with kicad-cli PDF output")
    p.add_argument("project")

    p = sub.add_parser("pcb-lint", help="KiCad DRC summary of a board (silkscreen first)")
    p.add_argument("board")
    p.add_argument("--all", action="store_true", help="all DRC findings, not only silkscreen")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("pcb-render", help="PNG of a board region (F.Cu, silkscreen, courtyard, edge) with silkscreen findings")
    p.add_argument("board")
    p.add_argument("--out", "-o")
    p.add_argument("--region", help="x0,y0,x1,y1 in mm")
    p.add_argument("--around", help="references to zoom on")
    p.add_argument("--layers", default="F.Cu,F.Silkscreen,F.Courtyard,Edge.Cuts")

    p = sub.add_parser("pcb-fix", help="move reference designators off pads, silkscreen, other parts and the board edge")
    p.add_argument("board")
    p.add_argument("--write", action="store_true")
    p.add_argument("--min-size", type=float, default=0.0, help="allow smaller reference text (mm) when nothing fits")
    p.add_argument("--refs", help="only these references")

    sub.add_parser("checks", help="list check codes")
    sub.add_parser("mcp", help="run the MCP server on stdio")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "lint":
            res = api.lint(a.project, a.sheet, a.severity, a.codes.split(",") if a.codes else None, a.clearance)
            print(json.dumps(res, indent=1) if a.json else api.format_findings(res, a.limit))
            return 1 if res["counts"].get("error") else 0
        if a.cmd == "render":
            res = api.render(
                a.project,
                a.sheet,
                a.out,
                _floats(a.region) if a.region else None,
                a.around,
                a.margin,
                not a.no_findings,
                a.severity,
                a.boxes,
                a.tiles,
                a.grid,
            )
            if a.json:
                print(json.dumps(res, indent=1))
            else:
                for im in res["images"]:
                    print(f"{im['png']}  region {im['region']}  {im['px_per_mm']} px/mm")
                print(f"{res['findings_drawn']} findings drawn")
            return 0
        if a.cmd == "fix":
            res = api.fix(a.project, a.sheet, a.write, not a.no_labels, a.horizontal, not a.no_verify, a.clearance)
            if a.json:
                print(json.dumps(res, indent=1))
            else:
                for m in res["moves"]:
                    print(f"{m['file']}: {m['what']} -> ({m['to'][0]:g}, {m['to'][1]:g}) angle {m['to'][2]:g} {m['justify']}")
                for u in res["unresolved"]:
                    print(f"unresolved: {u}")
                if "error" in res:
                    print("ERROR:", res["error"])
                    for d in res.get("netlist_diff", []):
                        print("  ", d)
                    return 2
                if res["written"]:
                    print(f"written. netlist {res.get('netlist', 'not checked')}. findings {res['findings_before']} -> {res['findings_after']}")
                else:
                    print(res.get("note", ""))
            return 0
        if a.cmd == "inspect":
            print(json.dumps(api.inspect(a.project, a.sheet, a.refs.split(",") if a.refs else None), indent=1))
            return 0
        if a.cmd == "free":
            w, h = _floats(a.size)
            print(json.dumps(api.free_space(a.project, a.sheet, w, h, _floats(a.near) if a.near else None, count=a.count), indent=1))
            return 0
        if a.cmd == "selftest":
            print(json.dumps(api.selftest(a.project), indent=1))
            return 0
        if a.cmd in ("pcb-lint", "pcb-render", "pcb-fix"):
            from . import pcb

            if a.cmd == "pcb-lint":
                res = pcb.lint(a.board, silk_only=not a.all)
                if a.json:
                    print(json.dumps(res, indent=1))
                else:
                    for f in res["findings"]:
                        print(f"#{f['n']} {f['type']} @({f['at'][0]:g}, {f['at'][1]:g}): {' / '.join(f['items'])}")
                    print(f"counts {res['counts']}  unconnected {res['unconnected']}  parity {res['parity']}")
                return 1 if any(f["type"] != "lib_footprint_issues" for f in res["findings"]) else 0
            if a.cmd == "pcb-render":
                res = pcb.render(a.board, a.out, _floats(a.region) if a.region else None, a.around, layers=a.layers)
                print(f"{res['png']}  region {res['region']}  {res['findings_drawn']} findings drawn")
                return 0
            res = pcb.fix(a.board, a.write, a.min_size, a.refs.split(",") if a.refs else None)
            for m in res.get("moves", []):
                print(f"{m['ref']}: ({m['from'][0]:g}, {m['from'][1]:g}) -> ({m['to'][0]:g}, {m['to'][1]:g}) angle {m['to'][2]:g} size {m['to'][3]:g}")
            for u in res.get("unresolved", []):
                print(f"unresolved: {u['ref']}")
            if "error" in res:
                print("ERROR:", res["error"])
                return 2
            print(f"written. silkscreen findings {res['silk_findings'][0]} -> {res['silk_findings'][1]}" if res.get("written") else "dry run. Pass --write to apply")
            return 0
        if a.cmd == "checks":
            for k, v in api.list_checks().items():
                print(f"{k:24s} {v['severity']:8s} {v['what']}")
            return 0
        if a.cmd == "mcp":
            from .mcp_server import serve

            serve()
            return 0
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
