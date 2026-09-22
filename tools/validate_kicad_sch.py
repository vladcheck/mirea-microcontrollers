#!/usr/bin/env python3
"""Validate KiCad schematic (.kicad_sch) s-expression files.

Lexical checks (always run):
  - no ';' comments outside string literals (KiCad s-expr does not support them)
  - balanced parentheses (strings and escapes are respected)
  - all (uuid "...") values are well-formed UUIDs

Geometric / layout checks (run when the file parses):
  errors:
  - pin-wire-connectivity  detached wire endpoints (unconnected pins are warnings)
  - unintended-junction    nets with >= 3 pins and no global label;
                           nets with >= 2 global labels of different names
  - symbol-body-overlap    instance graphics bboxes overlapping > 0.05 mm
  - wire-through-body      wire segment crossing a symbol body it does not
                           connect to
  warnings:
  - text-overlap           estimated text bboxes vs bodies / wires / texts
  - sheet-utilization      used area < 10% of the paper
  - grid-alignment         coordinates not on the 1.27 mm grid

Usage:
    python3 tools/validate_kicad_sch.py [--strict] <file.kicad_sch> [more files...]

Exit code 0 when all files are valid, 1 on any error (warnings also fail
with --strict), 2 on usage errors.
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

EPS = 0.01  # coincidence tolerance, mm
OVERLAP_MIN = 0.05  # min bbox overlap depth that counts as overlap, mm
BODY_SHRINK = 0.05  # shrink of body bbox for wire-through-body, mm
WIRE_INFLATE = 0.2  # inflation of wire segments for text-overlap, mm
GRID = 1.27  # schematic grid step, mm
GRID_TOL = 0.001  # tolerance for grid alignment, mm
MIN_UTILIZATION = 10.0  # percent of sheet area that should be used
PAPER_SIZES = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A5": (210.0, 148.0)}


def fmt(v: float) -> str:
    """Format a coordinate compactly (105.16, 65.4, ...)."""
    return f"{v:.4f}".rstrip("0").rstrip(".")


def close(a: float, b: float, eps: float = EPS) -> bool:
    return abs(a - b) <= eps


def points_close(
    p: tuple[float, float], q: tuple[float, float], eps: float = EPS
) -> bool:
    return close(p[0], q[0], eps) and close(p[1], q[1], eps)


# --------------------------------------------------------------------------
# Lexical validation (original checks, unchanged)
# --------------------------------------------------------------------------


def validate_lexical(path: str) -> list[str]:
    """Return a list of human-readable lexical problems found in the file."""
    errors: list[str] = []
    src = open(path, encoding="utf-8").read()

    depth = 0
    in_str = False
    esc = False
    line = 1
    col = 0
    str_start = (0, 0)

    uuids: list[tuple[str, int, int]] = []
    cur_tok: list[str] | None = None

    for i, c in enumerate(src):
        if c == "\n":
            line += 1
            col = 0
            continue
        col += 1

        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
                if cur_tok is not None:
                    cur_tok.append(c)
            else:
                if cur_tok is not None:
                    cur_tok.append(c)
            continue

        if c == ";":
            errors.append(
                f'{path}:{line}:{col}: ";" comment is not valid in .kicad_sch files'
            )
            continue
        if c == '"':
            in_str = True
            str_start = (line, col)
            if cur_tok is None:
                cur_tok = ['"']
            continue
        if c == "(":
            depth += 1
            cur_tok = None
            continue
        if c == ")":
            depth -= 1
            if depth < 0:
                errors.append(f'{path}:{line}:{col}: unmatched ")"')
                depth = 0
            cur_tok = None
            continue
        if cur_tok is not None and not c.isspace():
            cur_tok.append(c)
            if c == '"':
                tok = "".join(cur_tok)
                uuids.append((tok, str_start[0], str_start[1]))

    if in_str:
        errors.append(
            f"{path}:{str_start[0]}:{str_start[1]}: unterminated string literal"
        )
    if depth != 0:
        errors.append(f"{path}: unbalanced parentheses, final depth {depth}")

    # Collect uuid values via regex over the whole file (positions from line scan)
    for m in re.finditer(r'\(uuid\s+"([^"]*)"\)', src):
        u = m.group(1)
        if not UUID_RE.match(u):
            l = src.count("\n", 0, m.start()) + 1
            cnum = m.start() - src.rfind("\n", 0, m.start())
            errors.append(f'{path}:{l}:{cnum}: malformed uuid "{u}"')

    return errors


# --------------------------------------------------------------------------
# S-expression parser
# --------------------------------------------------------------------------


class QString(str):
    """A quoted string literal, as opposed to a bare atom."""


SExpr = "str | list[SExpr]"


def tokenize(src: str) -> list:
    tokens: list = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c in "()":
            tokens.append(c)
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf: list[str] = []
            while j < n:
                cj = src[j]
                if cj == "\\" and j + 1 < n:
                    buf.append(src[j + 1])
                    j += 2
                    continue
                if cj == '"':
                    break
                buf.append(cj)
                j += 1
            if j >= n:
                raise ValueError("unterminated string literal")
            tokens.append(QString("".join(buf)))
            i = j + 1
            continue
        j = i
        while j < n and not src[j].isspace() and src[j] not in '()"':
            j += 1
        tokens.append(src[i:j])
        i = j
    return tokens


def parse_tokens(tokens: list) -> list:
    pos = 0

    def parse_one():
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        if tok == "(":
            out: list = []
            while True:
                if pos >= len(tokens):
                    raise ValueError("unexpected end of input inside '('")
                if tokens[pos] == ")":
                    pos += 1
                    return out
                out.append(parse_one())
        if tok == ")":
            raise ValueError("unexpected ')'")
        return tok

    trees: list = []
    while pos < len(tokens):
        trees.append(parse_one())
    return trees


def parse(src: str) -> list:
    return parse_tokens(tokenize(src))


# --------------------------------------------------------------------------
# S-expression access helpers
# --------------------------------------------------------------------------


def is_node(v, name: str | None = None) -> bool:
    return (
        isinstance(v, list)
        and len(v) > 0
        and isinstance(v[0], str)
        and (name is None or v[0] == name)
    )


def children(node: list, name: str) -> list:
    return [c for c in node if is_node(c, name)]


def child(node: list, name: str):
    for c in node:
        if is_node(c, name):
            return c
    return None


def get_at(node: list) -> tuple[float, float, float]:
    at = child(node, "at")
    if at is None or len(at) < 3:
        raise ValueError("missing (at X Y ...)")
    x = float(at[1])
    y = float(at[2])
    angle = float(at[3]) if len(at) > 3 else 0.0
    return x, y, angle


def get_xy(node: list) -> tuple[float, float]:
    return float(node[1]), float(node[2])


def get_font_and_justify(node: list) -> tuple[float, float, str]:
    font_w = font_h = 1.27
    justify = "center"
    effects = child(node, "effects")
    if effects is not None:
        font = child(effects, "font")
        if font is not None:
            size = child(font, "size")
            if size is not None and len(size) >= 3:
                font_w = float(size[1])
                font_h = float(size[2])
        jus = child(effects, "justify")
        if jus is not None and len(jus) >= 2:
            j = str(jus[1])
            if j in ("left", "right", "center"):
                justify = j
    return font_w, font_h, justify


# --------------------------------------------------------------------------
# Schematic model
# --------------------------------------------------------------------------

BBox = tuple[float, float, float, float]  # minx, miny, maxx, maxy


def bbox_union(a: BBox | None, b: BBox) -> BBox:
    if a is None:
        return b
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def bbox_of_points(pts: list[tuple[float, float]]) -> BBox:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_overlap(a: BBox, b: BBox) -> tuple[float, float]:
    """Overlap depth on each axis (negative when disjoint)."""
    return (min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1]))


def bboxes_intersect(a: BBox, b: BBox) -> bool:
    dx, dy = bbox_overlap(a, b)
    return dx > 1e-9 and dy > 1e-9


def rotate(x: float, y: float, angle: float) -> tuple[float, float]:
    """Rotate a local point by the instance angle (visual CCW, Y down)."""
    a = int(round(angle)) % 360
    if a == 0:
        return x, y
    if a == 90:
        return y, -x
    if a == 180:
        return -x, -y
    if a == 270:
        return -y, x
    r = math.radians(angle)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


def transform(
    x: float, y: float, angle: float, ox: float, oy: float
) -> tuple[float, float]:
    rx, ry = rotate(x, y, angle)
    return ox + rx, oy + ry


@dataclass
class LibPin:
    x: float
    y: float
    etype: str
    name: str
    number: str


@dataclass
class LibSymbol:
    name: str
    boxes: list[BBox] = field(default_factory=list)  # local graphics bboxes
    pins: list[LibPin] = field(default_factory=list)


@dataclass
class TextItem:
    kind: str  # 'text' | 'label' | 'property'
    owner: str  # human-readable origin, e.g. 'property MCU1.Value'
    content: str
    x: float
    y: float
    angle: float
    font_w: float
    font_h: float
    justify: str


@dataclass
class Instance:
    lib_id: str
    ref: str
    x: float
    y: float
    angle: float
    pins: list[tuple[str, float, float]] = field(
        default_factory=list
    )  # (label, ax, ay)
    bbox: BBox | None = None
    props: list[TextItem] = field(default_factory=list)


@dataclass
class Wire:
    pts: list[tuple[float, float]]


@dataclass
class Model:
    paper: str = "A4"
    instances: list[Instance] = field(default_factory=list)
    wires: list[Wire] = field(default_factory=list)
    labels: list[TextItem] = field(default_factory=list)
    texts: list[TextItem] = field(default_factory=list)  # standalone text items


def unit_is_graphics(unit_node: list) -> bool:
    """Unit name 'Name_0_1' holds graphics common to all units."""
    name = str(unit_node[1]) if len(unit_node) > 1 else ""
    parts = name.split("_")
    return len(parts) >= 3 and parts[-2] == "0"


def lib_graphics_boxes(sym_node: list) -> list[BBox]:
    boxes: list[BBox] = []
    units = children(sym_node, "symbol")
    gfx_units = [u for u in units if unit_is_graphics(u)] or units
    for u in gfx_units:
        for c in u:
            if is_node(c, "rectangle"):
                s = child(c, "start")
                e = child(c, "end")
                if s is not None and e is not None:
                    x0, y0 = get_xy(s)
                    x1, y1 = get_xy(e)
                    boxes.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
            elif is_node(c, "polyline"):
                pts_node = child(c, "pts")
                if pts_node is not None:
                    pts = [get_xy(p) for p in children(pts_node, "xy")]
                    if pts:
                        boxes.append(bbox_of_points(pts))
            elif is_node(c, "circle"):
                ce = child(c, "center")
                ra = child(c, "radius")
                if ce is not None and ra is not None:
                    cx, cy = get_xy(ce)
                    r = float(ra[1])
                    boxes.append((cx - r, cy - r, cx + r, cy + r))
            elif is_node(c, "arc"):
                pts = []
                for key in ("start", "mid", "end"):
                    n = child(c, key)
                    if n is not None:
                        pts.append(get_xy(n))
                if pts:
                    boxes.append(bbox_of_points(pts))
            elif is_node(c, "text"):
                try:
                    x, y, _ = get_at(c)
                    boxes.append((x, y, x, y))
                except (ValueError, IndexError):
                    pass
    return boxes


def lib_pins(sym_node: list) -> list[LibPin]:
    units = children(sym_node, "symbol")
    pin_units = [u for u in units if not unit_is_graphics(u)] or units
    pins: list[LibPin] = []
    for u in pin_units:
        for p in children(u, "pin"):
            etype = str(p[1]) if len(p) > 1 else "passive"
            at = child(p, "at")
            if at is None:
                continue
            x, y = float(at[1]), float(at[2])
            nm = child(p, "name")
            name = str(nm[1]) if nm is not None and len(nm) > 1 else ""
            nb = child(p, "number")
            number = str(nb[1]) if nb is not None and len(nb) > 1 else "?"
            pins.append(LibPin(x, y, etype, name, number))
    return pins


def pin_display_name(pin: LibPin) -> str:
    return pin.name if pin.name not in ("", "~") else pin.number


def build_model(root: list) -> Model:
    model = Model()

    paper = child(root, "paper")
    if paper is not None and len(paper) > 1:
        model.paper = str(paper[1])

    libs: dict[str, LibSymbol] = {}
    lib_symbols = child(root, "lib_symbols")
    if lib_symbols is not None:
        for sym in children(lib_symbols, "symbol"):
            name = str(sym[1])
            libs[name] = LibSymbol(name, lib_graphics_boxes(sym), lib_pins(sym))

    for node in root:
        if is_node(node, "symbol"):
            lib_id_node = child(node, "lib_id")
            if lib_id_node is None or len(lib_id_node) < 2:
                continue
            lib_id = str(lib_id_node[1])
            try:
                x, y, angle = get_at(node)
            except ValueError:
                continue
            inst = Instance(lib_id, "?", x, y, angle)
            for prop in children(node, "property"):
                if len(prop) < 3:
                    continue
                pname = str(prop[1])
                pvalue = str(prop[2])
                if pname == "Reference":
                    inst.ref = pvalue
                try:
                    px, py, pangle = get_at(prop)
                except ValueError:
                    continue
                fw, fh, jus = get_font_and_justify(prop)
                inst.props.append(
                    TextItem(
                        "property",
                        f"property {inst.ref}.{pname}",
                        pvalue,
                        px,
                        py,
                        pangle,
                        fw,
                        fh,
                        jus,
                    )
                )
            lib = libs.get(lib_id)
            if lib is not None:
                for lp in lib.pins:
                    ax, ay = transform(lp.x, lp.y, angle, x, y)
                    inst.pins.append((pin_display_name(lp), ax, ay))
                for x0, y0, x1, y1 in lib.boxes:
                    corners = [
                        transform(cx, cy, angle, x, y)
                        for cx, cy in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
                    ]
                    inst.bbox = bbox_union(inst.bbox, bbox_of_points(corners))
            model.instances.append(inst)
        elif is_node(node, "wire"):
            pts_node = child(node, "pts")
            if pts_node is not None:
                pts = [get_xy(p) for p in children(pts_node, "xy")]
                if len(pts) >= 2:
                    model.wires.append(Wire(pts))
        elif is_node(node, "global_label"):
            if len(node) < 2:
                continue
            name = str(node[1])
            try:
                x, y, angle = get_at(node)
            except ValueError:
                continue
            fw, fh, jus = get_font_and_justify(node)
            model.labels.append(
                TextItem(
                    "label", f'global_label "{name}"', name, x, y, angle, fw, fh, jus
                )
            )
        elif is_node(node, "text"):
            if len(node) < 2:
                continue
            content = str(node[1])
            try:
                x, y, angle = get_at(node)
            except ValueError:
                continue
            fw, fh, jus = get_font_and_justify(node)
            short = content if len(content) <= 30 else content[:27] + "..."
            model.texts.append(
                TextItem("text", f'text "{short}"', content, x, y, angle, fw, fh, jus)
            )

    return model


# --------------------------------------------------------------------------
# Check 1: pin-wire-connectivity
# --------------------------------------------------------------------------


def check_connectivity(model: Model) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    pin_pts = [
        (label, x, y)
        for inst in model.instances
        for (label, x, y) in [
            (f"{inst.ref}.{pname}", px, py) for (pname, px, py) in inst.pins
        ]
    ]
    label_pts = [(lbl.content, lbl.x, lbl.y) for lbl in model.labels]
    wire_endpoints: list[tuple[float, float]] = []
    for w in model.wires:
        wire_endpoints.append(w.pts[0])
        wire_endpoints.append(w.pts[-1])

    for wi, w in enumerate(model.wires):
        for endpoint in (w.pts[0], w.pts[-1]):
            if any(points_close(endpoint, (px, py)) for (_, px, py) in pin_pts):
                continue
            if any(points_close(endpoint, (lx, ly)) for (_, lx, ly) in label_pts):
                continue
            hit = False
            for wj, other in enumerate(model.wires):
                if wj == wi:
                    continue
                if any(points_close(endpoint, v) for v in other.pts):
                    hit = True
                    break
            if not hit:
                errors.append(
                    f"wire endpoint ({fmt(endpoint[0])},{fmt(endpoint[1])}) "
                    f"is not connected to any pin, label or wire"
                )

    for inst in model.instances:
        for pname, px, py in inst.pins:
            if any(points_close((px, py), ep) for ep in wire_endpoints):
                continue
            if any(points_close((px, py), (lx, ly)) for (_, lx, ly) in label_pts):
                continue
            warnings.append(
                f"pin {inst.ref}.{pname} at ({fmt(px)},{fmt(py)}) "
                f"has no wire or label connection"
            )

    return errors, warnings


# --------------------------------------------------------------------------
# Check 2: unintended-junction
# --------------------------------------------------------------------------


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[rj] = ri


def check_junctions(model: Model) -> list[str]:
    errors: list[str] = []

    # Points: pins, label anchors, wire vertices.
    points: list[tuple[float, float]] = []
    kinds: list[
        tuple[str, int]
    ] = []  # ('pin', inst/pin idx) | ('label', idx) | ('wire', wire idx)
    pin_info: list[tuple[str, str]] = []  # (ref, pin name) per pin point
    for inst in model.instances:
        for pname, px, py in inst.pins:
            pin_info.append((inst.ref, pname))
            points.append((px, py))
            kinds.append(("pin", len(pin_info) - 1))
    label_base = len(points)
    for lbl in model.labels:
        points.append((lbl.x, lbl.y))
        kinds.append(("label", len(points) - 1 - label_base))
    wire_base = len(points)
    wire_vertex_wire: list[int] = []
    for wi, w in enumerate(model.wires):
        for v in w.pts:
            points.append(v)
            kinds.append(("wire", wi))
            wire_vertex_wire.append(wi)

    uf = UnionFind(len(points))
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            if points_close(points[i], points[j]):
                uf.union(i, j)
    # Union consecutive vertices along each wire polyline.
    idx = wire_base
    for w in model.wires:
        for k in range(len(w.pts) - 1):
            uf.union(idx + k, idx + k + 1)
        idx += len(w.pts)

    nets: dict[int, dict[str, list]] = {}
    for i, (kind, num) in enumerate(kinds):
        root = uf.find(i)
        net = nets.setdefault(root, {"pins": [], "labels": []})
        if kind == "pin":
            ref, pname = pin_info[num]
            net["pins"].append(
                f"{ref}.{pname} ({fmt(points[i][0])},{fmt(points[i][1])})"
            )
        elif kind == "label":
            net["labels"].append(model.labels[num].content)

    for net in nets.values():
        pins, labels = net["pins"], net["labels"]
        if len(pins) >= 3 and not labels:
            errors.append(
                f"net with {len(pins)} pins and no global label: "
                + ", ".join(sorted(pins))
            )
        distinct = set(labels)
        if len(distinct) >= 2:
            errors.append(
                f"net contains conflicting global labels: "
                + ", ".join(sorted(distinct))
                + (" (pins: " + ", ".join(sorted(pins)) + ")" if pins else "")
            )

    return errors


# --------------------------------------------------------------------------
# Check 3: symbol-body-overlap
# --------------------------------------------------------------------------


def check_body_overlap(model: Model) -> list[str]:
    errors: list[str] = []
    bodies = [
        (inst.ref, inst.bbox) for inst in model.instances if inst.bbox is not None
    ]
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            ref_a, box_a = bodies[i]
            ref_b, box_b = bodies[j]
            dx, dy = bbox_overlap(box_a, box_b)
            if dx > OVERLAP_MIN and dy > OVERLAP_MIN:
                errors.append(
                    f"symbol bodies of {ref_a} and {ref_b} overlap by "
                    f"{dx:.2f} mm x {dy:.2f} mm"
                )
    return errors


# --------------------------------------------------------------------------
# Check 4: wire-through-body
# --------------------------------------------------------------------------


def seg_intersects_rect(x1: float, y1: float, x2: float, y2: float, rect: BBox) -> bool:
    """Liang-Barsky clip: True when the segment touches the (closed) rect."""
    rx0, ry0, rx1, ry1 = rect
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - rx0), (dx, rx1 - x1), (-dy, y1 - ry0), (dy, ry1 - y1)):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                if t > t1:
                    return False
                if t > t0:
                    t0 = t
            else:
                if t < t0:
                    return False
                if t < t1:
                    t1 = t
    return True


def check_wire_through_body(model: Model) -> list[str]:
    errors: list[str] = []
    for w in model.wires:
        for k in range(len(w.pts) - 1):
            (x1, y1), (x2, y2) = w.pts[k], w.pts[k + 1]
            for inst in model.instances:
                if inst.bbox is None:
                    continue
                x0, y0, bx, by = inst.bbox
                shrunk = (
                    x0 + BODY_SHRINK,
                    y0 + BODY_SHRINK,
                    bx - BODY_SHRINK,
                    by - BODY_SHRINK,
                )
                if shrunk[0] >= shrunk[2] or shrunk[1] >= shrunk[3]:
                    continue
                if not seg_intersects_rect(x1, y1, x2, y2, shrunk):
                    continue
                connected = any(
                    points_close((x1, y1), (px, py)) or points_close((x2, y2), (px, py))
                    for (_, px, py) in inst.pins
                )
                if not connected:
                    errors.append(
                        f"wire segment ({fmt(x1)},{fmt(y1)})-({fmt(x2)},{fmt(y2)}) "
                        f"passes through the body of {inst.ref}"
                    )
    return errors


# --------------------------------------------------------------------------
# Check 5: text-overlap
# --------------------------------------------------------------------------


def text_box(item: TextItem) -> BBox:
    w = len(item.content) * item.font_w * 0.7
    h = item.font_h * 1.4
    x, y = item.x, item.y
    if item.kind == "label":
        a = int(round(item.angle)) % 360
        if a == 90:  # up = decreasing y
            return (x - h / 2, y - w, x + h / 2, y)
        if a == 180:
            return (x - w, y - h / 2, x, y + h / 2)
        if a == 270:
            return (x - h / 2, y, x + h / 2, y + w)
        return (x, y - h / 2, x + w, y + h / 2)
    if item.justify == "left":
        x0, x1 = x, x + w
    elif item.justify == "right":
        x0, x1 = x - w, x
    else:
        x0, x1 = x - w / 2, x + w / 2
    return (x0, y - h / 2, x1, y + h / 2)


def check_text_overlap(model: Model) -> list[str]:
    warnings: list[str] = []

    items: list[tuple[TextItem, BBox]] = []
    for inst in model.instances:
        for prop in inst.props:
            items.append((prop, text_box(prop)))
    for lbl in model.labels:
        items.append((lbl, text_box(lbl)))
    for txt in model.texts:
        items.append((txt, text_box(txt)))

    bodies = [
        (inst.ref, inst.bbox) for inst in model.instances if inst.bbox is not None
    ]
    for item, box in items:
        for ref, bb in bodies:
            if bboxes_intersect(box, bb):
                warnings.append(f"{item.owner} overlaps the body of {ref}")

    for item, box in items:
        anchor = (item.x, item.y)
        for w in model.wires:
            # A text anchored exactly on a wire vertex legitimately attaches
            # to that wire; overlapping it at the anchor is not a defect.
            if any(points_close(anchor, v) for v in w.pts):
                continue
            for k in range(len(w.pts) - 1):
                (x1, y1), (x2, y2) = w.pts[k], w.pts[k + 1]
                seg_box = (
                    min(x1, x2) - WIRE_INFLATE,
                    min(y1, y2) - WIRE_INFLATE,
                    max(x1, x2) + WIRE_INFLATE,
                    max(y1, y2) + WIRE_INFLATE,
                )
                if bboxes_intersect(box, seg_box):
                    warnings.append(
                        f"{item.owner} overlaps wire segment "
                        f"({fmt(x1)},{fmt(y1)})-({fmt(x2)},{fmt(y2)})"
                    )

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            item_a, box_a = items[i]
            item_b, box_b = items[j]
            if bboxes_intersect(box_a, box_b):
                warnings.append(f"{item_a.owner} overlaps {item_b.owner}")

    return warnings


# --------------------------------------------------------------------------
# Check 6: sheet-utilization
# --------------------------------------------------------------------------


def check_sheet_utilization(model: Model) -> list[str]:
    # Standalone annotation texts are documentation, not circuit layout;
    # everything else (bodies, wires, labels, properties, pins) counts.
    used: BBox | None = None
    for inst in model.instances:
        if inst.bbox is not None:
            used = bbox_union(used, inst.bbox)
        for _, px, py in inst.pins:
            used = bbox_union(used, (px, py, px, py))
        for prop in inst.props:
            used = bbox_union(used, text_box(prop))
    for w in model.wires:
        used = bbox_union(used, bbox_of_points(w.pts))
    for lbl in model.labels:
        used = bbox_union(used, text_box(lbl))
        used = bbox_union(used, (lbl.x, lbl.y, lbl.x, lbl.y))
    if used is None:
        return []

    pw, ph = PAPER_SIZES.get(model.paper.upper(), PAPER_SIZES["A4"])
    used_area = (used[2] - used[0]) * (used[3] - used[1])
    pct = used_area / (pw * ph) * 100.0
    if pct < MIN_UTILIZATION:
        return [
            f"only {pct:.1f}% of the {model.paper} sheet area is used "
            f"(used bbox {used[2] - used[0]:.1f} x {used[3] - used[1]:.1f} mm)"
        ]
    return []


# --------------------------------------------------------------------------
# Check 7: grid-alignment
# --------------------------------------------------------------------------


def check_grid_alignment(model: Model) -> list[str]:
    warnings: list[str] = []

    def check_value(v: float, where: str) -> None:
        nearest = round(v / GRID) * GRID
        if abs(v - nearest) > GRID_TOL:
            warnings.append(f"off-grid coordinate {fmt(v)} at {where}")

    def check_point(x: float, y: float, where: str) -> None:
        check_value(x, f"{where} (x)")
        check_value(y, f"{where} (y)")

    for inst in model.instances:
        check_point(inst.x, inst.y, f"{inst.ref} position")
        for prop in inst.props:
            check_point(prop.x, prop.y, f"{prop.owner} position")
    for w in model.wires:
        for v in w.pts:
            check_point(v[0], v[1], "wire vertex")
    for lbl in model.labels:
        check_point(lbl.x, lbl.y, f"{lbl.owner} anchor")
    for txt in model.texts:
        check_point(txt.x, txt.y, f"{txt.owner} anchor")

    return warnings


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

CHECKS = [
    ("pin-wire-connectivity", "error"),
    ("unintended-junction", "error"),
    ("symbol-body-overlap", "error"),
    ("wire-through-body", "error"),
    ("text-overlap", "warning"),
    ("sheet-utilization", "warning"),
    ("grid-alignment", "warning"),
]


def validate_file(path: str) -> tuple[list[str], list[str], list[tuple[str, int, int]]]:
    """Validate one file.

    Returns (error_lines, warning_lines, per-check (name, n_errors, n_warnings)).
    """
    lexical = validate_lexical(path)
    if lexical:
        summary = [("lexical", len(lexical), 0)]
        return lexical, [], summary

    src = open(path, encoding="utf-8").read()
    try:
        trees = parse(src)
    except ValueError as e:
        return [f"{path}: parse-error: {e}"], [], [("parse", 1, 0)]
    if len(trees) != 1 or not is_node(trees[0], "kicad_sch"):
        return [f"{path}: parse-error: not a kicad_sch document"], [], [("parse", 1, 0)]

    model = build_model(trees[0])

    results: dict[str, tuple[list[str], list[str]]] = {
        "pin-wire-connectivity": check_connectivity(model),
        "unintended-junction": (check_junctions(model), []),
        "symbol-body-overlap": (check_body_overlap(model), []),
        "wire-through-body": (check_wire_through_body(model), []),
        "text-overlap": ([], check_text_overlap(model)),
        "sheet-utilization": ([], check_sheet_utilization(model)),
        "grid-alignment": ([], check_grid_alignment(model)),
    }

    error_lines: list[str] = []
    warning_lines: list[str] = []
    summary: list[tuple[str, int, int]] = []
    for name, _severity in CHECKS:
        errs, warns = results[name]
        summary.append((name, len(errs), len(warns)))
        for m in errs:
            error_lines.append(f"{path}: {name}: {m}")
        for m in warns:
            warning_lines.append(f"{path}: {name}: {m}")
    return error_lines, warning_lines, summary


def main(argv: list[str]) -> int:
    args = argv[1:]
    strict = False
    if "--strict" in args:
        strict = True
        args.remove("--strict")
    if not args:
        print(__doc__)
        return 2
    failed = False
    for path in args:
        try:
            error_lines, warning_lines, summary = validate_file(path)
        except OSError as e:
            print(f"{path}: cannot read: {e}")
            failed = True
            continue
        for line in error_lines + warning_lines:
            print(line)
        for name, n_err, n_warn in summary:
            print(f"{path}: {name}: {n_err} error(s), {n_warn} warning(s)")
        if error_lines or (strict and warning_lines):
            failed = True
            print(f"{path}: FAIL")
        elif warning_lines:
            print(f"{path}: OK ({len(warning_lines)} warning(s))")
        else:
            print(f"{path}: OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
