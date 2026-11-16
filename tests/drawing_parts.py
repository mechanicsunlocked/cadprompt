"""Shared by the drawing tests: the test parts, and what the German sheets look like.

Each part builder closes every open document, makes a new one and returns
(document, object, (size x, size y, size z)).
"""

import FreeCAD as App
import Part
import Sketcher

# German ISO 7200 templates of FreeCAD 1.1, paper mm from the lower left corner:
# (sheet, orientation): (frame, title block). The block is 180 x 40 in the corner.
GERMAN = {
    ("A4", "Portrait"): ((20, 10, 200, 287), (20, 10, 200, 50)),
    ("A4", "Landscape"): ((20, 10, 287, 200), (107, 10, 287, 50)),
    ("A3", "Landscape"): ((20, 10, 410, 287), (230, 10, 410, 50)),
    ("A2", "Landscape"): ((20, 10, 584, 410), (404, 10, 584, 50)),
    ("A1", "Landscape"): ((20, 10, 831, 584), (651, 10, 831, 50)),
    ("A0", "Landscape"): ((20, 10, 1179, 831), (999, 10, 1179, 50)),
}


def overlap(a, b, gap=0.0):
    """True when two rectangles (x0, y0, x1, y1) are closer than gap."""
    return a[2] > b[0] - gap and a[0] < b[2] + gap and a[3] > b[1] - gap and a[1] < b[3] + gap


def inside(r, frame, margin=0.0):
    return (r[0] >= frame[0] + margin - 1e-6 and r[1] >= frame[1] + margin - 1e-6
            and r[2] <= frame[2] - margin + 1e-6 and r[3] <= frame[3] - margin + 1e-6)


def _new(name):
    for d in list(App.listDocuments()):
        App.closeDocument(d)
    return App.newDocument(name)


def plate():
    """100 x 50 x 10 with four 6 mm holes, as a Part cut. The box and the holes stay visible."""
    doc = _new("PlateDoc")
    blank = doc.addObject("Part::Box", "Blank")
    blank.Length, blank.Width, blank.Height = 100, 50, 10
    holes = []
    for i, (x, y) in enumerate(((10, 10), (90, 10), (10, 40), (90, 40))):
        c = doc.addObject("Part::Cylinder", f"Hole{i + 1}")
        c.Radius, c.Height = 3, 10
        c.Placement.Base = App.Vector(x, y, 0)
        holes.append(c)
    drill = doc.addObject("Part::MultiFuse", "Holes")
    drill.Shapes = holes
    cut = doc.addObject("Part::Cut", "Plate")
    cut.Base, cut.Tool = blank, drill
    doc.recompute()
    return doc, cut, (100.0, 50.0, 10.0)


def shaft():
    """Three cylinders along Z, fused: diameter 20 x 40, 30 x 60, 16 x 30."""
    doc = _new("ShaftDoc")
    parts, z = [], 0
    for i, (r, length) in enumerate(((10, 40), (15, 60), (8, 30))):
        c = doc.addObject("Part::Cylinder", f"Step{i + 1}")
        c.Radius, c.Height = r, length
        c.Placement.Base = App.Vector(0, 0, z)
        z += length
        parts.append(c)
    fuse = doc.addObject("Part::MultiFuse", "Shaft")
    fuse.Shapes = parts
    fuse.Refine = True
    doc.recompute()
    return doc, fuse, (30.0, 30.0, 130.0)


def bracket():
    """A PartDesign Body: an L profile 60 x 50 (legs 10 thick) on the XZ plane, padded 40."""
    doc = _new("BracketDoc")
    body = doc.addObject("PartDesign::Body", "Bracket")
    sketch = body.newObject("Sketcher::SketchObject", "Profile")
    xz = [o for o in body.Origin.OriginFeatures if o.Role == "XZ_Plane"][0]
    sketch.AttachmentSupport = [(xz, "")]
    sketch.MapMode = "FlatFace"
    pts = [(0, 0), (60, 0), (60, 10), (10, 10), (10, 50), (0, 50)]
    for i, p in enumerate(pts):
        q = pts[(i + 1) % len(pts)]
        sketch.addGeometry(Part.LineSegment(App.Vector(p[0], p[1], 0), App.Vector(q[0], q[1], 0)))
    for i in range(len(pts)):
        sketch.addConstraint(Sketcher.Constraint("Coincident", i, 2, (i + 1) % len(pts), 1))
    pad = body.newObject("PartDesign::Pad", "Pad")
    pad.Profile = sketch
    pad.Length = 40
    doc.recompute()
    return doc, body, (60.0, 40.0, 50.0)
