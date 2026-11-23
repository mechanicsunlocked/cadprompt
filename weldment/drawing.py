"""ISO 2553 weld symbols on TechDraw pages, driven from the weld features.

TechDraw already has the pieces (DrawLeaderLine, DrawWeldSymbol with two
DrawTileWeld tiles, arrow side = row 0, other side = row -1).  We place the
leader on the weld, fill the tiles from the weld's properties and remember
which weld a symbol belongs to, so "Update symbols" can refresh them.
"""

import os

import FreeCAD as App
import Part
import TechDraw

from . import symbol as sym
from .feature import FilletWeld

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILLET_SYMBOL = os.path.join(HERE, "icons", "iso2553_fillet.svg")
DECIMAL_COMMA = True    # DIN drawings: a3,5


def view_point(view, point3d):
    """Where a 3D point lands on `view`, in model mm relative to the view's origin.

    TechDraw centres the projected shape on the view, so the projection of
    the shapes' centroid is the view origin.  A leader's X/Y are in these
    unscaled units (TechDraw applies the view scale when drawing), so no
    scaling here.
    """
    shapes = [o.Shape for o in view.Source if hasattr(o, "Shape")]
    if not shapes:
        raise ValueError(f"{view.Label} has no source shapes")
    centroid = TechDraw.findCentroid(Part.makeCompound(shapes), view.Direction)
    p = view.projectPoint(point3d) - view.projectPoint(centroid)
    return App.Vector(p.x, p.y, 0.0)


def spec_of(weld):
    return sym.WeldSpec(
        throat=weld.Throat.Value, length=weld.Length.Value, count=weld.Count,
        segment=weld.Segment.Value, gap=weld.Gap.Value, both_sides=weld.BothSides,
        staggered=getattr(weld, "Staggered", False),
        all_around=weld.AllAround, field=weld.FieldWeld, tail=weld.TailText)


def _tiles(weld_symbol):
    tiles = [o for o in weld_symbol.InList if o.isDerivedFrom("TechDraw::DrawTileWeld")]
    arrow = [t for t in tiles if t.TileRow == 0]
    other = [t for t in tiles if t.TileRow == -1]
    return (arrow[0] if arrow else None), (other[0] if other else None)


def fill_symbol(weld_symbol, weld):
    """Write the weld's ISO 2553 texts and symbols into an existing TechDraw weld symbol."""
    spec = spec_of(weld)
    arrow_text, other_text = sym.tile_texts(spec, DECIMAL_COMMA)
    arrow, other = _tiles(weld_symbol)
    if arrow is None:
        raise ValueError(f"{weld_symbol.Label} has no arrow-side tile")
    arrow.SymbolFile = FILLET_SYMBOL
    arrow.LeftText = arrow_text["left"]
    arrow.RightText = arrow_text["right"]
    if other is not None:
        if other_text:
            other.SymbolFile = FILLET_SYMBOL
            other.LeftText = other_text["left"]
            other.RightText = other_text["right"]
        else:
            other.SymbolFile = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Symbols", "Welding", "blankTile.svg")
            other.LeftText = ""
            other.RightText = ""
    weld_symbol.AllAround = spec.all_around
    weld_symbol.AlternatingWeld = bool(spec.both_sides and spec.staggered and spec.segment > 0)
    weld_symbol.FieldWeld = spec.field
    weld_symbol.TailText = spec.tail
    weld_symbol.Label = f"{weld.Label} symbol"


def add_symbol(weld, view, page=None, offset=(-12.0, 12.0), tail=15.0):
    """Leader from the weld on `view` to a horizontal reference line with the fillet symbol.

    offset: page mm from the arrow tip to the kink; the reference line runs
    `tail` mm further, horizontally, away from the arrow.
    """
    doc = weld.Document
    if page is None:
        pages = [o for o in view.InList if o.isDerivedFrom("TechDraw::DrawPage")]
        if not pages:
            raise ValueError(f"{view.Label} is not on a page")
        page = pages[0]
    anchor = view_point(view, FilletWeld.arrow_point(weld))

    leader = doc.addObject("TechDraw::DrawLeaderLine", "WeldLeader")
    leader.LeaderParent = view
    page.addView(leader)
    leader.StartSymbol = "Filled arrow"
    leader.EndSymbol = "None"
    leader.Scalable = False
    leader.AutoHorizontal = True
    leader.X = anchor.x
    leader.Y = anchor.y
    # WayPoints are scene coordinates: y grows downwards, unlike X/Y above.
    kink = App.Vector(offset[0], -offset[1], 0)
    end = App.Vector(offset[0] + (tail if offset[0] >= 0 else -tail), -offset[1], 0)
    leader.WayPoints = [App.Vector(0, 0, 0), kink, end]
    leader.Label = f"{weld.Label} leader"

    weld_symbol = doc.addObject("TechDraw::DrawWeldSymbol", "WeldSymbol")
    weld_symbol.Leader = leader
    page.addView(weld_symbol)
    weld_symbol.addProperty("App::PropertyLink", "Weld", "Weldment", "The weld this symbol describes")
    weld_symbol.Weld = weld
    fill_symbol(weld_symbol, weld)
    return weld_symbol


def symbols_of(doc, weld=None):
    out = []
    for o in doc.Objects:
        if o.isDerivedFrom("TechDraw::DrawWeldSymbol") and "Weld" in o.PropertiesList and o.Weld is not None:
            if weld is None or o.Weld == weld:
                out.append(o)
    return out


def update_symbols(doc):
    """Re-read every weld into its symbols (after the weld or the view changed)."""
    done = 0
    for s in symbols_of(doc):
        fill_symbol(s, s.Weld)
        leader = s.Leader
        if leader is not None and leader.LeaderParent is not None:
            anchor = view_point(leader.LeaderParent, FilletWeld.arrow_point(s.Weld))
            leader.X = anchor.x
            leader.Y = anchor.y
        done += 1
    return done
