# RUN: freecad-gui
"""Tests of cadprompt/drawing.py in FreeCAD with its window (no display needed):

    FREECAD_USER_HOME=<fresh folder> QT_QPA_PLATFORM=offscreen freecad tests/test_drawing_gui.py

The result goes to $DRAWING_REPORT (default: <FREECAD_USER_HOME>/test_drawing_gui.txt),
the projects with their PDFs to $DRAWING_OUT (default: <FREECAD_USER_HOME>/projects).
tests/drawing_run.py sets all of this up, runs it and shows the result.

The fresh folder must hold the user.cfg that drawing_run.py writes: with the
offscreen platform FreeCAD 1.1 locks up when it shows a notification, and its
first-start dialog would wait for a click forever.
"""

import datetime
import os
import re
import sys
import traceback
import unittest

if not os.environ.get("FREECAD_USER_HOME"):
    os._exit(2)

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(os.getcwd(), "tests")
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
HOISCHEN = REPO
sys.path.insert(0, HOISCHEN)

import FreeCAD as App                                   # noqa: E402
import FreeCADGui as Gui                                # noqa: E402
import Part                                             # noqa: E402
from PySide import QtCore, QtWidgets                    # noqa: E402

from cadprompt import drawing as D                      # noqa: E402
import drawing_parts                                    # noqa: E402
from drawing_parts import GERMAN, inside, overlap       # noqa: E402

REPORT = os.environ.get("DRAWING_REPORT") or os.path.join(os.environ["FREECAD_USER_HOME"], "test_drawing_gui.txt")
OUT = os.environ.get("DRAWING_OUT") or os.path.join(os.environ["FREECAD_USER_HOME"], "projects")
MM_TO_PT = 72.0 / 25.4


def pages(doc):
    return [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawPage")]


def tabs(title):
    """Open tabs with this title."""
    return [w for w in Gui.getMainWindow().findChildren(QtWidgets.QMdiSubWindow)
            if w.isVisible() and w.windowTitle().replace("[*]", "") == title]


def view_rect(group, item, size3, s):
    """Where a view must be, from its position, the scale and the model's bounding box."""
    x = tuple(item.XDirection)
    up = D._cross(tuple(item.Direction), x)
    w = sum(abs(a) * b for a, b in zip(x, size3)) * s
    h = sum(abs(a) * b for a, b in zip(up, size3)) * s
    cx, cy = float(group.X) + float(item.X), float(group.Y) + float(item.Y)
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def media_box(path):
    """Page size of a PDF in mm, or None."""
    with open(path, "rb") as f:
        m = re.search(rb"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]", f.read())
    if not m:
        return None
    x0, y0, x1, y1 = (float(v) for v in m.groups())
    return ((x1 - x0) / MM_TO_PT, (y1 - y0) / MM_TO_PT)


class Case(unittest.TestCase):
    def project(self, name):
        return os.path.join(OUT, name)

    def check_drawing(self, doc, label, size3, sheet, out, values, hoischen=True, pdf=True):
        """All a finished drawing must be. Returns (page, orientation, scale)."""
        self.assertFalse(out.startswith("ERROR"), out)
        self.assertNotIn("CHECK THE LAYOUT", out)
        found = [p for p in pages(doc) if p.Label == "Drawing " + label]
        self.assertEqual(len(found), 1, [p.Label for p in pages(doc)])
        page = found[0]
        template = os.path.basename(page.Template.Template)
        m = re.match(r"(A\d)_(Portrait|Landscape)_ISO7200_DE\.svg$", template)
        self.assertTrue(m and m.group(1) == sheet, template)
        orientation = m.group(2)
        frame, block = GERMAN[(sheet, orientation)]
        self.assertIn(f"Sheet: {sheet} {orientation.lower()}, template {template}.", out)

        # the scale is a standard one, on the page and on every view
        s = float(page.Scale)
        scale = [sc for sc in D.SCALES if abs(sc[0] / sc[1] - s) < 1e-9]
        self.assertEqual(len(scale), 1, f"{s} is not an ISO 5455 scale")
        scale = scale[0]
        self.assertIn(f"Scale: {scale[0]}:{scale[1]},", out)

        # one projection group with front, top and left view, first angle
        groups = [v for v in page.Views if v.isDerivedFrom("TechDraw::DrawProjGroup")]
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.ProjectionType, "First angle")
        items = {v.Type: v for v in group.Views}
        self.assertEqual(sorted(items), ["Front", "Left", "Top"])
        self.assertEqual(group.Anchor, items["Front"])
        self.assertLess(float(items["Top"].Y), 0)       # first angle: top view under the front view
        self.assertGreater(float(items["Left"].X), 0)   # left view right of it
        self.assertAlmostEqual(float(items["Top"].X), 0)
        self.assertAlmostEqual(float(items["Left"].Y), 0)

        # every view inside the frame and off the title block, from position, scale and model size
        rects = {}
        for kind, item in items.items():
            self.assertAlmostEqual(float(item.getScale()), s)
            r = rects[kind] = view_rect(group, item, size3, s)
            self.assertTrue(inside(r, frame), f"{kind} view {r} leaves the frame {frame}")
            self.assertFalse(overlap(r, block), f"{kind} view {r} is on the title block {block}")
            self.assertTrue(inside(r, frame, 5.0 - 1e-6), f"{kind} view {r} touches the frame")
            self.assertFalse(overlap(r, block, 5.0 - 1e-6), f"{kind} view {r} touches the title block")
        for a in rects:
            for b in rects:
                if a < b:
                    self.assertFalse(overlap(rects[a], rects[b], 5.0), f"{a} and {b} view too close")
        # ... and that is where TechDraw really drew the lines
        views = [items["Front"], items["Top"], items["Left"]]
        isos = [v for v in page.Views if v.TypeId == "TechDraw::DrawViewPart"]
        self.assertLessEqual(len(isos), 1)
        drawn = D.view_boxes(views, isos[0] if isos else None, s)
        for kind in ("Front", "Top", "Left"):
            for got, want in zip(drawn[kind.lower()], rects[kind]):
                self.assertAlmostEqual(got, want, delta=0.01, msg=f"{kind} view drawn at {drawn[kind.lower()]}")
        if isos:
            r = drawn["isometric"]
            self.assertTrue(inside(r, frame, 5.0 - 1e-6), f"isometric view {r} leaves the frame")
            self.assertFalse(overlap(r, block, 5.0 - 1e-6), f"isometric view {r} is on the title block")
            for kind in rects:
                self.assertFalse(overlap(r, rects[kind], 5.0), f"isometric view on the {kind} view")
            self.assertIn("isometric view", out)
        else:
            self.assertIn("no isometric view", out)

        # dimensions: the right numbers, on their Hoischen lines, on the sheet
        dims = [v for v in page.Views if v.isDerivedFrom("TechDraw::DrawViewDimension")]
        self.assertEqual(sorted(round(abs(d.getRawValue()), 4) for d in dims), sorted(values), out)
        if hoischen:
            from hoischen import arrange
        for d in dims:
            view = [o for o, _sub in d.References2D][0]
            cx, cy = float(group.X) + float(view.X), float(group.Y) + float(view.Y)
            spot = (cx + float(d.X) - 3, cy + float(d.Y) - 3, cx + float(d.X) + 3, cy + float(d.Y) + 3)
            self.assertTrue(inside(spot, frame), f"{d.Name} number at {spot} leaves the frame")
            self.assertFalse(overlap(spot, block), f"{d.Name} number is on the title block")
            for kind in rects:
                if d.Type != "Diameter":
                    self.assertFalse(overlap(spot, rects[kind]), f"{d.Name} number is on the {kind} view")
            if hoischen and d.Type in ("DistanceX", "DistanceY"):
                # 10 mm from the part itself (not from the ends of the centre lines) to the dimension line
                r = rects[view.Type]
                if d.Type == "DistanceX":       # above the view
                    line, axis = r[3] - cy + 10, "x"
                    offset = float(d.Y) - line
                else:                            # left of the view
                    line, axis = r[0] - cx - 10, "y"
                    offset = line - float(d.X)
                # The number sits beside its line by half a text box plus the gap. How tall the box is
                # depends on the font and the system, so the line itself is what is checked.
                height, gap = float(d.ViewObject.Fontsize), 0.5
                self.assertTrue(gap + 0.4 * height <= offset <= gap + 1.0 * height, f"{d.Name}: number {offset} from its line")
                self.assertAlmostEqual(arrange.drawn_line(d, axis), line, delta=0.05, msg=d.Name)
                self.assertAlmostEqual(float(d.ViewObject.Fontsize), 2.5 if sheet not in ("A1", "A0") else 3.5)
        if hoischen:
            self.assertIn("Style: Hoischen / DIN preset", out)

        # title block
        texts = dict(page.Template.EditableTexts)
        self.assertEqual(texts["drawing_title"], label)
        self.assertEqual(texts["fc-sc"], f"{scale[0]}:{scale[1]}")
        self.assertEqual(texts["fc-date"], datetime.date.today().isoformat())
        self.assertEqual(texts["page_number"], "1 / 1")
        self.assertEqual(texts["Sachnummer"], "Sachnummer")      # left as the template has it
        self.assertIn("Title block filled: title, date, scale, sheet number, project.", out)
        self.assertIn("- Title block, to fill in: Genehmiger, Sachnummer, author_name, document_type, owner_1", out)
        self.assertIn("No tolerances", out)

        # the PDF
        if pdf:
            path = os.path.join(self.project(label), "drawings", f"{label}-{sheet}.pdf")
            self.assertIn(f"PDF: {path}", out)
            self.assertEqual(texts["projekt"], label)             # the project folder's name
            self.assertTrue(os.path.isfile(path), path)
            self.assertGreater(os.path.getsize(path), 5000)
            with open(path, "rb") as f:
                self.assertEqual(f.read(4), b"%PDF")
            box = media_box(path)
            if box:
                paper = (frame[2] + 10, frame[3] + 10)
                self.assertAlmostEqual(box[0], paper[0], delta=1.0)
                self.assertAlmostEqual(box[1], paper[1], delta=1.0)
        self.assertEqual(len(tabs("Drawing " + label)), 1 if pdf else 0)
        return page, orientation, scale

    def centre_lines(self, doc):
        """{view type: number of centre lines}, and every one of them is a thin chain line."""
        out = {}
        for item in doc.Objects:
            if item.isDerivedFrom("TechDraw::DrawProjGroupItem"):
                out[item.Type] = len(item.CosmeticEdges)
                for edge in item.CosmeticEdges:
                    self.assertEqual(edge.Format["style"], 4)        # ISO 128-20 line 04: long dash, dot
                    self.assertAlmostEqual(edge.Format["weight"], 0.25)
        return out


class Parts(Case):
    """The three parts of the task, each on A4 and on A3."""

    def both_sheets(self, build, values, expect):
        doc, obj, size3 = build()
        label = obj.Label
        for sheet in ("A4", "A3"):
            out = D.make_drawing(self.project(label), sheet)
            _page, orientation, scale = self.check_drawing(doc, label, size3, sheet, out, values)
            self.assertEqual((orientation, scale), expect[sheet], out)
            self.assertEqual(len(pages(doc)), 1)
        self.assertIn("it replaced the earlier page", out)
        return doc, out

    def test_plate_with_four_holes(self):
        doc, out = self.both_sheets(drawing_parts.plate, [100, 50, 10],
                                    {"A4": ("Portrait", (1, 1)), "A3": ("Landscape", (2, 1))})
        self.assertIn("Object: Plate, 100 x 50 x 10 mm", out)
        self.assertIn("front (from above (looking along Z))", out)
        self.assertIn("length 100 (front view); height 50 (front view); depth 10 (top view)", out)
        front = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawProjGroupItem") and o.Type == "Front"][0]
        self.assertEqual(tuple(front.Direction), (0, 0, 1))
        self.assertEqual(self.centre_lines(doc), {"Front": 8, "Top": 0, "Left": 0})     # a cross in each hole
        self.assertIn("Centre lines added: 8 ", out)
        ends = sorted((round(e.Start.x, 3), round(e.End.x, 3), round(e.Start.y, 3), round(e.End.y, 3))
                      for e in front.CosmeticEdges)
        # hole at (10, 10) of the plate = (-40, -15) from the view centre, radius 3, 3 mm on paper at 2:1 = 1.5
        self.assertIn((-44.5, -35.5, -15.0, -15.0), ends)
        self.assertIn((-40.0, -40.0, -19.5, -10.5), ends)

    def test_turned_shaft(self):
        doc, out = self.both_sheets(drawing_parts.shaft, [130, 30],
                                    {"A4": ("Landscape", (1, 1)), "A3": ("Landscape", (2, 1))})
        self.assertIn("front (the side of the turned part, axis level)", out)
        self.assertIn("length 130 (front view); largest diameter ⌀30 (front view)", out)
        dia = doc.getObject("DimDiameter")
        self.assertEqual(dia.Type, "DistanceY")
        self.assertTrue(dia.FormatSpec.startswith("⌀"), dia.FormatSpec)
        self.assertFalse(doc.getObject("DimLength").FormatSpec.startswith("⌀"))
        self.assertEqual(self.centre_lines(doc), {"Front": 1, "Top": 1, "Left": 2})     # the axis twice, one cross
        front = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawProjGroupItem") and o.Type == "Front"][0]
        axis = front.CosmeticEdges[0]
        self.assertEqual((round(axis.Start.y, 6), round(axis.End.y, 6)), (0, 0))
        self.assertEqual(sorted((round(axis.Start.x, 3), round(axis.End.x, 3))), [-66.5, 66.5])   # 3 mm past, at 2:1

    def test_partdesign_body(self):
        doc, out = self.both_sheets(drawing_parts.bracket, [60, 50, 40],
                                    {"A4": ("Portrait", (1, 1)), "A3": ("Landscape", (2, 1))})
        self.assertIn("Object: Bracket, 60 x 40 x 50 mm", out)     # the Body, not its Pad
        self.assertIn("front (from the front (looking along Y))", out)
        self.assertIn("length 60 (front view); height 50 (front view); depth 40 (top view)", out)
        group = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawProjGroup")][0]
        self.assertEqual([o.Name for o in group.Source], ["Bracket"])
        self.assertEqual(self.centre_lines(doc), {"Front": 0, "Top": 0, "Left": 0})
        self.assertIn("Centre lines added: none", out)


class Choice(Case):
    """Which object, which sheet, and what happens when something is missing."""

    def test_no_document(self):
        for d in list(App.listDocuments()):
            App.closeDocument(d)
        out = D.make_drawing(self.project("None"))
        self.assertTrue(out.startswith("ERROR: no document is open"), out)
        self.assertIn("build a model first", out)

    def test_empty_document(self):
        for d in list(App.listDocuments()):
            App.closeDocument(d)
        doc = App.newDocument("Empty")
        out = D.make_drawing(self.project("Empty"))
        self.assertTrue(out.startswith("ERROR: the document 'Empty' has no solid to draw"), out)
        self.assertEqual(len(doc.Objects), 0)
        sketch = doc.addObject("Sketcher::SketchObject", "Sketch")      # a shape, but no solid
        sketch.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(10, 0, 0)))
        doc.recompute()
        self.assertTrue(D.make_drawing(self.project("Empty")).startswith("ERROR: the document 'Empty' has no solid"))
        out = D.make_drawing(self.project("Empty"), "A3", "Sketch")
        self.assertTrue(out.startswith("ERROR: 'Sketch' is not a solid"), out)
        self.assertEqual(pages(doc), [])

    def test_unknown_object_and_unknown_sheet(self):
        doc, _obj, _size = drawing_parts.plate()
        out = D.make_drawing(self.project("Plate"), "A3", "Flange")
        self.assertTrue(out.startswith("ERROR: there is no object named 'Flange'"), out)
        self.assertIn("Solids you can draw: Plate.", out)             # not the blank and the holes it is cut from
        out = D.make_drawing(self.project("Plate"), "B4")
        self.assertTrue(out.startswith("ERROR: 'B4' is not a sheet this tool knows. Use one of A4, A3, A2, A1, A0."), out)
        self.assertEqual(pages(doc), [])

    def test_a5_becomes_a4_with_a_note(self):
        doc, _obj, size3 = drawing_parts.bracket()
        out = D.make_drawing(self.project("Bracket"), "A5")
        self.assertIn("FreeCAD has no A5 sheet, so A4 was used.", out)
        self.check_drawing(doc, "Bracket", size3, "A4", out, [60, 50, 40])

    def test_second_call_replaces_the_page(self):
        doc, _obj, size3 = drawing_parts.plate()
        out = D.make_drawing(self.project("Plate"), "A3")
        self.assertNotIn("replaced", out)
        first = sorted(o.TypeId for o in doc.Objects)
        self.assertEqual(len(doc.Objects), 7 + 10)      # the model, then page, template, group, 3 views, iso, 3 dims
        for _ in range(2):
            out = D.make_drawing(self.project("Plate"), "A3")
            self.assertIn("it replaced the earlier page of that name", out)
            self.assertEqual(sorted(o.TypeId for o in doc.Objects), first)
            self.check_drawing(doc, "Plate", size3, "A3", out, [100, 50, 10])
        # a page somebody else made stays
        other = doc.addObject("TechDraw::DrawPage", "Mine")
        other.Label = "My own page"
        D.make_drawing(self.project("Plate"), "A4")
        self.assertEqual(sorted(p.Label for p in pages(doc)), ["Drawing Plate", "My own page"])

    def test_undo_takes_the_drawing_away_in_three_steps(self):
        doc, _obj, _size = drawing_parts.plate()
        before = len(doc.Objects)
        D.make_drawing(self.project("Plate"), "A3")
        self.assertEqual(doc.UndoNames[:3], ["Centre lines", "Arrange dimensions (Hoischen)", "Make drawing"])
        for _ in range(3):
            doc.undo()
        self.assertEqual(len(doc.Objects), before)

    def test_which_object(self):
        for d in list(App.listDocuments()):
            App.closeDocument(d)
        doc = App.newDocument("Two")
        block = doc.addObject("Part::Box", "Block")
        block.Length, block.Width, block.Height = 80, 30, 20
        pin = doc.addObject("Part::Cylinder", "Pin")
        pin.Radius, pin.Height = 6, 45
        pin.Placement.Base = App.Vector(200, 0, 0)
        doc.recompute()
        Gui.Selection.clearSelection()
        project = self.project("Two")
        self.assertIn("Object: Pin,", D.make_drawing(project))               # the last visible solid
        self.assertEqual([p.Label for p in pages(doc)], ["Drawing Pin"])
        pin.ViewObject.Visibility = False
        self.assertIn("Object: Block,", D.make_drawing(project))             # the pin is hidden
        pin.ViewObject.Visibility = True
        Gui.Selection.addSelection(block)
        self.assertIn("Object: Block,", D.make_drawing(project))             # the selected one
        Gui.Selection.addSelection(pin)
        self.assertIn("Object: Pin,", D.make_drawing(project))               # two selected: the rule again
        self.assertIn("Object: Block,", D.make_drawing(project, "A3", "block"))   # by name, any case
        Gui.Selection.clearSelection()
        block.ViewObject.Visibility = False
        pin.ViewObject.Visibility = False
        out = D.make_drawing(project)
        self.assertTrue(out.startswith("ERROR: no solid is visible."), out)
        self.assertIn("Block, Pin", out)
        self.assertEqual(sorted(p.Label for p in pages(doc)), ["Drawing Block", "Drawing Pin"])

    def test_last_feature_of_a_body_means_the_body(self):
        doc, _body, _size = drawing_parts.bracket()
        out = D.make_drawing(self.project("Bracket"), "A3", "Pad")
        self.assertIn("Object: Bracket,", out)
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(doc.getObject("Pad"))
        self.assertIn("Object: Bracket,", D.make_drawing(self.project("Bracket"), "A3"))
        Gui.Selection.clearSelection()


class Shapes(Case):
    """Shapes where the rules have to think."""

    def one(self, name, shape, **kw):
        for d in list(App.listDocuments()):
            App.closeDocument(d)
        doc = App.newDocument(name + "Doc")
        obj = doc.addObject("Part::Feature", name)
        obj.Shape = shape
        for key, value in kw.items():
            setattr(obj, key, value)
        doc.recompute()
        return doc, obj

    def test_disc_gets_a_diameter(self):
        doc, _obj = self.one("Disc", Part.makeCylinder(40, 12))
        out = D.make_drawing(self.project("Disc"), "A3")
        self.check_drawing(doc, "Disc", (80, 80, 12), "A3", out, [80, 12])
        self.assertIn("front (along the axis of the turned part)", out)
        self.assertIn("diameter ⌀80 (front view); thickness 12 (top view)", out)
        self.assertEqual(doc.getObject("DimDiameter").Type, "Diameter")
        self.assertEqual(self.centre_lines(doc), {"Front": 2, "Top": 1, "Left": 1})
        top = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawProjGroupItem") and o.Type == "Top"][0]
        axis = top.CosmeticEdges[0]                 # the axis stands upright in the top view
        self.assertEqual((round(axis.Start.x, 6), round(axis.End.x, 6)), (0, 0))

    def test_round_ends_get_no_wrong_length(self):
        # a link plate 120 x 40 x 8 with fully round ends: no corner at its two ends
        body = Part.makeBox(80, 40, 8, App.Vector(20, 0, 0))
        for x in (20, 100):
            body = body.fuse(Part.makeCylinder(20, 8, App.Vector(x, 20, 0)))
        doc, _obj = self.one("Link", body.removeSplitter())
        out = D.make_drawing(self.project("Link"), "A3")
        self.check_drawing(doc, "Link", (120, 40, 8), "A3", out, [40, 8])
        self.assertIn("- No dimension for length on the front view: the outline is round", out)
        self.assertIsNone(doc.getObject("DimLength"))

    def test_ball_gets_no_dimension_at_all(self):
        doc, _obj = self.one("Ball", Part.makeSphere(25))
        out = D.make_drawing(self.project("Ball"), "A4")
        self.assertFalse(out.startswith("ERROR"), out)
        dims = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawViewDimension")]
        for d in dims:                                   # whatever is there is right
            self.assertAlmostEqual(abs(d.getRawValue()), 50, places=4)

    def test_turned_part_with_a_flat_is_an_ordinary_part(self):
        shaft = Part.makeCylinder(15, 100)
        flat = shaft.cut(Part.makeBox(40, 40, 30, App.Vector(11, -20, 70)))
        doc, _obj = self.one("Spindle", flat)
        out = D.make_drawing(self.project("Spindle"), "A3")
        self.assertFalse(out.startswith("ERROR"), out)
        self.assertIn("front (from the front (looking along Y))", out)     # standing up, as it was modelled
        self.assertIn("Centre lines added: none.", out)
        for d in [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawViewDimension")]:
            self.assertIn(round(abs(d.getRawValue()), 4), (100, 30, 26))

    def test_moved_and_turned_part(self):
        # a box turned 90 degrees about Z and moved away: X and Y change places on the drawing
        placement = App.Placement(App.Vector(500, -300, 40), App.Rotation(App.Vector(0, 0, 1), 90))
        doc, _obj = self.one("Rail", Part.makeBox(200, 20, 30), Placement=placement)
        out = D.make_drawing(self.project("Rail"), "A3")
        self.assertIn("Object: Rail, 20 x 200 x 30 mm", out)
        self.check_drawing(doc, "Rail", (20, 200, 30), "A3", out, [200, 30, 20])

    def test_small_and_large_parts_and_large_sheets(self):
        doc, _obj = self.one("Chip", Part.makeBox(6, 4, 1.5))
        out = D.make_drawing(self.project("Chip"), "A4")
        _page, _o, scale = self.check_drawing(doc, "Chip", (6, 4, 1.5), "A4", out, [6, 4, 1.5])
        self.assertEqual(scale, (10, 1))
        doc, _obj = self.one("Frame", Part.makeBox(2400, 600, 1800).cut(Part.makeBox(2300, 600, 1700, App.Vector(50, 0, 50))))
        for sheet, want in (("A3", (1, 20)), ("A2", (1, 10)), ("A1", (1, 5)), ("A0", (1, 5))):
            out = D.make_drawing(self.project("Frame"), sheet)
            _page, _o, scale = self.check_drawing(doc, "Frame", (2400, 600, 1800), sheet, out, [2400, 1800, 600])
            self.assertEqual(scale, want, sheet)


class NoHoischen(Case):
    def test_it_works_without_the_hoischen_addon(self):
        saved = {k: v for k, v in sys.modules.items() if k == "hoischen" or k.startswith("hoischen.")}
        for k in saved:
            del sys.modules[k]
        sys.modules["hoischen"] = None           # "import hoischen" now fails, as if it were not installed
        try:
            doc, obj, size3 = drawing_parts.plate()
            obj.Label = "PlainPlate"             # its own PDF, to look at
            out = D.make_drawing(self.project("PlainPlate"), "A3")
        finally:
            del sys.modules["hoischen"]
            sys.modules.update(saved)
        self.assertIn("the Hoischen addon was not found", out)
        self.assertNotIn("Hoischen / DIN preset", out)
        page, _o, _s = self.check_drawing(doc, "PlainPlate", size3, "A3", out, [100, 50, 10], hoischen=False)
        # this tool puts the numbers 10 mm from the part by itself: length above, the others left
        group = [v for v in page.Views if v.isDerivedFrom("TechDraw::DrawProjGroup")][0]
        s = float(page.Scale)
        self.assertGreater(float(doc.getObject("DimLength").Y), 50 * s / 2 + 10)
        self.assertLess(float(doc.getObject("DimHeight").X), -100 * s / 2 - 10)
        self.assertLess(float(doc.getObject("DimDepth").X), -100 * s / 2 - 10)
        self.assertEqual(group.ProjectionType, "First angle")


def run():
    code = 1
    try:
        with open(REPORT, "w", encoding="utf-8") as stream:
            suite = unittest.TestSuite()
            for case in (Parts, Choice, Shapes, NoHoischen):
                suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
            code = 0 if result.wasSuccessful() else 1
    except Exception:
        with open(REPORT, "a", encoding="utf-8") as stream:
            stream.write(traceback.format_exc())
    os._exit(code)


# from the event loop, as the panel calls it
QtCore.QTimer.singleShot(500, run)
