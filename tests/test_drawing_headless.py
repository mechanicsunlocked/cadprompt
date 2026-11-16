"""Tests of cadprompt/drawing.py that need no window: the rules, and a headless call.

    FREECAD_USER_HOME=<fresh folder> freecadcmd tests/test_drawing_headless.py

Or run everything (this file and the GUI test) with: python3 tests/drawing_run.py
"""

import os
import random
import sys
import tempfile
import unittest

if not os.environ.get("FREECAD_USER_HOME"):
    sys.stdout.write("Set FREECAD_USER_HOME to a fresh folder first.\n")
    sys.stdout.flush()
    os._exit(2)

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(os.getcwd(), "tests")
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import FreeCAD as App                    # noqa: E402
import Part                              # noqa: E402
from cadprompt import drawing as D       # noqa: E402
import drawing_parts                     # noqa: E402

from drawing_parts import GERMAN, inside, overlap   # noqa: E402

ISO = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO")


class Templates(unittest.TestCase):
    def test_german_templates_are_found_first(self):
        for (sheet, orientation) in GERMAN:
            path = D.find_template(sheet, orientation)
            self.assertTrue(path and path.endswith(f"{sheet}_{orientation}_ISO7200_DE.svg"), path)
        self.assertIsNone(D.find_template("A5", "Landscape"))
        self.assertIsNone(D.find_template("A3", "Portrait"))

    def test_frame_and_title_block_of_the_german_templates(self):
        for key, (frame, block) in GERMAN.items():
            t = D.read_template(D.find_template(*key))
            for got, want in zip(t["frame"], frame):
                self.assertAlmostEqual(got, want, places=3, msg=f"{key} frame")
            for got, want in zip(t["block"], block):
                self.assertAlmostEqual(got, want, delta=0.5, msg=f"{key} block {t['block']}")
            self.assertEqual(t["autofill"].get("drawing_title"), "title")
            self.assertEqual(t["autofill"].get("fc-sc"), "scale")
            self.assertEqual(t["autofill"].get("Sachnummer"), "")

    def test_english_template_keeps_the_fields_above_the_block_free_too(self):
        t = D.read_template(os.path.join(ISO, "A3_Landscape_ISO5457_advanced.svg"))
        x0, y0, x1, y1 = t["block"]
        self.assertAlmostEqual(x0, 230, delta=0.5)
        self.assertEqual((y0, x1), (10, 410))
        self.assertTrue(74 <= y1 <= 82, y1)     # 48 mm block, the scale / material line, the projection symbol

    def test_blank_template_has_no_block(self):
        t = D.read_template(os.path.join(ISO, "A3_Landscape_blank.svg"))
        self.assertIsNone(t["block"])
        self.assertEqual((t["width"], t["height"]), (420, 297))

    def test_every_iso_template_can_be_read(self):
        count = 0
        for folder, _dirs, files in os.walk(ISO):
            for f in files:
                if not f.endswith(".svg") or "titleblocks" in folder:
                    continue
                t = D.read_template(os.path.join(folder, f))
                count += 1
                self.assertTrue(t["width"] > 100 and t["height"] > 100, f)
                if t["block"]:
                    b, fr = t["block"], t["frame"]
                    self.assertTrue(inside(b, fr), f"{f}: {b}")
                    self.assertTrue((b[2] - b[0]) * (b[3] - b[1]) <= 0.45 * (fr[2] - fr[0]) * (fr[3] - fr[1]) + 1, f)
        self.assertGreater(count, 30)

    def test_path_points(self):
        self.assertEqual(D._path_points("m 68.75,5.00 v 5"), [(68.75, 5.0), (68.75, 10.0)])
        self.assertEqual(D._path_points("M -135,-3e-5 V -40"), [(-135.0, -3e-05), (-135.0, -40.0)])
        self.assertEqual(D._path_points("m 0,0 h 10 v 5 h -5 v 5 h -5 z"),
                         [(0, 0), (10, 0), (10, 5), (5, 5), (5, 10), (0, 10)])
        self.assertEqual(D._path_points("m 389,222 -10,2.5 v 5 l 10,2.5 z"),
                         [(389, 222), (379, 224.5), (379, 229.5), (389, 232)])
        self.assertEqual(D._matrix((1, 0, 0, 1, 0, 0), "translate(410, 287)"), (1, 0, 0, 1, 410, 287))


class Rules(unittest.TestCase):
    def test_front_view_shows_the_largest_side(self):
        self.assertEqual(D.front_view((100, 50, 10))[:2], ((0, 0, 1), (1, 0, 0)))    # plate lying flat
        self.assertEqual(D.front_view((50, 100, 10))[:2], ((0, 0, 1), (0, 1, 0)))    # long side level
        self.assertEqual(D.front_view((60, 40, 50))[:2], ((0, -1, 0), (1, 0, 0)))    # FreeCAD's front
        self.assertEqual(D.front_view((20, 80, 60))[:2], ((1, 0, 0), (0, 1, 0)))     # from the right
        self.assertEqual(D.front_view((50, 50, 50))[:2], ((0, -1, 0), (1, 0, 0)))    # a tie: the front

    def test_turned_parts_lie_with_their_axis_level(self):
        self.assertEqual(D.front_view((30, 30, 130), 2)[:2], ((0, -1, 0), (0, 0, 1)))
        self.assertEqual(D.front_view((130, 30, 30), 0)[:2], ((0, -1, 0), (1, 0, 0)))
        self.assertEqual(D.front_view((30, 130, 30), 1)[:2], ((1, 0, 0), (0, 1, 0)))
        self.assertEqual(D.front_view((80, 80, 10), 2)[:2], ((0, 0, 1), (1, 0, 0)))  # a disc: look along the axis

    def test_turned_axis(self):
        shaft = Part.makeCylinder(10, 40).fuse(Part.makeCylinder(15, 60, App.Vector(0, 0, 40))).removeSplitter()
        self.assertEqual(D.turned_axis(shaft), 2)
        lying = Part.makeCylinder(10, 40, App.Vector(0, 0, 0), App.Vector(1, 0, 0))
        self.assertEqual(D.turned_axis(lying), 0)
        cone = Part.makeCone(10, 5, 20).fuse(Part.makeSphere(4, App.Vector(0, 0, 22)))
        self.assertEqual(D.turned_axis(cone), 2)
        self.assertIsNone(D.turned_axis(Part.makeBox(10, 20, 30)))
        flat = shaft.cut(Part.makeBox(40, 40, 20, App.Vector(12, -20, 50)))       # a milled flat
        self.assertIsNone(D.turned_axis(flat))
        cross = shaft.cut(Part.makeCylinder(3, 60, App.Vector(-30, 0, 60), App.Vector(1, 0, 0)))
        self.assertIsNone(D.turned_axis(cross))                                    # a cross hole
        off = Part.makeCylinder(10, 40).fuse(Part.makeCylinder(4, 20, App.Vector(3, 0, 40)))
        self.assertIsNone(D.turned_axis(off))                                      # an eccentric pin

    def test_bound_box_is_exact_on_round_parts(self):
        box = D._bound_box(Part.makeCylinder(15, 60))
        self.assertAlmostEqual(box.XLength, 30, places=6)
        self.assertAlmostEqual(box.YLength, 30, places=6)

    def test_sheet_names(self):
        self.assertEqual(D._parse_sheet("a3"), ("A3", None, None))
        self.assertEqual(D._parse_sheet(" A4 landscape "), ("A4", "Landscape", None))
        self.assertEqual(D._parse_sheet("A4-portrait"), ("A4", "Portrait", None))
        self.assertEqual(D._parse_sheet("A3 portrait")[:2], ("A3", None))          # FreeCAD has no upright A3
        sheet, _force, note = D._parse_sheet("A5")
        self.assertEqual(sheet, "A4")
        self.assertIn("no A5", note)
        self.assertEqual(D._parse_sheet("B4")[0], None)
        self.assertTrue(D._parse_sheet("B4")[2].startswith("ERROR:"))
        self.assertEqual(D._parse_sheet(None)[0], "A3")

    def test_small_helpers(self):
        self.assertEqual(D._num(100.0), "100")
        self.assertEqual(D._num(12.345), "12.35")
        self.assertEqual(D._num(0.5), "0.5")
        self.assertEqual(D.scale_text((1, 2)), "1:2")
        self.assertEqual(D._file_name("Plate / v2: final"), "Plate - v2- final")
        self.assertEqual(D._file_name("///"), "part")


class Layout(unittest.TestCase):
    def check(self, size, iso, frame, block, name=""):
        plan = D.plan_layout(size, iso, frame, block)
        self.assertIsNotNone(plan, name)
        self.assertIn(plan["scale"], D.SCALES)
        s = plan["scale"][0] / plan["scale"][1]
        rects = list(plan["rects"]) + ([plan["iso"]["rect"]] if plan["iso"] else [])
        for r in rects:
            self.assertTrue(inside(r, frame, D.EDGE), f"{name}: {r} leaves the frame")
            if block:
                self.assertFalse(overlap(r, block, D.EDGE - 1e-6), f"{name}: {r} is on the title block")
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                self.assertFalse(overlap(a, b, -1e-6), f"{name}: views overlap {a} {b}")
        # the plan's rectangles are the views at that scale
        w, h, d = size
        fx, fy = plan["front"]
        self.assertAlmostEqual(plan["rects"][0][2] - fx, w * s / 2)
        self.assertAlmostEqual(plan["rects"][1][3] - (fy + plan["top_y"]), d * s / 2)
        self.assertAlmostEqual(plan["rects"][2][2] - (fx + plan["left_x"]), d * s / 2)
        self.assertLess(plan["top_y"], 0)       # first angle: the top view is under the front view
        self.assertGreater(plan["left_x"], 0)   # and the left view is right of it
        # no larger standard scale fits
        n = D.SCALES.index(plan["scale"])
        if n:
            bigger = D.SCALES[n - 1][0] / D.SCALES[n - 1][1]
            self.assertIsNone(D._place(D.view_rects(w, h, d, bigger)[0], frame, block), f"{name}: not the largest scale")
        return plan

    def test_plate_on_a3_and_a4(self):
        iso = D.iso_extent((100, 50, 10))
        frame, block = GERMAN[("A3", "Landscape")]
        plan = self.check((100, 50, 10), iso, frame, block, "plate A3")
        self.assertEqual(plan["scale"], (2, 1))
        self.assertIsNotNone(plan["iso"])
        frame, block = GERMAN[("A4", "Portrait")]
        plan = self.check((100, 50, 10), iso, frame, block, "plate A4")
        self.assertEqual(plan["scale"], (1, 1))
        self.assertEqual(plan["iso"]["scale"], (1, 1))

    def test_many_sizes_on_every_sheet(self):
        rnd = random.Random(7)
        for key, (frame, block) in GERMAN.items():
            for _ in range(150):
                size3 = tuple(round(10 ** rnd.uniform(0, 3.7), 1) for _ in range(3))
                direction, xdir, _words = D.front_view(size3)
                up = D._cross(direction, xdir)
                size = (D._extent(size3, xdir), D._extent(size3, up), D._extent(size3, direction))
                self.check(size, D.iso_extent(size3), frame, block, f"{key} {size3}")
            self.check((100, 50, 10), D.iso_extent((100, 50, 10)), frame, None, f"{key} no block")

    def test_too_large_for_any_scale(self):
        frame, block = GERMAN[("A4", "Portrait")]
        self.assertIsNone(D.plan_layout((1e6, 1e6, 1e6), (1e6, 1e6), frame, block))

    def test_a4_takes_the_orientation_with_the_larger_scale(self):
        orientation, path, _data, plan = D.choose_sheet("A4", (130, 30, 30), D.iso_extent((30, 30, 130)))
        self.assertEqual((orientation, plan["scale"]), ("Landscape", (1, 1)))      # a long shaft
        self.assertTrue(path.endswith("A4_Landscape_ISO7200_DE.svg"))
        orientation, _path, _data, plan = D.choose_sheet("A4", (100, 50, 10), D.iso_extent((100, 50, 10)))
        self.assertEqual((orientation, plan["scale"]), ("Portrait", (1, 1)))       # the same scale: upright
        self.assertIsNone(D.choose_sheet("A5", (10, 10, 10), (10, 10)))


class WhichObject(unittest.TestCase):
    def doc(self, name):
        for d in list(App.listDocuments()):
            App.closeDocument(d)
        return App.newDocument(name)

    def plate(self, doc):
        box = doc.addObject("Part::Box", "Plate")
        box.Length, box.Width, box.Height = 100, 50, 10
        return box

    def sketch_on(self, doc, obj, face):
        sk = doc.addObject("Sketcher::SketchObject", "Sketch")
        sk.AttachmentSupport = [(obj, face)]
        sk.MapMode = "FlatFace"
        sk.addGeometry(Part.Circle(App.Vector(20, 20, 0), App.Vector(0, 0, 1), 5))
        return sk

    def test_a_sketch_on_a_face_does_not_hide_the_solid(self):
        doc = self.doc("Sk")
        box = self.plate(doc)
        self.sketch_on(doc, box, "Face6")            # the usual next step of a student
        doc.recompute()
        self.assertEqual([p.Name for p in box.InList], ["Sketch"])
        self.assertEqual(D.pick_object(doc), (box, None))
        self.assertEqual(D._solids(doc), [box])
        out = D.make_drawing(tempfile.mkdtemp(prefix="drawing-", dir=os.environ["FREECAD_USER_HOME"]), "A3")
        self.assertTrue(out.startswith('Drawing made: page "Drawing Plate"'), out)

    def test_a_binder_in_another_body_does_not_hide_the_body(self):
        doc = self.doc("Bind")
        body_a = doc.addObject("PartDesign::Body", "BodyA")
        body_a.newObject("PartDesign::AdditiveBox", "Block")
        body_b = doc.addObject("PartDesign::Body", "BodyB")
        binder = body_b.newObject("PartDesign::SubShapeBinder", "Binder")
        binder.Support = [(body_a, ("",))]
        doc.recompute()
        self.assertIn("Binder", [p.Name for p in body_a.InList])
        self.assertEqual(D.pick_object(doc), (body_a, None))

    def test_the_parts_of_a_cut_stay_hidden(self):
        doc = self.doc("CutDoc")
        box = self.plate(doc)
        pin = doc.addObject("Part::Cylinder", "Pin")
        pin.Radius, pin.Height = 5, 30
        cut = doc.addObject("Part::Cut", "Drilled")
        cut.Base, cut.Tool = box, pin
        self.sketch_on(doc, box, "Face6")            # a sketch on one of its parts changes nothing
        doc.recompute()
        self.assertEqual(D.pick_object(doc), (cut, None))
        self.assertEqual(D._solids(doc), [cut])

    def test_a_feature_inside_a_body_is_not_the_part(self):
        doc = self.doc("BodyDoc")
        body = doc.addObject("PartDesign::Body", "Body")
        block = body.newObject("PartDesign::AdditiveBox", "Block")
        doc.recompute()
        sk = body.newObject("Sketcher::SketchObject", "Sketch")      # the sketch for the next feature
        sk.AttachmentSupport = [(block, "Face6")]
        sk.MapMode = "FlatFace"
        doc.recompute()
        self.assertIn("Sketch", [p.Name for p in block.InList])
        self.assertEqual(D._solids(doc), [body])
        self.assertEqual(D.pick_object(doc), (body, None))


class Headless(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp(prefix="drawing-", dir=os.environ["FREECAD_USER_HOME"])

    def test_no_document(self):
        for d in list(App.listDocuments()):
            App.closeDocument(d)
        out = D.make_drawing(self.project)
        self.assertTrue(out.startswith("ERROR: no document is open"), out)

    def test_empty_document(self):
        for d in list(App.listDocuments()):
            App.closeDocument(d)
        App.newDocument("Empty")
        out = D.make_drawing(self.project)
        self.assertTrue(out.startswith("ERROR: the document 'Empty' has no solid"), out)
        self.assertEqual(len(App.ActiveDocument.Objects), 0)

    def test_plate_without_a_window(self):
        doc, _obj, _size = drawing_parts.plate()
        out = D.make_drawing(self.project, "A3")
        self.assertTrue(out.startswith('Drawing made: page "Drawing Plate" in document PlateDoc.'), out)
        self.assertIn("Scale: 2:1", out)
        self.assertIn("PDF: not written.", out)
        self.assertIn("FreeCAD runs without its window", out)
        self.assertIn("length 100 (front view); height 50 (front view); depth 10 (top view)", out)
        self.assertIn("Centre lines added: 8 ", out)
        self.assertFalse(os.path.exists(os.path.join(self.project, "drawings")))
        pages = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawPage")]
        self.assertEqual([p.Label for p in pages], ["Drawing Plate"])
        groups = [v for v in pages[0].Views if v.isDerivedFrom("TechDraw::DrawProjGroup")]
        self.assertEqual(sorted(v.Type for v in groups[0].Views), ["Front", "Left", "Top"])
        dims = [v for v in pages[0].Views if v.isDerivedFrom("TechDraw::DrawViewDimension")]
        self.assertEqual(sorted(round(d.getRawValue(), 6) for d in dims), [10, 50, 100])
        # and again: still one page
        out = D.make_drawing(self.project, "A4", "Plate")
        self.assertIn("it replaced the earlier page", out)
        self.assertEqual(len([o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawPage")]), 1)
        self.assertEqual(len([o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawViewDimension")]), 3)

    def test_drawing_is_still_right_after_save_and_open(self):
        doc, _obj, _size = drawing_parts.shaft()
        out = D.make_drawing(self.project, "A3")
        self.assertIn("length 130 (front view); largest diameter ⌀30 (front view)", out)
        self.assertIn("Centre lines added: 4 ", out)
        path = os.path.join(self.project, "Shaft.FCStd")
        doc.saveAs(path)
        App.closeDocument(doc.Name)
        doc = App.openDocument(path)
        doc.recompute()
        dims = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawViewDimension")]
        self.assertEqual(sorted(round(abs(d.getRawValue()), 6) for d in dims), [30, 130])
        items = {o.Type: o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawProjGroupItem")}
        self.assertEqual({k: len(v.CosmeticEdges) for k, v in items.items()}, {"Front": 1, "Top": 1, "Left": 2})
        page = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawPage")][0]
        self.assertEqual(page.Template.EditableTexts["drawing_title"], "Shaft")
        self.assertEqual(tuple(items["Front"].XDirection), (0, 0, 1))      # axis level
        # and a new drawing of the opened file replaces the old page
        self.assertIn("it replaced the earlier page", D.make_drawing(self.project, "A4"))
        self.assertEqual(len([o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawPage")]), 1)

    def test_wrong_name_and_wrong_sheet(self):
        doc, _obj, _size = drawing_parts.plate()
        before = len(doc.Objects)
        out = D.make_drawing(self.project, "A3", "Flange")
        self.assertTrue(out.startswith("ERROR: there is no object named 'Flange'"), out)
        self.assertIn("Plate", out)
        out = D.make_drawing(self.project, "B4")
        self.assertTrue(out.startswith("ERROR: 'B4' is not a sheet"), out)
        self.assertEqual(len(doc.Objects), before)


suite = unittest.TestSuite()
for case in (Templates, Rules, Layout, WhichObject, Headless):
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
sys.stdout.flush()
os._exit(0 if result.wasSuccessful() else 1)
