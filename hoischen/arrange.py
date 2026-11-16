"""Move TechDraw dimensions onto their Hoischen lines.

Coordinates, as TechDraw keeps them (checked against TechDraw's own cascade
tool and against FreeCAD 1.1.3):
  * view geometry (getVisibleEdges) and dimension end points (getLinearPoints)
    are paper mm around the view centre with Y pointing down (Qt style);
  * a dimension's X/Y properties place its text label, paper mm around the
    view centre with Y pointing up.
The text sits beside its dimension line (above it, or left of it for vertical
dimensions), so the label goes to the line position plus that offset (see
text_offset). verify() reads back where TechDraw drew each line, for tests.
"""

import FreeCAD as App

from . import layout as L

LINEAR = ("DistanceX", "DistanceY")
OWN = "User parameter:BaseApp/Preferences/Mod/Hoischen"
TD_DIMS = "User parameter:BaseApp/Preferences/Mod/TechDraw/Dimensions"


def spacing():
    p = App.ParamGet(OWN)
    return p.GetFloat("FirstOffset", L.FIRST_OFFSET), p.GetFloat("Pitch", L.PITCH)


def _value(v):
    return float(getattr(v, "Value", v))


def is_linear(obj):
    return obj.isDerivedFrom("TechDraw::DrawViewDimension") and obj.Type in LINEAR


def parent_view(dim):
    for obj, _sub in dim.References2D or []:
        if obj is not None and obj.isDerivedFrom("TechDraw::DrawViewPart"):
            return obj
    for obj in dim.InList:
        if obj.isDerivedFrom("TechDraw::DrawViewPart"):
            return obj
    return None


def outline(view):
    """(x_lo, x_hi, y_lo, y_hi) of the visible geometry, paper mm, Y up. None if not computed yet."""
    edges = view.getVisibleEdges()
    if not edges:
        return None
    bb = edges[0].BoundBox
    for e in edges[1:]:
        bb.add(e.BoundBox)
    return bb.XMin, bb.XMax, -bb.YMax, -bb.YMin


def text_offset(dim):
    """Distance from the dimension line to the centre of its text, paper mm."""
    vo = getattr(dim, "ViewObject", None) if App.GuiUp else None
    p = App.ParamGet(TD_DIMS)
    if vo is not None:
        style = str(vo.StandardAndStyle)
        height = _value(vo.Fontsize)
        gap = float(vo.LineSpacingFactorISO) * _value(vo.LineWidth)
    else:
        style = ["ISO Oriented", "ISO Referencing", "ASME Inlined", "ASME Referencing"][p.GetInt("StandardAndStyle", 0)]
        height = p.GetFloat("FontSize", 5.0)
        from .preset import GROUPS  # no GUI: take the narrow line of the chosen group
        narrow = GROUPS.get(App.ParamGet(OWN).GetString("LineGroup", "0.7"), GROUPS["0.7"])["narrow"]
        gap = p.GetFloat("LineSpacingFactorISO", 2.0) * narrow
    if "Inlined" in style:
        return 0.0
    # TechDraw puts the line half a text box plus the gap away from the label
    # centre. Measured on FreeCAD 1.1.3 with osifont (the default ISO 3098
    # look-alike): the text box is 1.676 x the font size tall, at 2.5 mm and at
    # 5 mm alike, so the factor is 0.838. Another font may need another
    # factor: set TextBoxFactor under Mod/Hoischen.
    factor = App.ParamGet(OWN).GetFloat("TextBoxFactor", 0.838)
    return gap + factor * height


def plan(dims, first=None, pitch=None):
    """Work out the new label position of every linear dimension.

    Returns ({dim_name: (dim, X, Y, axis, line)}, [skipped messages]).
    axis is "x" for horizontal dimensions, "y" for vertical ones; line is the
    target coordinate of the dimension line across that axis.
    """
    if first is None or pitch is None:
        f, p = spacing()
        first = f if first is None else first
        pitch = p if pitch is None else pitch

    by_view, skipped = {}, []
    for d in dims:
        if not is_linear(d):
            continue
        v = parent_view(d)
        if v is None:
            skipped.append(f"{d.Label}: no view found")
            continue
        by_view.setdefault(v.Name, (v, []))[1].append(d)

    result = {}
    for view, group in by_view.values():
        box = outline(view)
        if box is None:
            skipped.append(f"{view.Label}: view has no geometry yet, recompute first")
            continue
        x_lo, x_hi, y_lo, y_hi = box
        x_mid, y_mid = (x_lo + x_hi) / 2.0, (y_lo + y_hi) / 2.0

        horiz, vert, owner = [], [], {}
        for d in group:
            try:
                p1, p2 = d.getLinearPoints()
            except Exception as exc:  # broken references
                skipped.append(f"{d.Label}: {exc}")
                continue
            if d.Type == "DistanceX":
                span = L.Span(d.Name, p1.x, p2.x, +1 if _value(d.Y) >= y_mid else -1)
                target = horiz
            else:
                span = L.Span(d.Name, -p1.y, -p2.y, +1 if _value(d.X) >= x_mid else -1)
                target = vert
            if span.length <= 1e-6:
                skipped.append(f"{d.Label}: zero length")
                continue
            target.append(span)
            owner[d.Name] = d

        for name, (line, middle, _row) in L.layout(horiz, y_lo, y_hi, first, pitch).items():
            d = owner[name]
            result[name] = (d, middle, line + text_offset(d), "x", line)
        for name, (line, middle, _row) in L.layout(vert, x_lo, x_hi, first, pitch).items():
            d = owner[name]
            result[name] = (d, line - text_offset(d), middle, "y", line)
    return result, skipped


def apply(placed):
    for d, x, y, _axis, _line in placed.values():
        # TechDraw's own snapping would pull a label onto its idea of a
        # chain/cascade position (measured: up to ~1 mm off ours), so a
        # dimension we placed by the book is told not to snap.
        vo = getattr(d, "ViewObject", None) if App.GuiUp else None
        if vo is not None and hasattr(vo, "AllowSnap"):
            vo.AllowSnap = False
        d.X = x
        d.Y = y


def drawn_line(dim, axis):
    """Where TechDraw actually drew the dimension line, from its arrow tips. GUI only."""
    try:
        a, b = dim.getArrowPositions()
    except Exception:
        return None
    if a.Length < 1e-9 and b.Length < 1e-9:
        return None
    view = parent_view(dim)
    scale = _value(view.Scale) if view is not None else 1.0
    return (a.y if axis == "x" else a.x) * scale


def verify(placed, tolerance=0.05):
    """Read back where TechDraw drew each line and report any that missed. GUI only.

    Read-only on purpose: within one macro the page does not always redraw a
    follow-up move, so read-backs are good for a report, not for steering.
    Returns {name: (target, drawn or None)} for the ones off by more than tolerance.
    """
    misses = {}
    for name, (d, _x, _y, axis, line) in placed.items():
        seen = drawn_line(d, axis)
        if seen is None or abs(seen - line) > tolerance:
            misses[name] = (line, seen)
    return misses


def dimensions_for(selection, page=None):
    """Dimensions to arrange: picked dimensions, all of a picked view's, or the whole page."""
    picked = [o for o in selection if is_linear(o)]
    if picked:
        return picked
    views = [o for o in selection if o.isDerivedFrom("TechDraw::DrawViewPart")]
    if views:
        names = {v.Name for v in views}
        out = []
        for v in views:
            for o in v.InList:
                if is_linear(o) and (parent_view(o) is not None and parent_view(o).Name in names) and o not in out:
                    out.append(o)
        return out
    pages = [o for o in selection if o.isDerivedFrom("TechDraw::DrawPage")] or ([page] if page else [])
    out = []
    for pg in pages:
        for o in pg.Views:
            if is_linear(o) and o not in out:
                out.append(o)
    return out
