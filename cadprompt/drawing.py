"""A workshop drawing of one part, made by fixed rules: no AI in here.

make_drawing() puts a TechDraw page into the active document: an ISO 5457
sheet, front, top and left view in first-angle projection, an isometric view
where there is room, the overall dimensions, the title block, and a PDF in the
project's drawings/ folder. It follows the Hoischen addon (DIN preset, 10 mm
and 7 mm dimension lines) when that addon is installed.

It only adds what it can add safely. What a person still has to do (hole
dimensions, tolerances, material) is listed in the text it returns.

Paper coordinates in this file are millimetres from the lower left corner of
the sheet, Y up, as TechDraw places views. SVG templates have Y down; the
template reader turns them over.
"""

import datetime
import os
import re
import time
import xml.etree.ElementTree as ET

import FreeCAD as App

# ISO 5455, largest first: (drawing, real)
SCALES = [(50, 1), (20, 1), (10, 1), (5, 1), (2, 1), (1, 1), (1, 2), (1, 5), (1, 10),
          (1, 20), (1, 50), (1, 100), (1, 200), (1, 500), (1, 1000)]
SHEETS = ("A4", "A3", "A2", "A1", "A0")

FRAME_LEFT, FRAME_REST = 20.0, 10.0   # ISO 5457: filing edge left, 10 mm elsewhere
EDGE = 5.0        # free strip along the frame and around the title block
DIM_ROOM = 17.0   # one dimension line: 10 mm to the line, then the number
GAP = 20.0        # between two views
CAPTION = 9.0     # room under a view for its own scale note
BLOCK_GUESS = (180.0, 60.0)   # title block size when a template cannot be read
WAIT = 60.0       # seconds to wait for TechDraw to work out the views

PAGE_PREFIX = "Drawing "


# ---- small helpers ---------------------------------------------------------
def _num(v):
    return ("%.2f" % v).rstrip("0").rstrip(".")


def scale_text(scale):
    return f"{scale[0]}:{scale[1]}"


def _file_name(label):
    return re.sub(r"[^\w.\- ]+", "-", label).strip(" .-") or "part"


# ---- which object ----------------------------------------------------------
def _has_solid(obj):
    shape = getattr(obj, "Shape", None)
    try:
        return shape is not None and not shape.isNull() and bool(shape.Solids)
    except Exception:
        return False


def _visible(obj):
    vo = getattr(obj, "ViewObject", None) if App.GuiUp else None
    if vo is not None:
        return bool(vo.Visibility)
    return bool(getattr(obj, "Visibility", True))


def _body_of(obj):
    for parent in obj.InList:
        if parent.isDerivedFrom("PartDesign::Body"):
            return parent
    return None


# parents that only look at a solid (attached to it, or copying it): they do not use it up
LOOKS_ONLY = ("Part::Part2DObject", "Part::Datum", "PartDesign::ShapeBinder", "PartDesign::SubShapeBinder")


def _looks_only(parent):
    return any(parent.isDerivedFrom(kind) for kind in LOOKS_ONLY)


def _top_level(obj):
    """False for a feature inside a Body and for the parts a cut or fusion is made of.

    A sketch, datum or binder that is attached to the solid does not hide it.
    """
    return not any(parent.isDerivedFrom("Part::Feature") and not _looks_only(parent)
                   for parent in obj.InList)


def _solids(doc):
    return [o for o in doc.Objects if _has_solid(o) and _top_level(o)]


def pick_object(doc, obj_name=""):
    """(object, None) or (None, error text)."""
    names = ", ".join(o.Label for o in _solids(doc)) or "none"
    if obj_name:
        obj = doc.getObject(obj_name)
        if obj is None:
            for o in doc.Objects:
                if o.Label == obj_name or o.Label.lower() == obj_name.lower():
                    obj = o
                    break
        if obj is None:
            return None, (f"ERROR: there is no object named '{obj_name}' in this document. "
                          f"Solids you can draw: {names}.")
        body = _body_of(obj)
        if body is not None and getattr(body, "Tip", None) == obj:
            obj = body    # the last feature of a Body is the Body
        if not _has_solid(obj):
            return None, (f"ERROR: '{obj.Label}' is not a solid, so there is nothing to draw. "
                          f"Solids you can draw: {names}.")
        return obj, None

    if App.GuiUp:
        import FreeCADGui as Gui
        picked = []
        for o in Gui.Selection.getSelection(doc.Name):
            o = _body_of(o) or o
            if _has_solid(o) and o not in picked:
                picked.append(o)
        if len(picked) == 1:
            return picked[0], None

    for o in reversed(doc.Objects):
        if _has_solid(o) and _top_level(o) and _visible(o):
            return o, None
    if names != "none":
        return None, (f"ERROR: no solid is visible. Show the part or give its name. "
                      f"Solids in this document: {names}.")
    return None, (f"ERROR: the document '{doc.Label}' has no solid to draw. "
                  "Build a part first, then ask for the drawing again.")


def _global_shape(obj):
    """The shape where TechDraw will see it (placements of parent containers included)."""
    shape = obj.Shape.copy()
    try:
        shape.Placement = obj.getGlobalPlacement()
    except Exception:
        pass
    return shape


def _bound_box(shape):
    # Shape.BoundBox comes from the display mesh and can be a little too small
    # on round parts (measured: 29.94 for a 30 mm shaft).
    try:
        return shape.optimalBoundingBox(False, False)
    except Exception:
        return shape.BoundBox


# ---- which way to look -----------------------------------------------------
AXES = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _parallel(v, axis):
    return App.Vector(*v).cross(App.Vector(*axis)).Length < 1e-6


def _on_line(point, origin, axis):
    return (App.Vector(*point) - App.Vector(*origin)).cross(App.Vector(*axis)).Length < 1e-6


def turned_axis(shape):
    """0, 1 or 2 when the shape is a pure turned part around X, Y or Z, else None.

    Every face must be a plane across the axis or a round surface on it, so a
    keyway, a flat or a cross hole makes it an ordinary part.
    """
    faces = shape.Faces
    for i, axis in enumerate(AXES):
        origin, ok = None, bool(faces)
        for f in faces:
            s = f.Surface
            kind = type(s).__name__
            try:
                if kind == "Plane":
                    good = _parallel(tuple(s.Axis), axis)
                elif kind in ("Cylinder", "Cone", "Toroid"):
                    centre = tuple(s.Center)
                    origin = origin or centre
                    good = _parallel(tuple(s.Axis), axis) and _on_line(centre, origin, axis)
                elif kind == "Sphere":
                    centre = tuple(s.Center)
                    origin = origin or centre
                    good = _on_line(centre, origin, axis)
                else:
                    good = False
            except Exception:
                good = False
            if not good:
                ok = False
                break
        if ok and origin is not None:
            return i
    return None


def front_view(size, turned=None):
    """(Direction, XDirection, words) for the front view.

    size is the bounding box (x, y, z). The front view shows the largest side
    of that box. Z stays up when it can; a plate lying flat and a turned part
    get their long side level, as they are made.
    """
    dx, dy, dz = size
    if turned is not None:
        length = size[turned]
        across = max(size[j] for j in range(3) if j != turned)
        if length >= across:     # shaft: side view, axis level
            return (((0, -1, 0), (1, 0, 0), "the side of the turned part, axis level"),
                    ((1, 0, 0), (0, 1, 0), "the side of the turned part, axis level"),
                    ((0, -1, 0), (0, 0, 1), "the side of the turned part, axis level"))[turned]
        return (((1, 0, 0), (0, 1, 0), "along the axis of the turned part"),      # disc
                ((0, -1, 0), (1, 0, 0), "along the axis of the turned part"),
                ((0, 0, 1), (1, 0, 0), "along the axis of the turned part"))[turned]
    choices = [
        (dx * dz, (0, -1, 0), (1, 0, 0), "from the front (looking along Y)"),
        (dy * dz, (1, 0, 0), (0, 1, 0), "from the right (looking along X)"),
        (dx * dy, (0, 0, 1), (1, 0, 0) if dx >= dy else (0, 1, 0), "from above (looking along Z)"),
    ]
    best = choices[0]
    for c in choices[1:]:
        if c[0] > best[0] * (1 + 1e-6):
            best = c
    return best[1], best[2], best[3]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _extent(size, axis):
    return sum(abs(a) * s for a, s in zip(axis, size))


def iso_axes():
    """Direction and XDirection of the isometric view: the model as FreeCAD's own isometric view shows it."""
    d, x = App.Vector(1, -1, 1), App.Vector(1, 1, 0)
    d.normalize()
    x.normalize()
    return d, x


def iso_extent(size):
    """Width and height of the bounding box seen in the isometric view, model mm."""
    d, x = iso_axes()
    up = d.cross(x)
    xs, ys = [], []
    for cx in (-0.5, 0.5):
        for cy in (-0.5, 0.5):
            for cz in (-0.5, 0.5):
                p = App.Vector(cx * size[0], cy * size[1], cz * size[2])
                xs.append(p.dot(x))
                ys.append(p.dot(up))
    return max(xs) - min(xs), max(ys) - min(ys)


# ---- the sheet -------------------------------------------------------------
def _template_dir():
    return os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO")


def find_template(sheet, orientation="Landscape"):
    """FreeCAD's own template for a sheet: the German ISO 5457 / ISO 7200 one first."""
    base = _template_dir()
    fmt = f"{sheet}_{orientation}"
    for path in (
        os.path.join(base, "localized", "de", f"{fmt}_ISO7200_DE.svg"),        # FreeCAD 1.1
        os.path.join(base, "localized", "de", f"{fmt}_ISO5457_advanced.svg"),  # older layouts
        os.path.join(base, f"{fmt}_ISO5457_advanced.svg"),                     # English
        os.path.join(base, f"{fmt}_ISO5457_minimal.svg"),
    ):
        if os.path.isfile(path):
            return path
    return None


_NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_COMMAND = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])([^MmLlHhVvCcSsQqTtAaZz]*)")
_PAIRS = {"m": 1, "l": 1, "t": 1, "q": 2, "s": 2, "c": 3}


def _floats(text):
    return [float(v) for v in _NUMBER.findall(text or "")]


def _path_points(d):
    """The points an SVG path runs through (control points count: we only need its box)."""
    pts, x, y, start = [], 0.0, 0.0, (0.0, 0.0)
    for cmd, args in _COMMAND.findall(d or ""):
        n, rel, c = _floats(args), cmd.islower(), cmd.lower()
        if c == "z":
            x, y = start
        elif c == "h":
            for v in n:
                x = x + v if rel else v
                pts.append((x, y))
        elif c == "v":
            for v in n:
                y = y + v if rel else v
                pts.append((x, y))
        elif c == "a":
            for i in range(0, len(n) - 6, 7):
                x, y = (x + n[i + 5], y + n[i + 6]) if rel else (n[i + 5], n[i + 6])
                pts.append((x, y))
        else:
            step = 2 * _PAIRS[c]
            for i in range(0, len(n) - step + 1, step):
                seg = [(n[j] + (x if rel else 0.0), n[j + 1] + (y if rel else 0.0))
                       for j in range(i, i + step, 2)]
                pts += seg
                x, y = seg[-1]
                if c == "m" and i == 0:
                    start = (x, y)
    return pts


def _mul(m, t):
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = t
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2,
            a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def _matrix(m, text):
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        v = _floats(args)
        if name == "translate" and v:
            m = _mul(m, (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0.0))
        elif name == "scale" and v:
            m = _mul(m, (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0))
        elif name == "matrix" and len(v) == 6:
            m = _mul(m, tuple(v))
    return m


def _first(el, key, default=0.0):
    v = _floats(el.get(key))
    return v[0] if v else default


def _shapes(el, m, out):
    """Corner points of every drawn thing in the template, in sheet mm (Y down)."""
    tag = el.tag.split("}")[-1]
    if tag in ("defs", "metadata", "namedview", "style", "title", "desc"):
        return
    m = _matrix(m, el.get("transform"))
    pts = []
    if tag == "rect":
        x, y, w, h = (_first(el, k) for k in ("x", "y", "width", "height"))
        pts = [(x, y), (x + w, y + h)]
    elif tag == "circle":
        cx, cy, r = (_first(el, k) for k in ("cx", "cy", "r"))
        pts = [(cx - r, cy - r), (cx + r, cy + r)]
    elif tag == "line":
        pts = [(_first(el, "x1"), _first(el, "y1")), (_first(el, "x2"), _first(el, "y2"))]
    elif tag == "path":
        pts = _path_points(el.get("d"))
    elif tag == "text":
        editable = any(k.endswith("}editable") for k in el.attrib)
        if editable or "".join(el.itertext()).strip():
            x, y = _first(el, "x"), _first(el, "y")
            pts = [(x, y - 3.0), (x, y + 1.0)]   # a line of small text, more or less
    if pts:
        out.append([(m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]) for x, y in pts])
    if tag != "text":
        for child in el:
            _shapes(child, m, out)


def read_template(path):
    """What the layout needs to know about a template.

    Returns {"width", "height", "frame", "block", "autofill"}: frame and block
    are (x0, y0, x1, y1) in paper mm, Y up; block is the title block with all
    that belongs to it, or None when the template has none; autofill maps the
    name of an editable text to what FreeCAD would put there (title, scale…).
    """
    root = ET.parse(path).getroot()
    width, height = _first(root, "width"), _first(root, "height")
    box = _floats(root.get("viewBox"))
    k = width / box[2] if len(box) == 4 and box[2] else 1.0
    shapes, autofill = [], {}
    _shapes(root, (k, 0, 0, k, 0, 0), shapes)
    for el in root.iter():
        name = fill = None
        for key, value in el.attrib.items():
            if key.endswith("}editable"):
                name = value
            elif key.endswith("}autofill"):
                fill = value
        if name:
            autofill[name] = fill or ""

    x0, y0, x1, y1 = FRAME_LEFT, FRAME_REST, width - FRAME_REST, height - FRAME_REST
    inside = []
    for pts in shapes:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if min(xs) < x0 - 0.05 or max(xs) > x1 + 0.05 or min(ys) < y0 - 0.05 or max(ys) > y1 + 0.05:
            continue    # the border, grid marks and centring marks
        if max(xs) - min(xs) > 0.98 * (x1 - x0) and max(ys) - min(ys) > 0.98 * (y1 - y0):
            continue    # the frame itself
        inside.append((min(xs), min(ys), max(xs), max(ys)))
    block = None
    if inside:
        bx0, by0 = min(b[0] for b in inside), min(b[1] for b in inside)
        bx1, by1 = max(b[2] for b in inside), max(b[3] for b in inside)
        in_corner = bx1 > x1 - 15 and by1 > y1 - 15
        too_big = (bx1 - bx0) * (by1 - by0) > 0.45 * (x1 - x0) * (y1 - y0)
        if not in_corner or too_big:   # not a title block as we know it: keep the usual corner free
            bx0, by0 = max(x0, x1 - BLOCK_GUESS[0]), y1 - BLOCK_GUESS[1]
        block = (bx0, height - y1, x1, height - by0)
    return {"width": width, "height": height, "frame": (x0, height - y1, x1, height - y0),
            "block": block, "autofill": autofill}


# ---- where the views go ----------------------------------------------------
def _fits(rects, frame, block):
    for x0, y0, x1, y1 in rects:
        if (x0 < frame[0] + EDGE - 1e-6 or y0 < frame[1] + EDGE - 1e-6
                or x1 > frame[2] - EDGE + 1e-6 or y1 > frame[3] - EDGE + 1e-6):
            return False
        if block and (x1 > block[0] - EDGE + 1e-6 and x0 < block[2] + EDGE - 1e-6
                      and y1 > block[1] - EDGE + 1e-6 and y0 < block[3] + EDGE - 1e-6):
            return False
    return True


def _box(rects):
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def _shift(rects, dx, dy):
    return [(r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy) for r in rects]


def _regions(frame, block):
    """Where a group of views may be centred, best first."""
    if not block:
        return [frame]
    return [(frame[0], block[3], frame[2], frame[3]),    # above the title block
            frame,                                       # around the title block
            (frame[0], frame[1], block[0], frame[3])]    # left of the title block


def _centre_in(rects, region, frame, block):
    """Shift that centres a group of rectangles in a region: (dx, dy), or None when it does not fit."""
    b = _box(rects)
    dx = (region[0] + region[2]) / 2.0 - (b[0] + b[2]) / 2.0
    dy = (region[1] + region[3]) / 2.0 - (b[1] + b[3]) / 2.0
    return (dx, dy) if _fits(_shift(rects, dx, dy), frame, block) else None


def _place(rects, frame, block):
    """Move a group of rectangles to a good place on the sheet: (dx, dy) or None."""
    for region in _regions(frame, block):
        at = _centre_in(rects, region, frame, block)
        if at is not None:
            return at
    b = _box(rects)
    dx, dy = frame[0] + EDGE - b[0], frame[3] - EDGE - b[3]        # top left corner
    if _fits(_shift(rects, dx, dy), frame, block):
        return dx, dy
    return None


def view_rects(w, h, d, s):
    """Front, top and left view with the room their dimensions need, around the front view's centre.

    First angle: the top view is under the front view, the left view right of
    it. Dimensions go above and left of the views, where no other view is.
    Returns ([front, top, left], top_y, left_x).
    """
    fw, fh, dd = w * s, h * s, d * s
    top_y = -(fh / 2.0 + GAP + dd / 2.0)
    left_x = fw / 2.0 + GAP + dd / 2.0
    front = (-fw / 2.0 - DIM_ROOM, -fh / 2.0, fw / 2.0, fh / 2.0 + DIM_ROOM)
    top = (-fw / 2.0 - DIM_ROOM, top_y - dd / 2.0, fw / 2.0, top_y + dd / 2.0)
    left = (left_x - dd / 2.0, -fh / 2.0, left_x + dd / 2.0, fh / 2.0 + DIM_ROOM)
    return [front, top, left], top_y, left_x


def plan_layout(size, iso, frame, block):
    """Scale and positions for one sheet, or None when nothing fits.

    size = (w, h, d) of the front view (width, height) and the depth behind
    it, iso = (width, height) of the isometric view, all in model mm.
    Returns {"scale", "front": (x, y), "top_y", "left_x", "rects",
             "iso": None or {"scale", "pos": (x, y), "rect", "where"}}.
    """
    w, h, d = size
    for n, scale in enumerate(SCALES):
        s = scale[0] / float(scale[1])
        rects, top_y, left_x = view_rects(w, h, d, s)
        at = _place(rects, frame, block)
        if at is None:
            continue
        plan = {"scale": scale, "front": at, "top_y": top_y, "left_x": left_x,
                "rects": _shift(rects, *at), "iso": None}
        fw, fh = w * s, h * s
        gb = _box(rects)
        # The isometric view may be smaller than the others (it then gets its
        # own scale note), but not by more than two steps.
        for iso_scale in SCALES[n:n + 3]:
            si = iso_scale[0] / float(iso_scale[1])
            iw, ih = iso[0] * si, iso[1] * si
            note = CAPTION if iso_scale != scale else 0.0
            spots = (
                ("in the free corner of the views", fw / 2.0 + GAP, -fh / 2.0 - GAP),
                ("right of the views", gb[2] + GAP, (gb[1] + gb[3]) / 2.0 + (ih + note) / 2.0),
                ("under the views", gb[2] - iw, gb[1] - GAP),
            )
            # a sheet with everything above the title block looks best, so try that for every spot first
            for region in _regions(frame, block):
                for where, x0, y1 in spots:
                    rect = (x0, y1 - ih - note, x0 + iw, y1)
                    both = _centre_in(rects + [rect], region, frame, block)
                    if both is None:
                        continue
                    plan["front"] = both
                    plan["rects"] = _shift(rects, *both)
                    rect = _shift([rect], *both)[0]
                    plan["iso"] = {"scale": iso_scale, "rect": rect, "where": where,
                                   "pos": ((rect[0] + rect[2]) / 2.0, rect[3] - ih / 2.0)}
                    return plan
        return plan
    return None


def choose_sheet(sheet, size, iso):
    """(orientation, template path, template data, plan) for a sheet, or None.

    A4 is tried upright (the ISO 5457 way) and lying; the one with the larger
    scale wins, upright when both are the same.
    """
    best = None
    for orientation in (("Portrait", "Landscape") if sheet == "A4" else ("Landscape",)):
        path = find_template(sheet, orientation)
        if not path:
            continue
        data = read_template(path)
        plan = plan_layout(size, iso, data["frame"], data["block"])
        if plan is None:
            continue
        rank = SCALES.index(plan["scale"])
        if best is None or rank < best[0]:
            best = (rank, orientation, path, data, plan)
    return best[1:] if best else None


# ---- TechDraw --------------------------------------------------------------
def _events():
    """Let TechDraw's background work arrive, without taking clicks or keys."""
    if App.GuiUp:
        from PySide import QtCore
        QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)


def _wait_for(views, seconds=WAIT):
    """In the GUI TechDraw works out the visible edges in a thread. True when all are there."""
    end = time.time() + seconds
    while True:
        _events()
        try:
            if all(v.getVisibleEdges() for v in views):
                return True
        except Exception:
            pass
        if not App.GuiUp or time.time() > end:
            return False
        time.sleep(0.02)


def remove_page(doc, page):
    """Delete a page with its views, dimensions and template."""
    if App.GuiUp:
        try:
            page.ViewObject.hide()    # closes its tab; a deleted page leaves an empty tab behind
        except Exception:
            pass
    views = list(page.Views)
    groups = [v for v in views if v.isDerivedFrom("TechDraw::DrawProjGroup")]
    for name in [v.Name for v in views if v not in groups]:    # dimensions and single views first
        if doc.getObject(name) is not None:
            doc.removeObject(name)
    for g in groups:
        g.purgeProjections()      # deleting the views one by one makes TechDraw warn about its anchor
        doc.removeObject(g.Name)
    template = page.Template.Name if page.Template is not None else None
    doc.removeObject(page.Name)
    if template and doc.getObject(template) is not None:       # FreeCAD 1.1 takes it along with the page
        doc.removeObject(template)


def _geometry(view):
    """Corner points and edges of a view in model mm around the view centre, Y up.

    The position in each list is the number TechDraw uses in "Vertex3" / "Edge5".
    Centre lines and other added lines come after the real edges and are left out.
    """
    verts, edges = [], []
    while len(verts) < 20000:
        try:
            v = view.getVertexBySelection(f"Vertex{len(verts)}")
        except Exception:
            break
        verts.append((v.X, v.Y))
    while len(edges) < 20000:
        name = f"Edge{len(edges)}"
        try:
            edge = view.getEdgeBySelection(name)
        except Exception:
            break
        try:
            view.getCosmeticEdgeBySelection(name)
            break                       # the first added line: the part's own edges end here
        except Exception:
            edges.append(edge)
    return verts, edges


def _outline(edges):
    box = None
    for e in edges:
        if box is None:
            box = App.BoundBox(e.BoundBox)
        else:
            box.add(e.BoundBox)
    return box


def _straight_on(edge, axis, value, tol):
    """True when the edge is a straight line lying on x = value (axis 0) or y = value (axis 1)."""
    vs = edge.Vertexes
    if len(vs) != 2:
        return False
    a, b = ((vs[0].X, vs[0].Y), (vs[1].X, vs[1].Y))
    if abs(a[axis] - value) > tol or abs(b[axis] - value) > tol:
        return False
    chord = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    return abs(edge.Length - chord) <= tol


def extreme_points(verts, edges, axis, side, tol):
    """Numbers of two corner points that span the view along an axis, or None.

    axis 0 measures across (left to right), 1 measures up. side is +1 when the
    dimension line goes above / right of the view, -1 for below / left: the
    corner nearest to it is taken, so the extension lines stay short. A point
    on a straight outer edge is preferred; a round outline has no corner at
    its outermost point, and then there is no dimension.
    """
    box = _outline(edges)
    if box is None:
        return None
    lo, hi = (box.XMin, box.XMax) if axis == 0 else (box.YMin, box.YMax)
    picked = []
    for value in (lo, hi):
        at = [i for i, v in enumerate(verts) if abs(v[axis] - value) <= tol]
        if not at:
            return None
        ends = []
        for e in edges:
            if _straight_on(e, axis, value, tol):
                ends += [(v.X, v.Y) for v in e.Vertexes]
        on_edge = [i for i in at
                   if any(abs(verts[i][0] - p[0]) <= tol and abs(verts[i][1] - p[1]) <= tol for p in ends)]
        at = on_edge or at
        picked.append(max(at, key=lambda i: (side * verts[i][1 - axis], -i)))
    return picked[0], picked[1]


def outer_circle(edges, tol):
    """Number of the full circle that is the whole outline of a view, or None."""
    box = _outline(edges)
    if box is None or abs(box.XLength - box.YLength) > tol:
        return None
    for i, e in enumerate(edges):
        radius = getattr(e.Curve, "Radius", None)
        if radius is None or type(e.Curve).__name__ != "Circle" or not e.isClosed():
            continue
        c = e.Curve.Center
        if (abs(2 * radius - box.XLength) <= tol and abs(c.x - box.Center.x) <= tol
                and abs(c.y - box.Center.y) <= tol):
            return i
    return None


def _font_size():
    return App.ParamGet("User parameter:BaseApp/Preferences/Mod/TechDraw/Dimensions").GetFloat("FontSize", 5.0)


class _Dims:
    """Adds the overall dimensions to the three views and keeps count of what happened."""

    def __init__(self, doc, page, scale, size):
        self.doc, self.page, self.s = doc, page, scale
        self.tol = 1e-4 * max(size) + 1e-6
        self.made = []      # (dimension object, words, expected value)
        self.skipped = []   # words

    def _add(self, name, kind, refs, x, y, words, expected, prefix=""):
        d = self.doc.addObject("TechDraw::DrawViewDimension", name)
        d.Type = kind
        d.MeasureType = "Projected"
        d.References2D = refs
        self.page.addView(d)
        if prefix:
            d.FormatSpec = prefix + d.FormatSpec
        d.X, d.Y = x, y
        self.made.append((d, words, expected))
        return d

    def linear(self, view, geo, axis, name, what, where, expected, prefix=""):
        """One overall dimension: across the view (axis 0, line above) or up (axis 1, line left)."""
        verts, edges = geo
        side = 1 if axis == 0 else -1
        pair = extreme_points(verts, edges, axis, side, self.tol)
        if pair is None:
            self.skipped.append(f"{what} on the {where}: the outline is round or has no corner at its "
                                "outermost point")
            return False
        box = _outline(edges)
        away = 10.0 + 0.5 + 0.838 * _font_size()    # the number sits just beyond the dimension line
        if axis == 0:
            x, y = (box.XMin + box.XMax) / 2.0 * self.s, box.YMax * self.s + away
        else:
            x, y = box.XMin * self.s - away, (box.YMin + box.YMax) / 2.0 * self.s
        refs = [(view, f"Vertex{pair[0]}"), (view, f"Vertex{pair[1]}")]
        self._add(name, "DistanceX" if axis == 0 else "DistanceY", refs, x, y,
                  f"{what} {prefix}{_num(expected)} ({where})", expected, prefix)
        return True

    def diameter(self, view, geo, edge, name, where, expected):
        r = expected / 2.0 * self.s + 8.0
        self._add(name, "Diameter", [(view, f"Edge{edge}")], 0.7071 * r, 0.7071 * r,
                  f"diameter ⌀{_num(expected)} ({where})", expected)

    def check(self):
        """Throw out every dimension whose number is not the size of the model."""
        good = []
        for d, words, expected in self.made:
            try:
                value = abs(float(d.getRawValue()))
            except Exception:
                value = None
            if value is None or abs(value - expected) > max(1e-3, 1e-6 * expected):
                self.skipped.append(f"{words}: TechDraw measured {_num(value) if value is not None else 'nothing'}, "
                                    f"the model says {_num(expected)}, so it was removed")
                self.doc.removeObject(d.Name)
            else:
                good.append(words)
        return good


def add_dimensions(doc, page, views, scale, size):
    """Overall dimensions on front / top / left view. Returns the _Dims with what was made."""
    front, top, left = views
    w, h, d = size
    dims = _Dims(doc, page, scale, size)
    g_front, g_top, g_left = _geometry(front), _geometry(top), _geometry(left)
    tol = dims.tol

    circle = outer_circle(g_front[1], tol)
    if circle is not None and abs(w - h) <= tol:
        # a disc seen along its axis: one diameter, and the thickness below
        dims.diameter(front, g_front, circle, "DimDiameter", "front view", w)
        if not dims.linear(top, g_top, 1, "DimDepth", "thickness", "top view", d):
            dims.linear(left, g_left, 0, "DimDepth", "thickness", "left view", d)
        return dims
    if outer_circle(g_left[1], tol) is not None and abs(d - h) <= tol:
        # a shaft from the side: length, and its largest diameter instead of height and depth
        dims.linear(front, g_front, 0, "DimLength", "length", "front view", w)
        dims.linear(front, g_front, 1, "DimDiameter", "largest diameter", "front view", h, "⌀")
        return dims
    if outer_circle(g_top[1], tol) is not None and abs(d - w) <= tol:
        # a round part standing up
        dims.linear(front, g_front, 0, "DimDiameter", "largest diameter", "front view", w, "⌀")
        dims.linear(front, g_front, 1, "DimHeight", "height", "front view", h)
        return dims
    dims.linear(front, g_front, 0, "DimLength", "length", "front view", w)
    dims.linear(front, g_front, 1, "DimHeight", "height", "front view", h)
    if not dims.linear(top, g_top, 1, "DimDepth", "depth", "top view", d):
        dims.skipped.pop()    # the left view may still do it; say only the last word
        dims.linear(left, g_left, 0, "DimDepth", "depth", "left view", d)
    return dims


def _centre_style():
    """Number of the centre line (long dash, dot) in the line standard TechDraw is set to."""
    try:
        folder = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "LineGroup")
        names = sorted(f for f in os.listdir(folder) if f.endswith(".LineDef.csv"))
        index = App.ParamGet("User parameter:BaseApp/Preferences/Mod/TechDraw/Standards").GetInt("LineStandard", 1)
        return 3 if names[index].startswith("ANSI") else 4
    except Exception:
        return 4


MANY_CIRCLES = 80   # a perforated sheet does not get a cross in every hole


def add_centre_lines(views, scale, weight, axis=None):
    """A cross on every full circle, and the axis of a turned part. Returns (lines made, note or None).

    axis: None, "side" (turned part seen from the side in the front view) or
    "end" (seen along its axis). Call this after the dimensions are in place:
    the lines stick out 3 mm past the part and would push the dimensions away.
    """
    front, top, left = views
    over = 3.0 / scale
    style = {"style": _centre_style(), "weight": weight}
    count, note = 0, None

    def line(view, a, b):
        tag = view.makeCosmeticLine(App.Vector(a[0], a[1], 0), App.Vector(b[0], b[1], 0))
        edge = view.getCosmeticEdge(tag)
        look = edge.Format
        look.update(style)
        edge.Format = look

    for view in views:
        _verts, edges = _geometry(view)
        box = _outline(edges)
        if box is None:
            continue
        circles = {}
        for e in edges:
            if type(e.Curve).__name__ == "Circle" and e.isClosed():
                c = e.Curve.Center
                key = (round(c.x, 4), round(c.y, 4))
                circles[key] = max(circles.get(key, 0.0), e.Curve.Radius)   # one cross for circles in each other
        if len(circles) > MANY_CIRCLES:
            note = f"More than {MANY_CIRCLES} circles in one view: no centre lines there."
            circles = {}
        for (cx, cy), r in circles.items():
            line(view, (cx - r - over, cy), (cx + r + over, cy))
            line(view, (cx, cy - r - over), (cx, cy + r + over))
            count += 2
        # the view centre is the middle of the part, so the axis of a turned part runs through it
        level = (axis == "side" and view in (front, top)) or (axis == "end" and view == left)
        if level:
            line(view, (box.XMin - over, 0.0), (box.XMax + over, 0.0))
            count += 1
        elif axis == "end" and view == top:
            line(view, (0.0, box.YMin - over), (0.0, box.YMax + over))
            count += 1
    return count, note


def _hoischen():
    """(preset, arrange function) of the Hoischen addon, or (None, None)."""
    try:
        from hoischen import arrange, preset
    except Exception:
        return None, None

    def run(page):
        try:
            from hoischen.commands import run_arrange
            return run_arrange(selection=[], page=page)
        except ImportError:      # no FreeCADGui (freecadcmd): the same steps without the redraw
            placed, skipped = arrange.plan(arrange.dimensions_for([], page))
            arrange.apply(placed)
            return {"placed": len(placed), "skipped": skipped}

    return preset, run


FILL_BY_NAME = {
    "drawing_title": "title", "title": "title", "fc-title": "title",
    "fc-sc": "scale", "scale": "scale",
    "fc-date": "date", "date_of_issue": "date", "date": "date",
    "page_number": "sheet", "sheet_number": "sheet", "fc-sh": "sheet",
    "fc-si": "size", "sheet_size": "size", "format": "size",
    "projekt": "project", "project": "project",
    "supplementary_title_1": "document",
    "author_name": "author", "creator": "author",
    "owner_1": "organization", "legal_owner_1": "organization",
}


def fill_title_block(template, autofill, values, keep_rest):
    """Write what we know into the editable texts. Returns (filled words, names left to fill in).

    keep_rest: leave the template's own text in the fields we do not know
    (the German template shows the field name there). Other templates show
    sample data ("Stainless steel", "A. Nemesis"), which must not get onto a
    real drawing, so those fields are emptied.
    """
    texts = dict(template.EditableTexts)
    filled, rest = [], []
    for name in sorted(texts):
        key = autofill.get(name) or FILL_BY_NAME.get(name.lower(), "")
        if key not in values:
            key = FILL_BY_NAME.get(name.lower(), "")
        value = values.get(key, "")
        if value:
            texts[name] = value
            if key not in filled:
                filled.append(key)
        else:
            rest.append(name)
            if not keep_rest:
                texts[name] = ""
    template.EditableTexts = texts
    words = {"sheet": "sheet number", "size": "sheet size", "organization": "owner"}
    return [words.get(k, k) for k in filled], rest


# ---- the tool --------------------------------------------------------------
def describe(sheet="A3", obj_name=""):
    """One line that says what make_drawing() would do now, for the user's yes (approval.py).

    Never raises. It changes nothing: it only looks which object would be drawn.
    """
    asked = str(sheet or "A3").strip() or "A3"
    wanted = str(obj_name or "").strip()
    try:
        size = _parse_sheet(asked)[0]
        doc = App.ActiveDocument
        obj = pick_object(doc, wanted)[0] if doc is not None and size is not None else None
    except Exception:
        size, obj = asked, None
    if size is None:
        return f"Make drawing: sheet {asked}. The tool does not know this sheet, so it will draw nothing."
    if obj is None:
        which = f"object {wanted}" if wanted else "the main object"
        return f"Make drawing: sheet {size}, {which}. No such solid is there now, so it will draw nothing."
    return f"Make drawing: sheet {size}, object {obj.Label} \u2192 drawings/{_file_name(obj.Label)}-{size}.pdf"


def make_drawing(project, sheet="A3", obj_name=""):
    """Workshop drawing of one object of the active document. Returns a text report.

    Never raises: what went wrong comes back as text that starts with "ERROR:".
    """
    doc = App.ActiveDocument
    try:
        return _make(doc, project, sheet, obj_name)
    except Exception as exc:
        try:
            if doc is not None:
                doc.abortTransaction()
        except Exception:
            pass
        return f"ERROR: the drawing could not be made: {type(exc).__name__}: {exc}"


def _parse_sheet(sheet):
    """(sheet, forced orientation or None, note or None) or (None, None, error text)."""
    text = str(sheet or "A3").strip().upper()
    force = None
    for word, orientation in (("PORTRAIT", "Portrait"), ("LANDSCAPE", "Landscape")):
        if word in text:
            force, text = orientation, text.replace(word, "")
    text = text.strip(" -_,")
    if text == "A5":
        return "A4", None, "FreeCAD has no A5 sheet, so A4 was used."
    if text not in SHEETS:
        return None, None, (f"ERROR: '{sheet}' is not a sheet this tool knows. "
                            f"Use one of {', '.join(SHEETS)}.")
    if force and (text != "A4" or find_template(text, force) is None):
        force = None
    return text, force, None


TECHDRAW_GROUPS = ("debug", "General", "HLR", "Decorations", "Dimensions", "Colors", "Files", "PAT",
                   "Standards", "Labels")


def _make_techdraw_groups():
    """Make TechDraw's settings groups now, in the main thread.

    TechDraw works out each view in a thread of its own, and each thread reads
    settings (the first one is "debug"). In a new FreeCAD profile the groups
    do not exist yet, and FreeCAD makes a group when it is first asked for:
    two threads that do that at the same moment can crash FreeCAD 1.1 (seen
    one time in the tests: a segmentation fault with both view threads in
    ParameterGrp::_GetGroup, called for "debug"). A group that exists is only
    read.
    """
    for name in TECHDRAW_GROUPS:
        App.ParamGet("User parameter:BaseApp/Preferences/Mod/TechDraw/" + name)


def _make(doc, project, sheet, obj_name):
    if doc is None:
        return ("ERROR: no document is open. Open or build a model first, "
                "then ask for the drawing again.")
    sheet, force, sheet_note = _parse_sheet(sheet)
    if sheet is None:
        return sheet_note
    obj, error = pick_object(doc, str(obj_name or "").strip())
    if obj is None:
        return error

    doc.recompute()
    shape = _global_shape(obj)
    box = _bound_box(shape)
    size3 = (box.XLength, box.YLength, box.ZLength)
    if max(size3) <= 1e-6:
        return f"ERROR: '{obj.Label}' has no size, so there is nothing to draw."
    turned = turned_axis(shape)
    direction, xdir, looking = front_view(size3, turned)
    up = _cross(direction, xdir)
    size = (_extent(size3, xdir), _extent(size3, up), _extent(size3, direction))
    iso = iso_extent(size3)

    if find_template(sheet, "Landscape") is None and find_template(sheet, "Portrait") is None:
        return (f"ERROR: this FreeCAD has no ISO 5457 template for {sheet} "
                f"(looked in {_template_dir()}). Try A3.")
    if force:
        data = read_template(find_template(sheet, force))
        plan = plan_layout(size, iso, data["frame"], data["block"])
        choice = (force, find_template(sheet, force), data, plan) if plan else None
    else:
        choice = choose_sheet(sheet, size, iso)
    if choice is None:
        return (f"ERROR: '{obj.Label}' ({' x '.join(_num(v) for v in size3)} mm) does not fit on {sheet}, "
                "not even at 1:1000.")
    orientation, tpl_path, tpl, plan = choice
    scale = plan["scale"]
    s = scale[0] / float(scale[1])

    notes = [sheet_note] if sheet_note else []
    _make_techdraw_groups()
    preset, arrange = _hoischen()
    if preset is not None:
        try:
            preset.apply_preferences("0.7" if sheet in ("A1", "A0") else "0.5")
        except Exception as exc:
            notes.append(f"The Hoischen preset could not be set ({exc}).")

    label = obj.Label
    page_label = PAGE_PREFIX + label
    own = not doc.HasPendingTransaction
    if own:
        doc.openTransaction("Make drawing")
    replaced = False
    for _ in range(5):
        _events()    # let the open page finish its redraws before its views go away
    for old in [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawPage") and o.Label == page_label]:
        remove_page(doc, old)
        replaced = True

    page = doc.addObject("TechDraw::DrawPage", "Drawing")
    template = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
    template.Template = tpl_path
    page.Template = template
    page.Label = page_label
    page.KeepUpdated = True     # or the views stay empty when the user has switched live updates off
    page.Scale = s
    try:
        page.ProjectionType = "First angle"
    except Exception:
        pass

    project_name = os.path.basename(os.path.normpath(project)) if project else ""
    values = {
        "title": label,
        "scale": scale_text(scale),
        "date": datetime.date.today().isoformat(),
        "sheet": "1 / 1",
        "size": sheet,
        "project": project_name or doc.Label,
        "document": doc.Label,
        "author": getattr(doc, "CreatedBy", "") or "",
        "organization": getattr(doc, "Company", "") or "",
    }
    german = os.path.basename(tpl_path).endswith("_ISO7200_DE.svg")
    filled, to_fill = fill_title_block(template, tpl["autofill"], values, keep_rest=german)

    group = doc.addObject("TechDraw::DrawProjGroup", "Views")
    page.addView(group)
    group.Source = [obj]
    group.ProjectionType = "First angle"
    group.ScaleType = "Page"
    front = group.addProjection("Front")
    # the other views take their directions from the front view when they are added
    group.Anchor.Direction = App.Vector(*direction)
    group.Anchor.XDirection = App.Vector(*xdir)
    top = group.addProjection("Top")
    left = group.addProjection("Left")
    group.AutoDistribute = False
    group.X, group.Y = plan["front"]
    top.X, top.Y = 0.0, plan["top_y"]
    left.X, left.Y = plan["left_x"], 0.0
    views = [front, top, left]

    iso_view = None
    if plan["iso"]:
        iso_view = doc.addObject("TechDraw::DrawViewPart", "Isometric")
        page.addView(iso_view)
        iso_view.Source = [obj]
        iso_view.Direction, iso_view.XDirection = iso_axes()
        if plan["iso"]["scale"] == scale:
            iso_view.ScaleType = "Page"
        else:
            iso_view.ScaleType = "Custom"
            iso_view.Scale = plan["iso"]["scale"][0] / float(plan["iso"]["scale"][1])
            iso_view.Caption = scale_text(plan["iso"]["scale"])
        iso_view.X, iso_view.Y = plan["iso"]["pos"]

    doc.recompute()
    ready = _wait_for(views + ([iso_view] if iso_view else []))

    made, skipped, arranged, centre_lines = [], [], None, 0
    if ready:
        dims = add_dimensions(doc, page, views, s, size)
        doc.recompute()
        _events()
        made, skipped = dims.check(), dims.skipped
        if own:
            doc.commitTransaction()     # the arrange step below is an undo step of its own
            own = False
        if made and arrange is not None:
            try:
                arranged = arrange(page)
            except Exception as exc:
                notes.append(f"The Hoischen addon could not arrange the dimensions ({exc}).")
        doc.recompute()
        _events()
        # centre lines last: they reach past the part, and the 10 mm are measured from the part
        doc.openTransaction("Centre lines")
        axis = None
        if turned is not None:
            axis = "side" if size3[turned] >= max(size3[i] for i in range(3) if i != turned) else "end"
        centre_lines, note = add_centre_lines(views, s, 0.35 if sheet in ("A1", "A0") else 0.25, axis)
        if note:
            notes.append(note)
        doc.commitTransaction()
        doc.recompute()
        _events()
    else:
        notes.append("TechDraw did not finish the views in time, so no dimensions and no centre lines "
                     "were added. Ask for the drawing again when FreeCAD is idle.")
    if own:
        doc.commitTransaction()

    problems = _check_layout(views, iso_view, s, tpl) if ready else []

    pdf = None
    if not project:
        notes.append("No project folder was given, so no PDF was written.")
    elif not App.GuiUp:
        notes.append("FreeCAD runs without its window, so no PDF was written: "
                     "the page is in the document, export it from the FreeCAD window.")
    else:
        try:
            pdf = _export_pdf(page, os.path.join(project, "drawings", f"{_file_name(label)}-{sheet}.pdf"))
        except Exception as exc:
            notes.append(f"The PDF could not be written ({exc}).")

    # ---- the report ----
    lines = [f"Drawing made: page \"{page.Label}\" in document {doc.Label}"
             + (" (it replaced the earlier page of that name)." if replaced else "."),
             f"Object: {label}, {' x '.join(_num(v) for v in size3)} mm (X x Y x Z).",
             f"Sheet: {sheet} {orientation.lower()}, template {os.path.basename(tpl_path)}.",
             f"Scale: {scale_text(scale)}, the largest ISO 5455 scale at which the views fit."]
    view_line = f"Views: front ({looking}), top, left; first-angle projection"
    if plan["iso"]:
        view_line += f"; isometric view {plan['iso']['where']}"
        if plan["iso"]["scale"] != scale:
            view_line += f" at {scale_text(plan['iso']['scale'])}"
    else:
        view_line += "; no isometric view (no room for it)"
    lines.append(view_line + ".")
    lines.append("Dimensions added: " + ("; ".join(made) if made else "none") + ".")
    lines.append(f"Centre lines added: {centre_lines} (on full circles and on the axis of a turned part)."
                 if centre_lines else "Centre lines added: none.")
    if preset is None:
        lines.append("Style: the Hoischen addon was not found, so FreeCAD's own TechDraw settings were used.")
    elif arranged is not None:
        lines.append("Style: Hoischen / DIN preset; dimension lines 10 mm from the part, then every 7 mm.")
    else:
        lines.append("Style: Hoischen / DIN preset.")
    lines.append("Title block filled: " + (", ".join(filled) if filled else "nothing") + ".")
    lines.append(f"PDF: {pdf}" if pdf else "PDF: not written.")
    lines += notes
    lines.append("Open points:")
    for p in problems:
        lines.append(f"- CHECK THE LAYOUT: {p}")
    for text in skipped:
        lines.append(f"- No dimension for {text}.")
    if to_fill:
        lines.append("- Title block, to fill in: " + ", ".join(to_fill)
                     + (" (the template's own words are still in these fields)." if german else "."))
    lines.append("- Only the overall sizes are dimensioned. Still missing: position and size of holes, "
                 "steps, radii, chamfers, threads.")
    lines.append("- No tolerances, surface finish or material on the drawing yet.")
    lines.append("- No hidden edges and no section views; no centre lines for slots, arcs and symmetry.")
    return "\n".join(lines)


def view_boxes(views, iso_view, scale):
    """Where each view's lines really are on the sheet: {name: (x0, y0, x1, y1)} in paper mm."""
    front, top, left = views
    gx = gy = 0.0
    for parent in front.InList:
        if parent.isDerivedFrom("TechDraw::DrawProjGroup"):
            gx, gy = float(parent.X), float(parent.Y)
    out = {}
    for name, view, ox, oy in (("front", front, gx, gy), ("top", top, gx, gy),
                               ("left", left, gx, gy), ("isometric", iso_view, 0.0, 0.0)):
        if view is None:
            continue
        box = _outline(_geometry(view)[1])
        if box is None:
            continue
        k = float(view.getScale()) if name == "isometric" else scale
        cx, cy = ox + float(view.X), oy + float(view.Y)
        out[name] = (cx + box.XMin * k, cy + box.YMin * k, cx + box.XMax * k, cy + box.YMax * k)
    return out


def _check_layout(views, iso_view, scale, tpl):
    """Words for every view that TechDraw did not draw where the plan put it (should never happen)."""
    problems = []
    for name, box in view_boxes(views, iso_view, scale).items():
        if not _fits([box], (tpl["frame"][0] - EDGE, tpl["frame"][1] - EDGE,
                             tpl["frame"][2] + EDGE, tpl["frame"][3] + EDGE), None):
            problems.append(f"the {name} view runs over the frame")
        elif tpl["block"] and not _fits([box], tpl["frame"], tpl["block"]):
            problems.append(f"the {name} view is too close to the frame or the title block")
    return problems


def _export_pdf(page, path):
    import TechDrawGui
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # TechDraw draws a page when its tab is opened; an unopened page exports without its views.
    page.ViewObject.doubleClicked()
    for _ in range(3):
        _events()
    if os.path.exists(path):
        os.remove(path)
    TechDrawGui.exportPageAsPdf(page, path)
    _events()
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise OSError("TechDraw wrote no file")
    return path
