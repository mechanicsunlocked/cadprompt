"""Weld list and cut list: a spreadsheet in the document, a table on the drawing.

Weld list: one row per weld feature (what the shop welds, what it costs).
Cut list: one row per distinct member (profile, length, end angles), with
the quantity.  A member is any solid a weld touches (or what you select).
FrameForge profiles report their own Family / SizeName / Length / cutting
angles / weight; anything else is measured from its shape: longest
bounding-box side as the cut length, the end faces' angles to that axis as
miter angles, volume x 7.85 as the mass.
"""

import math
import re

import FreeCAD as App

from . import geometry
from . import symbol as sym
from .feature import all_welds

WELD_HEADER = ["Weld", "Type", "a", "z", "Length", "Pattern", "Sides", "Seam", "Deposit g"]
WELD_WIDTHS = [30, 15, 10, 10, 15, 28, 18, 15, 18]      # mm on the drawing
CUT_HEADER = ["Pos", "Qty", "Profile", "Length", "Cut A", "Cut B", "Mass kg", "Members"]
CUT_WIDTHS = [10, 10, 38, 15, 13, 13, 16, 50]
ROW_HEIGHT = 7.0          # mm, like a Hoischen dimension line pitch
PX_PER_MM = 96.0 / 25.4   # TechDraw's spreadsheet view reads sheet sizes and text size in 96 dpi pixels


# --- welds --------------------------------------------------------------------

def weld_rows(welds, decimal_comma=True):
    rows = []
    for w in welds:
        spec = sym.WeldSpec(throat=w.Throat.Value, length=w.Length.Value, count=w.Count, segment=w.Segment.Value,
                            gap=w.Gap.Value, both_sides=w.BothSides, staggered=getattr(w, "Staggered", False))
        pattern = sym.length_text(spec, decimal_comma) or "full"
        sides = "2" if w.BothSides else "1"
        if w.BothSides and spec.staggered and spec.segment > 0:
            sides = "2 staggered"
        rows.append([w.Label, "fillet", round(w.Throat.Value, 2), round(w.Leg.Value, 2), round(w.WeldLength.Value, 1),
                     pattern, sides, round(w.SeamLength.Value, 1), round(w.DepositMass * 1000, 1)])
    return rows


def make_weld_list(doc, name="WeldList"):
    rows = weld_rows(all_welds(doc))
    total = sum(r[-1] for r in rows)
    rows.append(["Total", "", "", "", round(sum(r[4] for r in rows), 1), "", "", "", round(total, 1)])
    return write_sheet(doc, name, WELD_HEADER, rows, WELD_WIDTHS)


# --- members ------------------------------------------------------------------

def is_frameforge(obj):
    return all(hasattr(obj, p) for p in ("Family", "SizeName", "Length"))


def member_info(obj):
    """dict(label, profile, length, cut_a, cut_b, mass) for one member."""
    if is_frameforge(obj):
        family = obj.Family
        if getattr(obj, "CustomProfile", None):
            family = getattr(getattr(obj.CustomProfile, "LinkedObject", obj.CustomProfile), "Label", "custom")
        return {"label": obj.Label, "profile": f"{family} {obj.SizeName}".strip(),
                "length": round(obj.Length.Value, 1),
                "cut_a": _angle(getattr(obj, "CuttingAngleA", 0.0)),
                "cut_b": _angle(getattr(obj, "CuttingAngleB", 0.0)),
                "mass": round(float(getattr(obj, "ApproxWeight", 0.0) or 0.0), 3)}


    shape = obj.Shape
    bb = shape.BoundBox
    sizes = [bb.XLength, bb.YLength, bb.ZLength]
    axis_index = sizes.index(max(sizes))
    axis = [0.0, 0.0, 0.0]
    axis[axis_index] = 1.0
    section = sorted(s for i, s in enumerate(sizes) if i != axis_index)
    ends = _end_angles(shape, axis)
    return {"label": obj.Label, "profile": f"{section[1]:g}x{section[0]:g}", "length": round(max(sizes), 1),
            "cut_a": ends[0], "cut_b": ends[1], "mass": round(shape.Volume * geometry.STEEL_DENSITY_KG_PER_MM3, 3)}


def _angle(value):
    """FrameForge stores cutting angles as strings ('45', '45.0°', ''); 0 when empty."""
    if isinstance(value, (int, float)):
        return round(float(value), 1)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    return round(float(m.group(0).replace(",", ".")), 1) if m else 0.0


def _end_angles(shape, axis):
    """Angles of the two end faces (the planar faces furthest along the axis, each end) to the axis."""
    a = App.Vector(*axis)
    planar = [f for f in shape.Faces if f.Surface.__class__.__name__ == "Plane"]
    if not planar:
        return 0.0, 0.0
    # candidates for an end face: normal not perpendicular to the axis
    ends = [f for f in planar if abs(f.normalAt(0, 0).dot(a)) > 0.05]
    if not ends:
        return 0.0, 0.0
    lo = min(ends, key=lambda f: f.CenterOfMass.dot(a))
    hi = max(ends, key=lambda f: f.CenterOfMass.dot(a))
    return (round(geometry.miter_angle(axis, tuple(lo.normalAt(0, 0))), 1),
            round(geometry.miter_angle(axis, tuple(hi.normalAt(0, 0))), 1))


def welded_members(doc):
    """The parts the welds touch, in document order."""
    seen = []
    for w in all_welds(doc):
        for link in (w.FaceA, w.FaceB):
            if link and link[0] not in seen:
                seen.append(link[0])
    return seen


def cut_rows(members):
    rows = [member_info(m) for m in members if hasattr(m, "Shape") and not m.Shape.isNull()]
    grouped = geometry.group_rows(rows, ("profile", "length", "cut_a", "cut_b"))
    out = []
    for i, r in enumerate(grouped, 1):
        out.append([i, r["qty"], r["profile"], r["length"], r["cut_a"], r["cut_b"], round(r["mass"] * r["qty"], 3),
                    ", ".join(r["labels"])])
    return out


def make_cut_list(doc, members=None, name="CutList"):
    if members is None:
        members = welded_members(doc)
    rows = cut_rows(members)
    rows.append(["", sum(r[1] for r in rows), "Total", "", "", "", round(sum(r[6] for r in rows), 3), ""])
    return write_sheet(doc, name, CUT_HEADER, rows, CUT_WIDTHS)


# --- spreadsheet and table ------------------------------------------------------

def _col(i):
    return chr(ord("A") + i)


def write_sheet(doc, name, header, rows, widths=None):
    sheet = doc.getObject(name)
    if sheet is None or not sheet.isDerivedFrom("Spreadsheet::Sheet"):
        sheet = doc.addObject("Spreadsheet::Sheet", name)
        sheet.Label = name
    sheet.clearAll()
    for c, text in enumerate(header):
        sheet.set(f"{_col(c)}1", str(text))
        sheet.setStyle(f"{_col(c)}1", "bold")
        if widths:
            sheet.setColumnWidth(_col(c), int(round(widths[c] * PX_PER_MM)))
    for r in range(1, len(rows) + 2):
        sheet.setRowHeight(str(r), int(round(ROW_HEIGHT * PX_PER_MM)))
    for r, row in enumerate(rows, 2):
        for c, value in enumerate(row):
            if value == "" or value is None:
                continue
            sheet.set(f"{_col(c)}{r}", str(value) if isinstance(value, str) else f"={value}")
    sheet.setAlignment(f"A1:{_col(len(header) - 1)}{len(rows) + 1}", "center|vcenter")
    sheet.recompute()
    return sheet


def add_table(page, sheet, x=None, y=None, text_size=2.5):
    """Put the sheet on the page as a TechDraw table, bottom right unless told otherwise."""
    doc = page.Document
    rows = 1
    while sheet.getContents(f"A{rows + 1}") or sheet.getContents(f"B{rows + 1}"):
        rows += 1
    cols = 1
    while sheet.getContents(f"{_col(cols)}1"):
        cols += 1
    table = doc.addObject("TechDraw::DrawViewSpreadsheet", f"{sheet.Name}Table")
    page.addView(table)
    table.Source = sheet
    table.CellStart = "A1"
    table.CellEnd = f"{_col(cols - 1)}{rows}"
    table.TextSize = text_size * PX_PER_MM
    table.X = x if x is not None else page.PageWidth * 0.72
    table.Y = y if y is not None else page.PageHeight * 0.30
    table.Label = f"{sheet.Label} table"
    return table
