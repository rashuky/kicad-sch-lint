"""Build small test schematics from KiCad stock symbols.

KiCad itself (kicad-cli) is the oracle: tests generate a schematic from our
model's idea of the geometry, then check KiCad's netlist or PDF agrees.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid as _uuid

from kschlint import sexpr
from kschlint.kicad import kicad_cli, stock_symbol_dir


def uid() -> str:
    return str(_uuid.uuid4())


def stock_symbol(lib: str, name: str) -> str:
    """Return the lib symbol s-expression as it appears in a schematic cache."""
    path = os.path.join(stock_symbol_dir(), f"{lib}.kicad_sym")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    root = sexpr.parse(text)
    for s in root.children("symbol"):
        if s.arg(0) == name:
            body = text[s.start : s.end]
            # only the top-level name gets the library prefix
            return body.replace(f'(symbol "{name}"', f'(symbol "{lib}:{name}"', 1)
    raise KeyError(f"{lib}:{name}")


def effects(size=1.27, justify: str = "", hide: bool = False) -> str:
    j = f" (justify {justify})" if justify else ""
    return f"(effects (font (size {size} {size})){j}){' (hide yes)' if hide else ''}"


def prop(name: str, value: str, x: float, y: float, a: float = 0, justify: str = "", hide: bool = False) -> str:
    j = f" (justify {justify})" if justify else ""
    h = " (hide yes)" if hide else ""
    return f'(property {sexpr.quote(name)} {sexpr.quote(value)} (at {x} {y} {a}){h} (effects (font (size 1.27 1.27)){j}))'


def symbol(lib_id, ref, value, x, y, rot=0, mirror="", root_uuid="", fields_extra="", unit=1, ref_at=None, val_at=None) -> str:
    m = f" (mirror {mirror})" if mirror else ""
    ra = ref_at or (x, y - 5.08, 0, "")
    va = val_at or (x, y + 5.08, 0, "")
    return (
        f'(symbol (lib_id "{lib_id}") (at {x} {y} {rot}){m} (unit {unit}) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no) (uuid "{uid()}")\n'
        f"  {prop('Reference', ref, ra[0], ra[1], ra[2], ra[3])}\n"
        f"  {prop('Value', value, va[0], va[1], va[2], va[3])}\n"
        f"  {prop('Footprint', '', x, y, 0, '', True)}\n"
        f"  {fields_extra}\n"
        f'  (instances (project "t" (path "/{root_uuid}" (reference "{ref}") (unit {unit})))))'
    )


def label(text, x, y, a=0, justify="left bottom") -> str:
    return f'(label {sexpr.quote(text)} (at {x} {y} {a}) {effects(justify=justify)} (uuid "{uid()}"))'


def wire(a, b) -> str:
    return f'(wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]})) (stroke (width 0) (type default)) (uuid "{uid()}"))'


def text(t, x, y, a=0, justify="left bottom") -> str:
    return f'(text {sexpr.quote(t)} (exclude_from_sim no) (at {x} {y} {a}) {effects(justify=justify)} (uuid "{uid()}"))'


def schematic(lib_symbols: list[str], items: list[str], root_uuid: str, paper: str = "A3") -> str:
    body = "\n".join(items)
    libs = "\n".join(lib_symbols)
    txt = (
        f'(kicad_sch (version 20260306) (generator "eeschema") (generator_version "10.0") (uuid "{root_uuid}") (paper "{paper}")\n'
        f"(lib_symbols\n{libs}\n)\n{body}\n"
        f'(sheet_instances (path "/" (page "1")))\n(embedded_fonts no)\n)\n'
    )
    return txt.replace("\r\n", "\n").replace("\n", "\r\n")


def write_project(dirpath: str, name: str, sch_text: str) -> str:
    os.makedirs(dirpath, exist_ok=True)
    p = os.path.join(dirpath, f"{name}.kicad_sch")
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(sch_text)
    with open(os.path.join(dirpath, f"{name}.kicad_pro"), "w", encoding="utf-8") as fh:
        fh.write('{"meta": {"filename": "%s.kicad_pro", "version": 3}}' % name)
    return p


def netlist_nets(sch_path: str, out_dir: str) -> dict:
    """{(ref, pin): net name} from kicad-cli."""
    import xml.etree.ElementTree as ET

    out = os.path.join(out_dir, "net.xml")
    subprocess.run([kicad_cli(), "sch", "export", "netlist", "--format", "kicadxml", "-o", out, sch_path], check=True, capture_output=True)
    res = {}
    for net in ET.parse(out).getroot().find("nets"):
        for n in net.findall("node"):
            res[(n.get("ref"), n.get("pin"))] = net.get("name")
    return res
