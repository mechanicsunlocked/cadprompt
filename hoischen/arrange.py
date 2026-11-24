"""Move TechDraw dimensions onto their Hoischen lines.

Coordinates, as TechDraw keeps them (checked against TechDraw's own cascade
tool and against FreeCAD 1.1.3):
  * view geometry (getVisibleEdges) and dimension end points (getLinearPoints)
    are paper mm around the view centre with Y pointing down (Qt style);
  * a dimension's X/Y properties place its text label, paper mm around the
    view centre with Y pointing up.
The text sits beside its dimension line (above it, or left of it for vertical
dimensions), so the label goes to the line position plus that offset. The
offset depends on the height of the text box, and that depends on the font
and on how the system measures it (Linux, Windows and FreeCAD versions give
different numbers for the same font). So it is read from the drawing as
TechDraw has it on screen (measured_offsets), and worked out from a fixed
factor only where nothing is drawn yet (text_offset).
verify() reads back where TechDraw drew each line, for tests.
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


def _text_style(dim):
    """(style, font, font size, gap between line and text box) of a dimension."""
    vo = getattr(dim, "ViewObject", None) if App.GuiUp else None
    p = App.ParamGet(TD_DIMS)
    if vo is not None:
        return (str(vo.StandardAndStyle), str(getattr(vo, "Font", "")), _value(vo.Fontsize),
                float(vo.LineSpacingFactorISO) * _value(vo.LineWidth))
    style = ["ISO Oriented", "ISO Referencing", "ASME Inlined", "ASME Referencing"][p.GetInt("StandardAndStyle", 0)]
    from .preset import GROUPS  # no GUI: take the narrow line of the chosen group
    narrow = GROUPS.get(App.ParamGet(OWN).GetString("LineGroup", "0.7"), GROUPS["0.7"])["narrow"]
    return style, "", p.GetFloat("FontSize", 5.0), p.GetFloat("LineSpacingFactorISO", 2.0) * narrow


def text_offset(dim):
    """Distance from the dimension line to the centre of its text, paper mm, from a fixed factor."""
    style, _font, height, gap = _text_style(dim)
    if "Inlined" in style:
        return 0.0
    # TechDraw puts the line half a text box plus the gap away from the label
    # centre. Measured on FreeCAD 1.1.3 with osifont (the default ISO 3098
    # look-alike): the text box is 1.676 x the font size tall, at 2.5 mm and at
    # 5 mm alike, so the factor is 0.838. Another font may need another
    # factor: set TextBoxFactor under Mod/Hoischen.
    factor = App.ParamGet(OWN).GetFloat("TextBoxFactor", 0.838)
    return gap + factor * height


def measured_offsets(dims):
    """{dimension name: text offset} as TechDraw draws it now, for the dimensions where that is known.

    A dimension that is on screen shows its own offset: the label position
    minus the position of the line. It is not the same for all: "Ø30" has a
    taller box than "30". Half a text box is between 0.4 and 1.0 times the
    font size with every font we have seen. A number outside that, or far
    from the others with the same text style, is a drawing that is not up to
    date; such a dimension, and one that is not drawn yet, gets the middle
    value of its style. With a set TextBoxFactor the user's number is used
    and nothing is measured.
    """
    if not App.GuiUp or App.ParamGet(OWN).GetFloat("TextBoxFactor", 0.0) > 0:
        return {}
    own, seen = {}, {}                       # name -> offset; text style -> the offsets read from the screen
    for d in dims:
        style, _font, height, gap = key = _text_style(d)
        if "Inlined" in style or height <= 0:
            continue
        axis = "x" if d.Type == "DistanceX" else "y"
        line = drawn_line(d, axis)
        if line is None:
            continue
        offset = (_value(d.Y) - line) if axis == "x" else (line - _value(d.X))
        if 0.4 * height <= offset - gap <= 1.0 * height:
            own[d.Name] = offset
            seen.setdefault(key, []).append(offset)
    out = {}
    for d in dims:
        key = _text_style(d)
        if key not in seen:
            continue
        middle = sorted(seen[key])[len(seen[key]) // 2]
        mine = own.get(d.Name)
        out[d.Name] = mine if mine is not None and abs(mine - middle) <= 0.1 * key[2] else middle
    return out


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
    measured = measured_offsets([d for d in dims if is_linear(d)])

    def offset(d):
        return measured[d.Name] if d.Name in measured else text_offset(d)

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
            result[name] = (d, middle, line + offset(d), "x", line)
        for name, (line, middle, _row) in L.layout(vert, x_lo, x_hi, first, pitch).items():
            d = owner[name]
            result[name] = (d, line - offset(d), middle, "y", line)
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
    scale = 1.0
    if view is not None:
        try:
            scale = float(view.getScale())       # the scale in use: a view can take it from its page or group
        except Exception:
            scale = _value(view.Scale)
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
