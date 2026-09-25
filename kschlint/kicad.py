"""Thin wrappers around kicad-cli."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET


def kicad_cli() -> str:
    env = os.environ.get("KICAD_CLI")
    if env:
        return env
    p = shutil.which("kicad-cli")
    if p:
        return p
    for cand in sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\kicad-cli.exe"), reverse=True):
        return cand
    for cand in ("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli", "/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"):
        if os.path.exists(cand):
            return cand
    raise FileNotFoundError("kicad-cli not found. Put it on PATH or set KICAD_CLI.")


def stock_symbol_dir() -> str:
    env = os.environ.get("KICAD_SYMBOL_DIR") or next((v for k, v in os.environ.items() if k.startswith("KICAD") and k.endswith("_SYMBOL_DIR")), None)
    if env and os.path.isdir(env):
        return env
    cli = kicad_cli()
    base = os.path.dirname(os.path.dirname(cli))
    for cand in (os.path.join(base, "share", "kicad", "symbols"), os.path.join(base, "SharedSupport", "symbols"), "/usr/share/kicad/symbols"):
        if os.path.isdir(cand):
            return cand
    raise FileNotFoundError("KiCad stock symbol directory not found")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([kicad_cli(), *args], capture_output=True, text=True)


def export_pdf(sch: str, out: str, pages: str = "") -> str:
    args = ["sch", "export", "pdf", "-o", out, "--exclude-pdf-property-popups"]
    if pages:
        args += ["--pages", pages]
    r = run(args + [sch])
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"kicad-cli pdf export failed: {r.stderr or r.stdout}")
    return out


def netlist_signature(sch: str) -> list:
    """Connectivity only: sorted nets as sorted (ref, pin) tuples, plus net names.

    Two schematics with the same signature are electrically identical.
    """
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "n.xml")
        r = run(["sch", "export", "netlist", "--format", "kicadxml", "-o", out, sch])
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(f"kicad-cli netlist export failed: {r.stderr or r.stdout}")
        root = ET.parse(out).getroot()
    nets = []
    for net in root.find("nets"):
        nodes = tuple(sorted((n.get("ref"), n.get("pin")) for n in net.findall("node")))
        nets.append((net.get("name"), nodes))
    return sorted(nets)


def diff_signatures(a: list, b: list) -> list[str]:
    sa, sb = set(a), set(b)
    out = []
    for name, nodes in sorted(sa - sb):
        out.append(f"- {name}: {' '.join(f'{r}.{p}' for r, p in nodes)}")
    for name, nodes in sorted(sb - sa):
        out.append(f"+ {name}: {' '.join(f'{r}.{p}' for r, p in nodes)}")
    return out


def erc(sch: str) -> dict:
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "erc.json")
        run(["sch", "erc", "--format", "json", "-o", out, sch])
        import json

        with open(out, encoding="utf-8") as fh:
            return json.load(fh)
