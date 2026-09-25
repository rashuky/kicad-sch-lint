"""Minimal MCP server (stdio, JSON-RPC 2.0, newline delimited). No dependencies.

Register in .mcp.json:
    {"mcpServers": {"kschlint": {"command": "python", "args": ["-m", "kschlint", "mcp"]}}}
"""

from __future__ import annotations

import base64
import json
import sys
import traceback

from . import __version__, api

PROJECT = {"type": "string", "description": "Path to .kicad_pro, root .kicad_sch or the project directory"}
SHEET = {"type": "string", "description": "Page: name path (/Power/), sheet or instance name (HighSideSwitch12V_PumpMain) or file name"}

TOOLS = [
    {
        "name": "sch_lint",
        "description": (
            "Find layout problems in a KiCad schematic: overlapping texts, text on symbols or wires, wires through bodies, "
            "floating labels, wire ends on pin lines, missing junctions, dangling wires, off-grid points, title block overlap. "
            "Run after every schematic edit. Coordinates are mm, y down."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": PROJECT,
                "sheet": SHEET,
                "severity": {"type": "string", "enum": ["error", "warning", "info"], "default": "warning"},
                "codes": {"type": "array", "items": {"type": "string"}, "description": "Only these check codes (see sch_checks)"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "sch_render",
        "description": (
            "Render one page to PNG (KiCad's own plot) and return it as an image, with findings boxed and numbered like sch_lint. "
            "Use region or around to zoom so text stays readable. tiles=true splits a full sheet into readable tiles."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": PROJECT,
                "sheet": SHEET,
                "region": {"type": "array", "items": {"type": "number"}, "description": "[x0, y0, x1, y1] mm"},
                "around": {"type": "string", "description": "References to zoom on, comma separated, e.g. U101,R101"},
                "margin": {"type": "number", "default": 15},
                "tiles": {"type": "boolean", "default": False},
                "findings": {"type": "boolean", "default": True},
                "severity": {"type": "string", "enum": ["error", "warning", "info"], "default": "warning"},
                "boxes": {"type": "boolean", "default": False, "description": "Draw every computed item box (debug)"},
                "grid": {"type": "boolean", "default": False},
                "max_images": {"type": "integer", "default": 4},
            },
            "required": ["project", "sheet"],
        },
    },
    {
        "name": "sch_fix",
        "description": (
            "Move symbol fields (reference, value) and local labels out of collisions. Never touches symbols, pins, wires or "
            "connectivity. Dry run by default. With write=true it edits the files, checks the kicad-cli netlist is unchanged "
            "and restores the files if not. KiCad must be closed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": PROJECT,
                "sheet": SHEET,
                "write": {"type": "boolean", "default": False},
                "labels": {"type": "boolean", "default": True},
                "horizontal": {"type": "boolean", "default": False, "description": "Also make vertical reference/value text horizontal"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "sch_inspect",
        "description": (
            "Exact geometry of symbols on a page: pin tip coordinates (where wires must end), pin direction, body box, field boxes. "
            "Without refs it also lists labels and wires. Use before drawing wires so they land on pin tips."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project": PROJECT, "sheet": SHEET, "refs": {"type": "array", "items": {"type": "string"}}},
            "required": ["project", "sheet"],
        },
    },
    {
        "name": "sch_free_space",
        "description": "Find free, grid-aligned rectangles of w x h mm on a page, nearest to a point. Use to place a new block without collisions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": PROJECT,
                "sheet": SHEET,
                "w": {"type": "number"},
                "h": {"type": "number"},
                "near": {"type": "array", "items": {"type": "number"}, "description": "[x, y] mm"},
                "count": {"type": "integer", "default": 3},
            },
            "required": ["project", "sheet", "w", "h"],
        },
    },
    {
        "name": "pcb_lint",
        "description": "KiCad DRC of a .kicad_pcb. Silkscreen findings by default (reference on pads, on other silkscreen), all=true for everything.",
        "inputSchema": {"type": "object", "properties": {"board": {"type": "string"}, "all": {"type": "boolean", "default": False}}, "required": ["board"]},
    },
    {
        "name": "pcb_render",
        "description": "PNG of a board region (F.Cu, silkscreen, courtyards, edge) as an image, silkscreen findings boxed. Use around (references) or region [x0,y0,x1,y1] mm.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "board": {"type": "string"},
                "around": {"type": "string"},
                "region": {"type": "array", "items": {"type": "number"}},
                "layers": {"type": "string", "default": "F.Cu,F.Silkscreen,F.Courtyard,Edge.Cuts"},
            },
            "required": ["board"],
        },
    },
    {
        "name": "pcb_fix",
        "description": (
            "Move reference designators flagged by DRC (and ones past the board edge) to the nearest clean spot next to their own part. "
            "Never moves footprints. With write=true it edits the board, re-runs DRC and restores the file if any non-silkscreen result changes. KiCad must be closed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"board": {"type": "string"}, "write": {"type": "boolean", "default": False}, "min_size": {"type": "number", "default": 0}},
            "required": ["board"],
        },
    },
    {"name": "sch_checks", "description": "List lint check codes with severity and meaning.", "inputSchema": {"type": "object", "properties": {}}},
    {
        "name": "sch_selftest",
        "description": "Compare the text geometry model with kicad-cli PDF output for a project. Run once after a KiCad update.",
        "inputSchema": {"type": "object", "properties": {"project": PROJECT}, "required": ["project"]},
    },
]


def _text(obj) -> dict:
    return {"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, indent=1)}


def call_tool(name: str, a: dict) -> list[dict]:
    if name == "sch_lint":
        res = api.lint(a["project"], a.get("sheet"), a.get("severity", "warning"), a.get("codes"))
        return [_text(api.format_findings(res)), _text(res)]
    if name == "sch_render":
        res = api.render(
            a["project"],
            a["sheet"],
            None,
            a.get("region"),
            a.get("around"),
            a.get("margin", 15.0),
            a.get("findings", True),
            a.get("severity", "warning"),
            a.get("boxes", False),
            a.get("tiles", False),
            a.get("grid", False),
        )
        out = [_text(res)]
        for im in res["images"][: a.get("max_images", 4)]:
            with open(im["png"], "rb") as fh:
                out.append({"type": "image", "data": base64.b64encode(fh.read()).decode(), "mimeType": "image/png"})
        return out
    if name == "sch_fix":
        return [_text(api.fix(a["project"], a.get("sheet"), a.get("write", False), a.get("labels", True), a.get("horizontal", False)))]
    if name == "sch_inspect":
        return [_text(api.inspect(a["project"], a["sheet"], a.get("refs")))]
    if name == "sch_free_space":
        return [_text(api.free_space(a["project"], a["sheet"], a["w"], a["h"], a.get("near"), count=a.get("count", 3)))]
    if name in ("pcb_lint", "pcb_render", "pcb_fix"):
        from . import pcb

        if name == "pcb_lint":
            return [_text(pcb.lint(a["board"], silk_only=not a.get("all", False)))]
        if name == "pcb_render":
            res = pcb.render(a["board"], None, a.get("region"), a.get("around"), layers=a.get("layers", "F.Cu,F.Silkscreen,F.Courtyard,Edge.Cuts"))
            with open(res["png"], "rb") as fh:
                return [_text(res), {"type": "image", "data": base64.b64encode(fh.read()).decode(), "mimeType": "image/png"}]
        return [_text(pcb.fix(a["board"], a.get("write", False), a.get("min_size", 0.0)))]
    if name == "sch_checks":
        return [_text(api.list_checks())]
    if name == "sch_selftest":
        return [_text(api.selftest(a["project"]))]
    raise ValueError(f"unknown tool {name}")


def handle(msg: dict) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}
    if mid is None:
        return None  # notification
    try:
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "kschlint", "version": __version__},
                "instructions": "KiCad schematic readability tools. Workflow: edit, sch_lint, sch_fix (write), sch_render to look, repeat.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                content = call_tool(params.get("name", ""), params.get("arguments") or {})
                result = {"content": content, "isError": False}
            except Exception as e:  # tool errors go back to the model, not as protocol errors
                result = {"content": [_text(f"{type(e).__name__}: {e}")], "isError": True}
                traceback.print_exc(file=sys.stderr)
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:  # pragma: no cover
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def serve() -> None:
    inp = sys.stdin.buffer
    out = sys.stdout.buffer
    while True:
        line = inp.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            resp = handle(m)
            if resp is not None:
                out.write(json.dumps(resp).encode("utf-8") + b"\n")
                out.flush()
