# kicad-sch-lint

Readability linter, renderer and text fixer for KiCad 10 schematics. Built for AI
assistants that edit `.kicad_sch` files as text and cannot see the result.

It answers three questions after every edit:

1. **What collides?** `lint` computes the real geometry (symbol transforms, pin tips,
   text extents in KiCad's stroke font) and reports overlaps, wires through bodies,
   floating labels, wire ends on pin lines, missing junctions, off-grid points.
2. **What does it look like?** `render` gives a PNG from KiCad's own plot, with the
   findings boxed and numbered. Crops and tiles keep 1.27 mm text readable.
3. **Can it be fixed safely?** `fix` moves fields and local labels to free spots. It
   never touches symbols, pins or wires, and it rolls back if the kicad-cli netlist
   changes.

Plus two helpers for building schematics: `inspect` (exact pin tip coordinates, so wires
land on pins) and `free` (empty space for a new block).

## Requirements

- Python 3.10+, no packages for lint, fix and inspect
- `kicad-cli` (KiCad 10) on PATH or in `KICAD_CLI`, for netlist checks and rendering
- `pymupdf` for `render` and `selftest` (`pip install pymupdf`)

## Use

```sh
python -m kschlint lint     PROJECT [--sheet Power] [--severity info] [--json]
python -m kschlint render   PROJECT --sheet Power [--around U101,R101 | --region x0,y0,x1,y1 | --tiles] [--boxes]
python -m kschlint fix      PROJECT [--sheet Power] [--write] [--horizontal] [--no-labels]
python -m kschlint inspect  PROJECT --sheet Power [--refs U101,C115]
python -m kschlint free     PROJECT --sheet Power --size 40,25 [--near 100,80]
python -m kschlint selftest PROJECT
python -m kschlint checks
python -m kschlint mcp
```

`PROJECT` is a `.kicad_pro`, the root `.kicad_sch` or the project directory.
`--sheet` takes a name path (`/Actuators/HighSideSwitch12V_Fan/`), a sheet or instance
name (`HighSideSwitch12V_Fan`) or a file name (`HighSideSwitch12V.kicad_sch`).
Coordinates are mm, y down, like the file.

`lint` exits 1 when there are errors, so it works in CI.

## MCP server

```json
{"mcpServers": {"kschlint": {"command": "python", "args": ["-m", "kschlint", "mcp"],
                              "env": {"PYTHONPATH": "C:/path/to/kicad-sch-lint"}}}}
```

Tools: `sch_lint`, `sch_render` (returns the PNG as an image), `sch_fix`, `sch_inspect`,
`sch_free_space`, `sch_checks`, `sch_selftest`. The server is a plain stdio JSON-RPC
loop, no `mcp` package needed.

## Checks

| Code | Severity | Meaning |
|---|---|---|
| `text-overlap` | error | two texts overlap (fields, labels, pin names and numbers, notes, sheet pins) |
| `text-over-body` | error | text on another symbol's body or a sheet |
| `text-over-wire` | error | a wire or a pin line runs through text |
| `body-overlap` | error | two symbol bodies overlap |
| `wire-through-body` | error | wire crosses a symbol body |
| `wire-overlap` | error | collinear wires overlap |
| `label-floating` | error | label anchor touches no wire or pin |
| `wire-end-on-pin-line` | error | wire ends on a pin line but not on the tip. KiCad does not connect it |
| `field-on-own-body` | warning | reference or value drawn across its own outline |
| `pin-on-wire-middle` | warning | pin tip touches the middle of a wire (connects, often unintended) |
| `missing-junction` | warning | T connection without a dot |
| `four-way-junction` | warning | four wires meet at one point |
| `dangling-wire` | warning | wire end connected to nothing |
| `off-grid` | warning | pin tip, wire end or label off the 1.27 mm grid |
| `outside-frame` | warning | item outside the frame or on the title block |
| `text-too-close` | info | text within the clearance (0.25 mm) of another item |
| `vertical-field` | info | vertical reference or value |
| `power-symbol-rotated` | info | GND not pointing down, supply not pointing up |
| `unneeded-junction` | info | dot where only two items meet |

Findings of reused sheets are reported once, with the list of instances.

## What `fix` changes

- **Symbol fields.** Visible fields of a colliding symbol are re-placed as a horizontal
  stack (reference above value) on the best side of the symbol. Candidates: right, top,
  left, bottom, shifted along the side. Sides without pins come first. Each candidate is
  scored against every text, body, wire and pin line on the page, the frame and the title
  block. The widest reference over all sheet instances is used. A spot is rejected
  when the text would sit nearer another part than its own, closer than 0.4 mm to
  another text, or (power symbols) away from its arrow or along an unrelated wire, where
  it would read as a net name. Power symbol text only moves to a fully clean spot. `fields_autoplaced` is
  removed from moved symbols so KiCad does not undo the placement.
- **Local labels.** Slide along the wire segment they sit on (1.27 mm steps) or flip
  side. Still on the same wire, so the same net.
- Nothing else. Hierarchical and global labels, wires, symbols stay put. Problems that
  need geometry changes are reported as `unresolved`.

With `--write` it edits in place, compares the kicad-cli netlist (nets and nodes)
before and after, and restores the files on any difference. KiCad must be closed.

## How the geometry was verified

KiCad is the oracle, the numbers are not guessed:

- **Pin tips:** `tests/test_transform.py` places three stock symbols in all 12
  rotation and mirror combinations, puts a label where the model says each pin tip is,
  and checks KiCad's netlist puts every pin on its own label's net. KiCad rotates
  first, then mirrors in screen space.
- **Text:** character advances and per-kind offsets were measured from the PDF text
  layer of kicad-cli exports. `tests/test_textbox.py` checks fields in every symbol
  orientation, labels and notes against a fresh PDF. Error < 0.5 mm, typically 0.1 mm.
- `selftest` runs the same comparison on your own project. Run it after a KiCad update.

Limits: stroke font only (custom TrueType fonts are measured as stroke font),
global label outlines are approximate, text inside symbol graphics is ignored.

## Workflow for AI edits

1. `inspect` the symbols you will wire. Use the pin tips it prints.
2. Edit the file. Use `free` to find room for a new block.
3. `lint`. Fix geometry errors yourself (wires, symbol positions).
4. `fix --write` for text collisions.
5. `render --around <refs>` and look at the image.
6. Repeat until `lint` is clean. Then the usual netlist and ERC diff.

## Tests

```sh
python -m unittest discover -s tests -v
```

Needs kicad-cli and pymupdf. The tests build their own schematics from KiCad stock
symbols.
