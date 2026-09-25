"""S-expression parser that keeps source offsets.

Every node remembers where it starts and ends in the original text, so a
fixer can replace one small node (for example an ``(at x y r)``) and leave
the rest of the file byte-identical (CRLF, indentation, ordering).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Atom:
    value: str
    quoted: bool
    start: int
    end: int

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Atom({self.value!r})"


@dataclass
class Node:
    items: list = field(default_factory=list)
    start: int = 0
    end: int = 0

    # ---- access helpers -------------------------------------------------
    @property
    def name(self) -> str:
        if self.items and isinstance(self.items[0], Atom):
            return self.items[0].value
        return ""

    def args(self) -> list:
        """Items after the head atom."""
        return self.items[1:]

    def atoms(self) -> list[str]:
        """Values of the atom arguments (skips child nodes)."""
        return [a.value for a in self.items[1:] if isinstance(a, Atom)]

    def arg(self, i: int = 0, default=None):
        a = self.atoms()
        return a[i] if i < len(a) else default

    def children(self, name: str | None = None) -> list["Node"]:
        return [c for c in self.items if isinstance(c, Node) and (name is None or c.name == name)]

    def child(self, name: str) -> "Node | None":
        for c in self.items:
            if isinstance(c, Node) and c.name == name:
                return c
        return None

    def has_flag(self, flag: str) -> bool:
        """True for a bare atom flag like ``hide`` (KiCad 6 style)."""
        return any(isinstance(a, Atom) and not a.quoted and a.value == flag for a in self.items[1:])

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Node({self.name}, {len(self.items)} items)"


class ParseError(ValueError):
    pass


def parse(text: str) -> Node:
    """Parse one top-level s-expression."""
    stack: list[Node] = []
    root: Node | None = None
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            node = Node(start=i)
            if stack:
                stack[-1].items.append(node)
            stack.append(node)
            i += 1
        elif c == ")":
            if not stack:
                raise ParseError(f"unbalanced ')' at {i}")
            node = stack.pop()
            node.end = i + 1
            if not stack:
                root = node
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n:
                d = text[j]
                if d == "\\" and j + 1 < n:
                    e = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(e, e))
                    j += 2
                    continue
                if d == '"':
                    break
                buf.append(d)
                j += 1
            if not stack:
                raise ParseError(f"string outside list at {i}")
            stack[-1].items.append(Atom("".join(buf), True, i, j + 1))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            if not stack:
                raise ParseError(f"atom outside list at {i}")
            stack[-1].items.append(Atom(text[i:j], False, i, j))
            i = j
        if root is not None:
            break
    if root is None:
        raise ParseError("no complete expression")
    return root


def quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def fmt_num(v: float) -> str:
    """Format a coordinate the way KiCad writes it (up to 4 decimals, no trailing zeros)."""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


class Patcher:
    """Collects non-overlapping text replacements and applies them in one go."""

    def __init__(self, text: str):
        self.text = text
        self.edits: list[tuple[int, int, str]] = []

    def replace(self, start: int, end: int, new: str) -> None:
        self.edits.append((start, end, new))

    def insert(self, pos: int, new: str) -> None:
        self.edits.append((pos, pos, new))

    def apply(self) -> str:
        out = []
        last = 0
        for start, end, new in sorted(self.edits, key=lambda e: (e[0], e[1])):
            if start < last:
                raise ValueError("overlapping edits")
            out.append(self.text[last:start])
            out.append(new)
            last = end
        out.append(self.text[last:])
        return "".join(out)
