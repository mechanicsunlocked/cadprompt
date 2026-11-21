# RUN: freecadcmd
"""Tests of cadprompt/helpers.py, the library that run_python offers as `cad`:

    FREECAD_USER_HOME=<fresh folder> freecadcmd tests/test_helpers.py

Every number that a part is compared with is computed here by its formula.
The parts of the benchmark (bench_requests.py) that a helper fits are built
with the helpers and measured with the benchmark's own checks.
"""

import contextlib
import io
import math
import os
import sys
import unittest

if not os.environ.get("FREECAD_USER_HOME"):
    sys.stdout.write("Set FREECAD_USER_HOME to a fresh folder first.\n")
    sys.stdout.flush()
    os._exit(2)

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(os.getcwd(), "tests")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from utf8_output import utf8_output            # noqa: E402

utf8_output()

import FreeCAD as App                           # noqa: E402
import Part                                     # noqa: E402

from cadprompt import helpers as cad            # noqa: E402
import bench_checks                             # noqa: E402
from bench_requests import REQUESTS             # noqa: E402

PI = math.pi
STANDARD = ("Part::Box", "Part::Cylinder", "Part::Wedge", "Part::Cut", "Part::MultiFuse", "Part::Compound",
            "Part::Fillet", "Part::Chamfer", "App::VarSet")


def disc(d, h):
    return PI / 4 * d * d * h


def new_doc(name="Model"):
    for d in list(App.listDocuments()):
        App.closeDocument(d)
    return App.newDocument(name)


def state(doc):
    """Everything that a failed call must leave as it was."""
    out = {}
    for o in doc.Objects:
        out[o.Name] = (o.TypeId, o.Label, o.Visibility, str(o.Placement) if hasattr(o, "Placement") else "",
                       sorted(o.ExpressionEngine))
        if o.TypeId == "App::VarSet":
            out[o.Name] += tuple(sorted((p, str(o.getPropertyByName(p))) for p in o.PropertiesList
                                        if o.getGroupOfProperty(p) != "Base"))
    return out


def holes(doc, kind="hole"):
    """[(x, y, z, diameter)] of the middle of each hole of the part, in the coordinates of the document."""
    m = bench_checks.measure(doc)
    c = m["bound"].Center
    return sorted((round(f["at"][0] + c.x, 3), round(f["at"][1] + c.y, 3), round(f["at"][2] + c.z, 3),
                   round(f["d"], 3)) for f in m["features"] if f["kind"] == kind)


def spots(points, d):
    return sorted((round(x, 3), round(y, 3), round(z, 3), d) for x, y, z in points)


class Case(unittest.TestCase):
    def setUp(self):
        self.doc = new_doc()

    def made(self, function, *args, **kwargs):
        """(the returned object, the one line that the call printed)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            obj = function(*args, **kwargs)
        text = out.getvalue()
        self.assertEqual(text.count("\n"), 1, text)
        self.assertTrue(text.startswith(obj.Name + ": ") and text.endswith(".\n"), text)
        self.assertLess(len(text), 330, text)
        return obj, text.strip()

    def quiet(self, function, *args, **kwargs):
        return self.made(function, *args, **kwargs)[0]

    def wrong(self, kind, words, function, *args, **kwargs):
        """The call raises `kind` with these words and an example, prints nothing and leaves nothing."""
        doc = App.ActiveDocument
        before = state(doc) if doc is not None else None
        out = io.StringIO()
        with self.assertRaises(kind) as cm, contextlib.redirect_stdout(out):
            function(*args, **kwargs)
        text = str(cm.exception)
        for word in [words] if isinstance(words, str) else words:
            self.assertIn(word, text)
        self.assertIn("\nExample: cad.", text)
        self.assertLessEqual(text.count("\n"), 1, text)
        self.assertTrue(text.startswith("cad."), text)
        self.assertEqual(out.getvalue(), "")
        if before is None:
            self.assertIsNone(App.ActiveDocument)
        else:
            self.assertEqual(state(doc), before)
        return text

    def params(self):
        return self.doc.getObject("Params")

    def change(self, **values):
        """Change values in Params, as a user does, and recompute."""
        for name, value in values.items():
            self.assertIn(name, self.params().PropertiesList)
            setattr(self.params(), name, value)
        self.doc.recompute()
        broken = [o.Name for o in self.doc.Objects if "Invalid" in o.State]
        self.assertEqual(broken, [])

    def one_part(self, obj, solids=1, also=()):
        """`obj` is the one thing that the user sees, a valid body, and nothing else is shown."""
        part, shown = bench_checks.part_objects(self.doc)
        self.assertEqual([o.Name for o in part], [obj.Name])
        self.assertEqual(shown, [])
        self.assertEqual(len(obj.Shape.Solids), solids)
        self.assertTrue(obj.Shape.isValid())
        self.assertEqual([o.Name for o in self.doc.Objects if o.TypeId not in STANDARD + also], [])
        self.assertEqual([o.Name for o in self.doc.Objects if hasattr(o, "Proxy")], [])

    def box(self, obj, low, high):
        b = obj.Shape.optimalBoundingBox(False, False)
        for found, wanted in zip((b.XMin, b.YMin, b.ZMin, b.XMax, b.YMax, b.ZMax), tuple(low) + tuple(high)):
            self.assertAlmostEqual(found, wanted, places=6)

    def volume(self, obj, wanted):
        self.assertAlmostEqual(obj.Shape.Volume / wanted, 1.0, places=7)


class Plate(Case):
    def test_size_place_line_and_parameters(self):
        plate, line = self.made(cad.plate, 120, 80, 10)
        self.assertEqual(plate.TypeId, "Part::Box")
        self.box(plate, (0, 0, 0), (120, 80, 10))
        self.volume(plate, 120 * 80 * 10)
        self.one_part(plate)
        self.assertEqual(line, "Plate: plate 120 x 80 x 10. Params: Plate_Length, Plate_Width, Plate_Thickness.")
        p = self.params()
        self.assertEqual(p.TypeId, "App::VarSet")
        self.assertEqual((p.Plate_Length.Value, p.Plate_Width.Value, p.Plate_Thickness.Value), (120, 80, 10))

    def test_a_change_in_params_changes_the_plate(self):
        plate = self.quiet(cad.plate, 120, 80, 10)
        self.change(Plate_Length=200, Plate_Thickness=12.5)
        self.box(plate, (0, 0, 0), (200, 80, 12.5))
        self.volume(plate, 200 * 80 * 12.5)

    def test_keywords_other_words_and_text_with_a_unit(self):
        self.box(self.quiet(cad.plate, length=60, width=40, thickness=20), (0, 0, 0), (60, 40, 20))
        self.box(self.quiet(cad.plate, 60, 40, height=5), (0, 0, 0), (60, 40, 5))
        self.box(self.quiet(cad.plate, "60", "4 cm", App.Units.Quantity("5 mm")), (0, 0, 0), (60, 40, 5))
        self.box(self.quiet(cad.box, 10, 20, 30), (0, 0, 0), (10, 20, 30))

    def test_a_second_plate_has_its_own_name_and_parameters(self):
        first = self.quiet(cad.plate, 120, 80, 10)
        second, line = self.made(cad.plate, 50, 40, 5)
        self.assertNotEqual(first.Name, second.Name)
        self.assertIn("%s_Length" % second.Name, line)
        # a first try that stays in the document is a second body for the user: the line names it
        self.assertTrue(line.endswith(" Other parts in the document: Plate."), line)
        line = self.made(cad.hole, first, 9)[1]
        self.assertTrue(line.endswith(" Other parts in the document: %s." % second.Name), line)
        self.doc.removeObject(second.Name)
        self.assertNotIn("Other parts", self.made(cad.hole, first, 5, 20, 20)[1])
        second = self.quiet(cad.plate, 50, 40, 5)
        self.change(**{"%s_Length" % second.Name: 70})
        self.box(second, (0, 0, 0), (70, 40, 5))
        self.box(first, (0, 0, 0), (120, 80, 10))
        self.assertEqual(len([o for o in self.doc.Objects if o.TypeId == "App::VarSet"]), 1)

    def test_a_name_is_the_name_the_label_and_the_start_of_the_parameters(self):
        plate, line = self.made(cad.plate, 100, 60, 8, name="Fußplatte")
        self.assertEqual((plate.Name, plate.Label), ("Fussplatte", "Fußplatte"))
        self.assertIn("Fussplatte_Length", self.params().PropertiesList)
        self.assertIn("Fussplatte_Thickness", line)
        # "T" is a unit (tesla) in an expression: the object gets another name, and it works
        odd = self.quiet(cad.plate, 10, 10, 2, name="T")
        self.assertNotEqual(odd.Name, "T")
        hole = self.quiet(cad.hole, odd, 4)
        self.volume(hole, 10 * 10 * 2 - disc(4, 2))
        self.assertEqual(self.quiet(cad.plate, 10, 10, 2, name="2 mm sheet").Label, "2 mm sheet")

    def test_usual_names_of_parts_that_the_unit_parser_of_freecad_cannot_take(self):
        # "1 Welle" is 1 W + e l for FreeCAD's unit parser, and its error ended FreeCAD: a name
        # never goes there with a number before it
        names = ("Welle", "Welle_1", "Wellenende", "Getriebewelle", "Test_Welle", "Weld", "Weldment",
                 "Welded_frame", "Well", "Kegel", "Kegelrad", "Gehäuse", "Gehaeuse", "Getriebe", "Gestell",
                 "Kette", "Kessel", "Kelle", "Messer", "Membran", "Metal", "Mesh", "Helix", "Nest", "Cell",
                 "Gel", "Jet", "1 Welle", "W e l l e", "5 kg 3 m", "kg", "mm", "GeV", "e", "pi", "Platte", "Flansch",
                 "Hexagon")
        for name in names:
            with self.subTest(name=name):
                self.doc = new_doc()
                shaft = self.quiet(cad.shaft, 30, 40, name=name)
                self.assertEqual(shaft.Label, name)
                self.volume(self.quiet(cad.hole, shaft, 10), disc(30, 40) - disc(10, 40))
        self.assertEqual(shaft.Name, "Hexagon")
        self.assertEqual(self.quiet(cad.plate, 100, 50, 10, "Kegel").Name, "Kegel")      # the name by position
        plate = self.quiet(cad.plate, 100, 50, 10, label="Weldment")
        self.assertEqual(plate.Name, "Weldment")
        self.assertEqual(self.quiet(cad.hole, plate, 8, 20, 20, name="Metal").Name, "Metal")
        # the name of a unit still gets another name, so that an expression can read the object
        for unit in ("T", "Ohm", "A", "Pa"):
            with self.subTest(unit=unit):
                self.doc = new_doc()
                shaft = self.quiet(cad.shaft, 30, 40, name=unit)
                self.assertEqual(shaft.Name, unit + "_part")
                part, line = self.made(cad.hole, shaft, 10)
                self.assertNotIn("Not bound", line)
                self.change(**{shaft.Name + "_Diameter": 40, shaft.Name + "_Length": 50})
                self.volume(part, disc(40, 50) - disc(10, 50))

    def test_the_users_params_are_used_and_stay(self):
        p = self.doc.addObject("App::VarSet", "Params")
        p.addProperty("App::PropertyLength", "Gap", "Mine")
        p.Gap = 3
        self.quiet(cad.plate, 120, 80, 10)
        self.assertEqual(len([o for o in self.doc.Objects if o.TypeId == "App::VarSet"]), 1)
        self.assertEqual(p.Gap.Value, 3)
        self.assertEqual(p.Plate_Length.Value, 120)

    def test_params_with_another_internal_name(self):
        p = self.doc.addObject("App::VarSet", "Sizes")       # the user named it Params in the tree
        p.Label = "Params"
        plate = self.quiet(cad.plate, 120, 80, 10)
        self.assertEqual(len([o for o in self.doc.Objects if o.TypeId == "App::VarSet"]), 1)
        self.assertEqual(p.Plate_Length.Value, 120)
        p.Plate_Length = 90
        self.doc.recompute()
        self.box(plate, (0, 0, 0), (90, 80, 10))

    def test_the_name_params_is_taken_by_something_else(self):
        self.doc.addObject("Part::Box", "Params")
        plate = self.quiet(cad.plate, 120, 80, 10)
        sets = [o for o in self.doc.Objects if o.TypeId == "App::VarSet"]
        self.assertEqual(len(sets), 1)
        sets[0].Plate_Length = 90
        self.doc.recompute()
        self.box(plate, (0, 0, 0), (90, 80, 10))
        hole = self.quiet(cad.hole, plate, 9, 20, 20)        # finds the same VarSet again
        self.assertEqual(len([o for o in self.doc.Objects if o.TypeId == "App::VarSet"]), 1)
        self.assertEqual(sets[0].Hole_Diameter.Value, 9)
        self.volume(hole, 90 * 80 * 10 - disc(9, 10))

    def test_wrong_calls(self):
        self.wrong(ValueError, "the width must be above 0, not -80.", cad.plate, 120, -80, 10)
        self.wrong(ValueError, "the thickness must be above 0, not 0.", cad.plate, 120, 80, 0)
        self.wrong(TypeError, "the length must be a number, not 'long'.", cad.plate, "long", 80, 10)
        self.wrong(TypeError, "the length must be a number, not None.", cad.plate, None, 80, 10)
        text = self.wrong(TypeError, "thickness", cad.plate, 120, 80)
        self.assertIn("Example: cad.plate(120, 80, 10)", text)
        self.wrong(TypeError, "colour", cad.plate, 120, 80, 10, colour="red")
        self.assertEqual(self.doc.Objects, [])           # no Params either

    def test_without_a_document(self):
        for d in list(App.listDocuments()):
            App.closeDocument(d)
        self.wrong(ValueError, "above 0", cad.plate, 120, 0, 10)
        self.assertEqual(App.listDocuments(), {})
        plate = self.quiet(cad.plate, 120, 80, 10)
        self.assertEqual(plate.Document.Name, App.ActiveDocument.Name)


class Shaft(Case):
    def test_one_step_is_a_round_bar_or_a_disc(self):
        bar, line = self.made(cad.shaft, 120, 12)
        self.box(bar, (-60, -60, 0), (60, 60, 12))
        self.volume(bar, disc(120, 12))
        self.one_part(bar)
        self.assertEqual(line, "Shaft: shaft along z, d120 x 12, 12 long. Params: Shaft_Diameter, Shaft_Length.")
        self.change(Shaft_Diameter=100, Shaft_Length=20)
        self.volume(bar, disc(100, 20))
        self.volume(self.quiet(cad.disc, 50, 5), disc(50, 5))

    def test_steps_one_after_the_other(self):
        shaft, line = self.made(cad.shaft, 25, 30, 40, 50, 30, 45)
        self.box(shaft, (-20, -20, 0), (20, 20, 125))
        self.volume(shaft, disc(25, 30) + disc(40, 50) + disc(30, 45))
        self.one_part(shaft)
        self.assertIn("d25 x 30 + d40 x 50 + d30 x 45, 125 long", line)
        for name in ("Shaft_D1", "Shaft_L1", "Shaft_D2", "Shaft_L2", "Shaft_D3", "Shaft_L3"):
            self.assertIn(name, line)
        # where each step is: the middle of its cylinder face
        self.assertEqual(holes(self.doc, "cylinder"),
                         [(0.0, 0.0, 15.0, 25.0), (0.0, 0.0, 55.0, 40.0), (0.0, 0.0, 102.5, 30.0)])

    def test_a_longer_first_step_moves_the_others(self):
        shaft = self.quiet(cad.shaft, 30, 40, 20, 60)
        self.change(Shaft_L1=70, Shaft_D2=24)
        self.box(shaft, (-15, -15, 0), (15, 15, 130))
        self.volume(shaft, disc(30, 70) + disc(24, 60))
        self.assertEqual(holes(self.doc, "cylinder"), [(0.0, 0.0, 35.0, 30.0), (0.0, 0.0, 100.0, 24.0)])

    def test_steps_as_pairs_and_as_a_list(self):
        wanted = disc(30, 40) + disc(20, 60)
        self.volume(self.quiet(cad.shaft, (30, 40), (20, 60)), wanted)
        self.volume(self.quiet(cad.shaft, [(30, 40), (20, 60)]), wanted)
        self.volume(self.quiet(cad.shaft, [30, 40, 20, 60]), wanted)
        self.volume(self.quiet(cad.shaft, diameter=30, length=40), disc(30, 40))
        self.volume(self.quiet(cad.shaft, d=30, l=40), disc(30, 40))
        self.volume(self.quiet(cad.shaft, diameter=30, height=40), disc(30, 40))
        self.box(self.quiet(cad.shaft, 30, 40, "x"), (0, -15, -15), (40, 15, 15))

    def test_along_another_axis(self):
        shaft, line = self.made(cad.shaft, 30, 40, 20, 60, axis="x")
        self.box(shaft, (0, -15, -15), (100, 15, 15))
        self.assertIn("along x", line)
        self.box(self.quiet(cad.shaft, 30, 40, axis="Y"), (-15, 0, -15), (15, 40, 15))

    def test_steps_of_the_same_diameter_are_one_cylinder(self):
        shaft = self.quiet(cad.shaft, 20, 30, 20, 30)
        self.assertEqual(holes(self.doc, "cylinder"), [(0.0, 0.0, 30.0, 20.0)])
        self.one_part(shaft)

    def test_wrong_calls(self):
        self.wrong(ValueError, "a diameter and a length for each step, 3 numbers were given", cad.shaft, 30, 40, 20)
        self.wrong(ValueError, "0 numbers were given", cad.shaft)
        self.wrong(ValueError, "the length must be above 0, not -5.", cad.shaft, 30, -5)
        self.wrong(TypeError, "the diameter must be a number", cad.shaft, "thick", 5)
        self.wrong(ValueError, 'the axis must be "x", "y" or "z", not \'up\'.', cad.shaft, 30, 40, axis="up")
        self.assertEqual(self.doc.Objects, [])


class Tube(Case):
    def test_rectangular_tube(self):
        tube, line = self.made(cad.tube, 60, 40, 4, 200)
        self.box(tube, (-30, -20, 0), (30, 20, 200))
        self.volume(tube, (60 * 40 - 52 * 32) * 200)
        self.one_part(tube)
        self.assertEqual(line, "Tube: tube 60 x 40 x 4 along z, 200 long. "
                               "Params: Tube_Width, Tube_Height, Tube_Wall, Tube_Length.")
        # hollow from end to end: a cut near each end has the area of the section
        m = bench_checks.measure(self.doc)
        for z in (0.5, 100, 199.5):
            area, sizes = bench_checks._cut_through(m, 2, z)
            self.assertAlmostEqual(area, 60 * 40 - 52 * 32, places=4)
            self.assertAlmostEqual(sizes[0], 60, places=6)
            self.assertAlmostEqual(sizes[1], 40, places=6)

    def test_square_tube_follows_its_parameters(self):
        tube = self.quiet(cad.tube, 40, 40, 3, 250)
        self.volume(tube, (40 * 40 - 34 * 34) * 250)
        self.change(Tube_Wall=5, Tube_Length=100, Tube_Width=50)
        self.volume(tube, (50 * 40 - 40 * 30) * 100)
        self.box(tube, (-25, -20, 0), (25, 20, 100))

    def test_along_x_the_width_is_along_y(self):
        self.box(self.quiet(cad.tube, 60, 40, 4, 200, axis="x"), (0, -30, -20), (200, 30, 20))
        self.box(self.quiet(cad.tube, 60, 40, 4, 200, axis="y"), (-30, 0, -20), (30, 200, 20))

    def test_other_words_for_the_arguments(self):
        tube = self.quiet(cad.tube, width=60, height=40, wall_thickness=4, length=200)
        self.volume(tube, (60 * 40 - 52 * 32) * 200)
        self.volume(self.quiet(cad.tube, 60, 40, t=4, l=200), (60 * 40 - 52 * 32) * 200)
        self.volume(self.quiet(cad.pipe, outer_diameter=20, inner_diameter=10, length=15), disc(20, 15) - disc(10, 15))

    def test_wrong_calls(self):
        self.wrong(ValueError, "the wall 20 is too thick for a section of 60 x 40.", cad.tube, 60, 40, 20, 200)
        text = self.wrong(TypeError, "length", cad.tube, 40, 3, 250)
        self.assertIn("Example: cad.tube(60, 40, 4, 200)", text)
        self.wrong(ValueError, "the wall must be above 0", cad.tube, 60, 40, 0, 200)
        self.assertEqual(self.doc.Objects, [])


class Pipe(Case):
    def test_round_tube(self):
        pipe, line = self.made(cad.pipe, 48.3, 3.2, 250)
        self.box(pipe, (-24.15, -24.15, 0), (24.15, 24.15, 250))
        self.volume(pipe, disc(48.3, 250) - disc(41.9, 250))
        self.one_part(pipe)
        self.assertEqual(line, "Pipe: pipe d48.3, bore d41.9, along z, 250 long. "
                               "Params: Pipe_Diameter, Pipe_Wall, Pipe_Length.")
        self.assertEqual(holes(self.doc), [(0.0, 0.0, 125.0, 41.9)])
        self.assertTrue(all(f["through"] for f in bench_checks.measure(self.doc)["features"] if f["kind"] == "hole"))
        self.change(Pipe_Wall=5, Pipe_Length=100)
        self.volume(pipe, disc(48.3, 100) - disc(38.3, 100))

    def test_with_the_bore(self):
        sleeve, line = self.made(cad.pipe, 20, bore=10.5, length=15)
        self.volume(sleeve, disc(20, 15) - disc(10.5, 15))
        self.assertIn("Pipe_Bore", line)
        self.assertNotIn("Pipe_Wall", self.params().PropertiesList)
        self.change(Pipe_Bore=12)
        self.volume(sleeve, disc(20, 15) - disc(12, 15))
        self.box(self.quiet(cad.pipe, 20, 2, 30, axis="x"), (0, -10, -10), (30, 10, 10))

    def test_wrong_calls(self):
        self.wrong(ValueError, "the wall 10 (for an inside diameter give bore=) does not fit into the diameter 20.",
                   cad.pipe, 20, 10, 15)
        self.wrong(ValueError, "the bore 25 does not fit into the diameter 20.", cad.pipe, 20, bore=25, length=15)
        self.wrong(ValueError, "give the wall or bore=, one of the two.", cad.pipe, 20, length=15)
        self.wrong(ValueError, "give the wall or bore=, one of the two.", cad.pipe, 20, 2, 15, bore=10)
        self.wrong(ValueError, "the length is missing.", cad.pipe, 20, 2)
        self.assertEqual(self.doc.Objects, [])


class Frame(Case):
    def test_four_mitred_bars_in_one_object(self):
        frame, line = self.made(cad.frame, 500, 300, 30, 30, 2)
        self.box(frame, (0, 0, 0), (500, 300, 30))
        self.one_part(frame, solids=4)
        section = 30 * 30 - 26 * 26
        found = sorted(s.Volume for s in frame.Shape.Solids)
        for volume, wanted in zip(found, [section * 270] * 2 + [section * 470] * 2):
            self.assertAlmostEqual(volume / wanted, 1.0, places=7)
        self.assertEqual(bench_checks._joined(frame.Shape.Solids), "")
        self.assertEqual(line, "Frame: frame 500 x 300 of 4 mitred bars (4 solids) from tube 30 x 30 x 2. Params: "
                               "Frame_Length, Frame_Width, Frame_ProfileWidth, Frame_ProfileHeight, Frame_Wall.")
        # a cut across the middle goes through the two long bars: two sections of the tube
        m = bench_checks.measure(self.doc)
        self.assertAlmostEqual(bench_checks._cut_through(m, 0, 250)[0], 2 * section, places=4)
        # the mitre at the corner (0, 0): under the line x = y is the bar along x, over it the bar along y
        bottom = [s for s in frame.Shape.Solids if s.BoundBox.XLength > 499 and s.BoundBox.YMax < 31][0]
        left = [s for s in frame.Shape.Solids if s.BoundBox.YLength > 299 and s.BoundBox.XMax < 31][0]
        for solid, inside, outside in ((bottom, (20, 10, 1), (10, 20, 1)), (left, (10, 20, 1), (20, 10, 1))):
            self.assertTrue(solid.isInside(App.Vector(*inside), 1e-6, True))
            self.assertFalse(solid.isInside(App.Vector(*outside), 1e-6, True))
        self.assertFalse(bottom.isInside(App.Vector(250, 15, 15), 1e-6, True))        # hollow

    def test_rectangular_profile_and_new_sizes(self):
        frame = self.quiet(cad.frame, 600, 400, 40, 60, 3)
        self.box(frame, (0, 0, 0), (600, 400, 60))
        section = 40 * 60 - 34 * 54
        self.volume(frame, section * (2 * 560 + 2 * 360))
        self.change(Frame_Length=800, Frame_Wall=4, Frame_ProfileWidth=50)
        section = 50 * 60 - 42 * 52
        self.box(frame, (0, 0, 0), (800, 400, 60))
        self.volume(frame, section * (2 * 750 + 2 * 350))
        self.assertEqual(len(frame.Shape.Solids), 4)
        self.assertEqual(bench_checks._joined(frame.Shape.Solids), "")

    def test_wrong_calls(self):
        self.wrong(ValueError, "the wall 15 is too thick for a profile of 30 x 30.", cad.frame, 500, 300, 30, 30, 15)
        self.wrong(ValueError, "a profile 30 wide does not fit into a frame of 500 x 50.", cad.frame, 500, 50, 30, 30, 2)
        self.wrong(TypeError, "wall", cad.frame, 500, 300, 30, 30)
        self.assertEqual(self.doc.Objects, [])


class Hole(Case):
    def test_through_hole_at_a_place(self):
        plate = self.quiet(cad.plate, 120, 80, 10)
        part, line = self.made(cad.hole, plate, 9, 20, 30)
        self.assertEqual(part.TypeId, "Part::Cut")
        self.volume(part, 120 * 80 * 10 - disc(9, 10))
        self.assertEqual(holes(self.doc), [(20.0, 30.0, 5.0, 9.0)])
        self.one_part(part)
        self.assertEqual(line, "Hole: 1 through hole d9 along z at x 20, y 30 in Plate. "
                               "Params: Hole_Diameter, Hole_X, Hole_Y.")
        self.assertFalse(plate.Visibility)
        self.assertEqual(part.Label, "Plate")            # the part keeps its name in the tree and on the drawing

    def test_the_hole_follows_its_parameters_and_the_plate(self):
        plate = self.quiet(cad.plate, 120, 80, 10)
        part = self.quiet(cad.hole, plate, 9, 20, 30)
        self.change(Hole_Diameter=12, Hole_X=100, Hole_Y=10, Plate_Thickness=25)
        self.volume(part, 120 * 80 * 25 - disc(12, 25))
        self.assertEqual(holes(self.doc), [(100.0, 10.0, 12.5, 12.0)])
        self.assertTrue(bench_checks.measure(self.doc)["features"][0]["through"])

    def test_without_a_place_it_is_the_centre_bore_and_stays_in_the_middle(self):
        disc_ = self.quiet(cad.shaft, 120, 12)
        part, line = self.made(cad.hole, disc_, 50)
        self.volume(part, disc(120, 12) - disc(50, 12))
        self.assertEqual(holes(self.doc), [(0.0, 0.0, 6.0, 50.0)])
        self.assertEqual(line, "Hole: 1 through hole d50 along z in the middle in Shaft. Params: Hole_Diameter.")
        plate = self.quiet(cad.plate, 100, 60, 8)
        part = self.quiet(cad.bore, plate, 20)
        self.change(Plate_Length=160, Plate_Width=90)
        self.volume(part, 160 * 90 * 8 - disc(20, 8))
        self.assertIn((80.0, 45.0, 4.0, 20.0), holes(self.doc))

    def test_one_number_puts_it_on_the_centre_line(self):
        bar = self.quiet(cad.plate, 120, 40, 6)
        line = self.made(cad.hole, bar, 9, x=20)[1]
        self.assertEqual(holes(self.doc), [(20.0, 20.0, 3.0, 9.0)])
        self.assertIn("Hole_X", line)
        self.assertNotIn("Hole_Y", line)
        self.change(Plate_Width=60)
        self.assertEqual(holes(self.doc), [(20.0, 30.0, 3.0, 9.0)])

    def test_a_list_of_places(self):
        plate = self.quiet(cad.plate, 100, 60, 8)
        places = [(10, 10), (90, 10), (90, 50), (10, 50)]
        part, line = self.made(cad.hole, plate, 9, places)
        self.volume(part, 100 * 60 * 8 - 4 * disc(9, 8))
        self.assertEqual(holes(self.doc), spots([(x, y, 4) for x, y in places], 9.0))
        self.one_part(part)
        self.assertIn("4 through holes d9 along z at 4 places in Plate", line)
        self.assertIn("Holes_Diameter", line)
        self.change(Holes_X2=80, Holes_Y3=40, Holes_Diameter=6)
        self.assertEqual(holes(self.doc), spots([(10, 10, 4), (80, 10, 4), (90, 40, 4), (10, 50, 4)], 6.0))

    def test_a_point_with_three_numbers_a_vector_and_one_tuple(self):
        plate = self.quiet(cad.plate, 100, 60, 8)
        part = self.quiet(cad.holes, plate, 5, [(10, 10, 0), App.Vector(30, 20, 8)])
        part = self.quiet(cad.hole, part, 5, (50, 30))
        part = self.quiet(cad.hole, part, d=5, center=(70, 40))
        part = self.quiet(cad.hole, part, diameter=5, positions=[(90, 50)])
        self.assertEqual(holes(self.doc), spots([(10, 10, 4), (30, 20, 4), (50, 30, 4), (70, 40, 4), (90, 50, 4)], 5.0))

    def test_blind_hole_from_the_top(self):
        block = self.quiet(cad.plate, 60, 40, 20)
        part, line = self.made(cad.hole, block, 15, depth=9)
        self.volume(part, 60 * 40 * 20 - disc(15, 9))
        self.assertEqual(holes(self.doc), [(30.0, 20.0, 15.5, 15.0)])      # from z 11 to 20
        self.assertFalse(bench_checks.measure(self.doc)["features"][0]["through"])
        self.assertEqual(line, "Hole: 1 hole d15, 9 deep from the high z side in the middle in Plate. "
                               "Params: Hole_Diameter, Hole_Depth.")
        self.change(Hole_Depth=5, Plate_Thickness=30)
        self.volume(part, 60 * 40 * 30 - disc(15, 5))
        self.assertEqual(holes(self.doc), [(30.0, 20.0, 27.5, 15.0)])

    def test_along_another_axis(self):
        block = self.quiet(cad.plate, 60, 40, 20)
        part, line = self.made(cad.hole, block, 8, 10, 5, axis="x")       # y 10, z 5
        self.volume(part, 60 * 40 * 20 - disc(8, 60))
        self.assertEqual(holes(self.doc), [(30.0, 10.0, 5.0, 8.0)])
        self.assertIn("along x at y 10, z 5", line)
        self.assertIn("Hole_Y, Hole_Z", line)
        part = self.quiet(cad.hole, part, 6, 50, 15, axis="y")            # x 50, z 15
        self.assertIn((50.0, 20.0, 15.0, 6.0), holes(self.doc))

    def test_a_cross_hole_through_the_middle_of_a_shaft(self):
        shaft = self.quiet(cad.shaft, 30, 80, axis="x")
        cross = self.quiet(cad.hole, shaft, 6)
        self.assertEqual(holes(self.doc), [(40.0, 0.0, 0.0, 6.0)])
        # what a drill of radius r takes out of a bar of radius R: the sum of 2 sqrt(r^2 - y^2) 2 sqrt(R^2 - y^2) dy
        n, r, big = 20000, 3.0, 15.0
        gone = sum(4 * math.sqrt(r * r - y * y) * math.sqrt(big * big - y * y) * (2 * r / n)
                   for y in (-r + (k + 0.5) * 2 * r / n for k in range(n)))
        self.assertAlmostEqual(cross.Shape.Volume / (disc(30, 80) - gone), 1.0, places=5)
        self.one_part(cross)

    def test_the_old_variable_does_not_make_a_second_body(self):
        plate = self.quiet(cad.plate, 100, 60, 8)
        first = self.quiet(cad.hole, plate, 9, 20, 30)
        second, line = self.made(cad.hole, plate, 9, 80, 30)        # `plate` again, not `first`
        self.assertEqual(second.Base.Name, first.Name)
        self.assertIn("in %s (the newest result of Plate)" % first.Name, line)
        self.volume(second, 100 * 60 * 8 - 2 * disc(9, 8))
        self.one_part(second)
        self.assertEqual(len([o for o in self.doc.Objects if o.TypeId == "Part::Cut" and o.Base == plate]), 1)

    def test_the_part_by_name_and_by_label(self):
        self.quiet(cad.plate, 100, 60, 8, name="Base plate")
        first = self.quiet(cad.hole, "Base_plate", 9, 20, 30)
        second = self.quiet(cad.hole, "Base plate", 9, 80, 30)
        third = self.quiet(cad.hole, "base PLATE", 5)
        self.assertEqual((second.Base.Name, third.Base.Name), (first.Name, second.Name))
        self.one_part(third)
        self.assertEqual(third.Label, "Base plate")
        self.assertEqual(len({o.Label for o in self.doc.Objects}), len(self.doc.Objects))

    def test_a_part_that_no_helper_made(self):
        shape = Part.makeBox(40, 30, 10, App.Vector(100, 50, 5))
        raw = Part.show(shape, "Block")
        part = self.quiet(cad.hole, raw, 8)
        self.volume(part, 40 * 30 * 10 - disc(8, 10))
        self.assertEqual(holes(self.doc), [(120.0, 65.0, 10.0, 8.0)])
        self.one_part(part, also=("Part::Feature",))

    def test_a_part_with_the_name_of_a_unit(self):
        # "A" is ampere in an expression: FreeCAD cannot read A.Shape there, so the place is a number
        raw = self.doc.addObject("Part::Box", "A")
        raw.Length, raw.Width, raw.Height = 40, 30, 10
        self.doc.recompute()
        part, line = self.made(cad.hole, "A", 8)
        self.volume(part, 40 * 30 * 10 - disc(8, 10))
        self.assertEqual(holes(self.doc), [(20.0, 15.0, 5.0, 8.0)])
        self.assertIn("Not bound: the sides of the part", line)
        self.change(Hole_Diameter=10)
        self.volume(part, 40 * 30 * 10 - disc(10, 10))
        other = self.doc.addObject("Part::Box", "Pa")
        self.doc.recompute()
        moved = self.quiet(cad.move, "Pa", 100, 0, 5)    # the names of its own parameters can be read
        self.box(moved, (100, 0, 5), (110, 10, 15))
        self.change(Pa_PosX=200)
        self.box(other, (200, 0, 5), (210, 10, 15))

    def test_a_hole_where_the_part_has_no_material(self):
        tube = self.quiet(cad.tube, 60, 40, 4, 200)
        self.wrong(ValueError, "the hole takes nothing away: the part has no material at that place.", cad.hole, tube, 9)
        cross = self.quiet(cad.hole, tube, 9, 0, 100, axis="x")          # through both walls
        self.volume(cross, (60 * 40 - 52 * 32) * 200 - 2 * disc(9, 4))
        self.one_part(cross)

    def test_wrong_calls(self):
        plate = self.quiet(cad.plate, 120, 80, 10)
        text = self.wrong(ValueError, "the hole at x 150, y 20 is outside the part, which goes from x 0 to 120 "
                                      "and from y 0 to 80.", cad.hole, plate, 9, 150, 20)
        self.assertIn("Example: cad.hole(plate, 9, 20, 30)", text)
        self.wrong(TypeError, "the first argument must be the part (the object, its name or its label), not 9. "
                              "The parts of the document are: Plate.", cad.hole, 9, 20, 30)
        self.wrong(TypeError, "not None. The parts of the document are: Plate.", cad.hole, None, 9)
        self.wrong(ValueError, ['there is no object "Plat3" in the document. Its parts are: Plate.'],
                   cad.hole, "Plat3", 9)
        self.wrong(ValueError, "the diameter must be above 0", cad.hole, plate, 0, 20, 30)
        self.wrong(ValueError, "the depth must be above 0", cad.hole, plate, 9, 20, 30, depth=-3)
        self.wrong(ValueError, "the hole takes the whole part away.", cad.hole, plate, 500)
        self.wrong(ValueError, "the hole cuts the part into 2 pieces.", cad.hole, plate, 100)
        self.wrong(ValueError, "give the places as one list, or x and y as two numbers.",
                   cad.hole, plate, 9, [(10, 10)], 20)
        self.wrong(ValueError, "the list of places is empty.", cad.hole, plate, 9, [])
        self.wrong(TypeError, "a place is two numbers such as (20, 30), not 'a'.", cad.hole, plate, 9, ["a", "b"])
        self.wrong(ValueError, "Params is not a solid part. The parts of the document are: Plate.",
                   cad.hole, self.params(), 9)
        self.assertEqual(len(self.doc.Objects), 2)       # the plate and Params
        self.assertTrue(plate.Visibility)
        self.assertEqual(plate.Label, "Plate")

    def test_an_object_that_is_gone(self):
        plate = self.quiet(cad.plate, 120, 80, 10)
        self.doc.removeObject(plate.Name)
        self.wrong(ValueError, "that object is not in the document any more", cad.hole, plate, 9)


class HoleGrid(Case):
    def test_pitches_centred_on_the_part(self):
        plate = self.quiet(cad.plate, 160, 100, 12)
        part, line = self.made(cad.hole_grid, plate, 9, 3, 2, 40, 50)
        self.volume(part, 160 * 100 * 12 - 6 * disc(9, 12))
        self.assertEqual(holes(self.doc), spots([(x, y, 6) for x in (40, 80, 120) for y in (25, 75)], 9.0))
        self.one_part(part)
        self.assertEqual(line, "HoleGrid: 6 through holes d9 along z, 3 along x by 2 along y, pitch 40 and 50, "
                               "centred on the part, in Plate. Params: HoleGrid_Diameter, HoleGrid_PitchX, "
                               "HoleGrid_PitchY. Not bound: the number of holes.")
        self.change(HoleGrid_PitchX=60, HoleGrid_PitchY=30, Plate_Length=200, HoleGrid_Diameter=7)
        self.assertEqual(holes(self.doc), spots([(x, y, 6) for x in (40, 100, 160) for y in (35, 65)], 7.0))

    def test_a_hole_in_each_corner_keeps_its_distance_from_the_edges(self):
        plate = self.quiet(cad.plate, 100, 60, 8)
        part, line = self.made(cad.hole_grid, plate, 9, 2, 2, edge=10)
        self.volume(part, 100 * 60 * 8 - 4 * disc(9, 8))
        self.assertEqual(holes(self.doc), spots([(x, y, 4) for x in (10, 90) for y in (10, 50)], 9.0))
        self.assertIn("4 through holes d9 along z, 2 along x by 2 along y, 10 from the edges, in Plate", line)
        self.assertIn("HoleGrid_Edge", line)
        self.change(Plate_Length=150, Plate_Width=80, HoleGrid_Edge=15)
        self.assertEqual(holes(self.doc), spots([(x, y, 4) for x in (15, 135) for y in (15, 65)], 9.0))

    def test_more_holes_between_the_edge_holes(self):
        plate = self.quiet(cad.plate, 100, 60, 8)
        self.quiet(cad.hole_grid, plate, 5, 4, 1, edge=10)
        self.assertEqual(holes(self.doc), spots([(x, 30, 4) for x in (10, 10 + 80 / 3, 10 + 160 / 3, 90)], 5.0))

    def test_one_row_one_pitch_and_a_centre(self):
        plate = self.quiet(cad.plate, 200, 100, 10)
        line = self.made(cad.hole_grid, plate, 6, 3, dx=20, center=(50, 30))[1]
        self.assertEqual(holes(self.doc), spots([(x, 30, 5) for x in (30, 50, 70)], 6.0))
        self.assertIn("centre at x 50, y 30", line)
        self.change(HoleGrid_X=100, HoleGrid_Y=70)
        self.assertEqual(holes(self.doc), spots([(x, 70, 5) for x in (80, 100, 120)], 6.0))
        plate = self.quiet(cad.plate, 100, 100, 10)
        self.quiet(cad.hole_grid, plate, 6, columns=2, rows=2, spacing=40)      # one pitch for both directions
        self.assertEqual([h for h in holes(self.doc) if h[0] in (30.0, 70.0) and h[1] in (30.0, 70.0)],
                         spots([(x, y, 5) for x in (30, 70) for y in (30, 70)], 6.0))

    def test_wrong_calls(self):
        plate = self.quiet(cad.plate, 160, 100, 12)
        self.wrong(ValueError, "the pitch is missing: give dx and dy, or edge= for holes at the edges.",
                   cad.hole_grid, plate, 9, 3, 2)
        self.wrong(ValueError, "give the pitches or edge=, not both", cad.hole_grid, plate, 9, 3, 2, 40, 50, edge=10)
        self.wrong(ValueError, "the hole at x -20, y 25 is outside the part", cad.hole_grid, plate, 9, 3, 2, 100, 50)
        self.wrong(ValueError, "the number of rows must be a whole number from 1, not 2.5.",
                   cad.hole_grid, plate, 9, 3, 2.5, 40, 50)
        self.wrong(ValueError, "60 from both edges is more than the part has along y (100).",
                   cad.hole_grid, plate, 9, 2, 2, edge=60)
        self.wrong(ValueError, "holes are too many for one call", cad.hole_grid, plate, 1, 50, 50, 2, 2)
        self.assertEqual(len(self.doc.Objects), 2)


class BoltCircle(Case):
    def test_holes_on_a_circle(self):
        disc_ = self.quiet(cad.shaft, 120, 12)
        part, line = self.made(cad.bolt_circle, disc_, 11, 6, 90)
        self.volume(part, disc(120, 12) - 6 * disc(11, 12))
        wanted = [(45 * math.cos(math.radians(60 * k)), 45 * math.sin(math.radians(60 * k)), 6) for k in range(6)]
        self.assertEqual(holes(self.doc), spots(wanted, 11.0))
        self.one_part(part)
        self.assertEqual(line, "BoltCircle: 6 through holes d11 along z on a circle of 90, the first at 0 degrees, "
                               "centred on the part, in Shaft. Params: BoltCircle_Diameter, BoltCircle_Circle, "
                               "BoltCircle_Start. Not bound: the number of holes.")

    def test_start_angle_and_new_values(self):
        plate = self.quiet(cad.plate, 100, 100, 10)
        part = self.quiet(cad.bolt_circle, plate, d=9, holes=4, pcd=70, start_angle=45)
        k = 35 * math.sqrt(0.5)
        self.assertEqual(holes(self.doc), spots([(50 + x, 50 + y, 5) for x in (-k, k) for y in (-k, k)], 9.0))
        self.change(BoltCircle_Circle=60, BoltCircle_Start=0, BoltCircle_Diameter=7, Plate_Length=120)
        self.assertEqual(holes(self.doc), spots([(90, 50, 5), (30, 50, 5), (60, 80, 5), (60, 20, 5)], 7.0))
        self.volume(part, 120 * 100 * 10 - 4 * disc(7, 10))

    def test_a_centre_and_a_depth(self):
        plate = self.quiet(cad.plate, 200, 100, 20)
        part, line = self.made(cad.bolt_circle, plate, 6, 3, 40, 90, center=(60, 50), depth=8)
        wanted = [(60 + 20 * math.cos(math.radians(a)), 50 + 20 * math.sin(math.radians(a)), 16) for a in (90, 210, 330)]
        self.assertEqual(holes(self.doc), spots(wanted, 6.0))
        self.volume(part, 200 * 100 * 20 - 3 * disc(6, 8))
        self.assertIn("3 holes d6, 8 deep from the high z side on a circle of 40, the first at 90 degrees, "
                      "centre at x 60, y 50, in Plate", line)
        self.change(BoltCircle_X=140)
        self.assertEqual(holes(self.doc), spots([(x + 80, y, z) for x, y, z in wanted], 6.0))

    def test_wrong_calls(self):
        disc_ = self.quiet(cad.shaft, 120, 12)
        self.wrong(ValueError, "is outside the part", cad.bolt_circle, disc_, 11, 6, 200)
        self.wrong(ValueError, "the number of holes must be a whole number from 1, not 0.",
                   cad.bolt_circle, disc_, 11, 0, 90)
        text = self.wrong(TypeError, "circle", cad.bolt_circle, disc_, 11, 6)
        self.assertIn("Example: cad.bolt_circle(disc, 11, 6, 90)", text)
        self.wrong(TypeError, "a centre is two numbers such as (20, 30), not 5.", cad.bolt_circle, disc_, 11, 6, 90,
                   center=5)
        self.assertEqual(len(self.doc.Objects), 2)


class Slot(Case):
    def slot_volume(self, width, length, depth):
        return (PI / 4 * width * width + width * (length - width)) * depth

    def test_slot_with_round_ends(self):
        bar = self.quiet(cad.plate, 120, 40, 6)
        part, line = self.made(cad.slot, bar, 9, 40, 85, 20)
        self.volume(part, 120 * 40 * 6 - self.slot_volume(9, 40, 6))
        self.assertEqual(holes(self.doc, "arc"), [(69.5, 20.0, 3.0, 9.0), (100.5, 20.0, 3.0, 9.0)])
        self.assertEqual(holes(self.doc), [])
        self.one_part(part)
        self.assertEqual(line, "Slot: slot 9 wide, 40 long overall, through along z, its middle at x 85, y 20, "
                               "0 degrees from x, in Plate. Params: Slot_Width, Slot_Length, Slot_X, Slot_Y, Slot_Angle.")
        self.assertEqual(part.Label, "Plate")

    def test_the_slot_follows_its_parameters(self):
        bar = self.quiet(cad.plate, 120, 40, 6)
        part = self.quiet(cad.slot, bar, 9, 40, 85, 20)
        self.change(Slot_Width=12, Slot_Length=50, Slot_X=60, Slot_Angle=90, Plate_Width=80, Slot_Y=40,
                    Plate_Thickness=10)
        self.volume(part, 120 * 80 * 10 - self.slot_volume(12, 50, 10))
        self.assertEqual(holes(self.doc, "arc"), [(60.0, 21.0, 5.0, 12.0), (60.0, 59.0, 5.0, 12.0)])

    def test_in_the_middle_turned_and_with_a_depth(self):
        plate = self.quiet(cad.plate, 100, 100, 10)
        part, line = self.made(cad.slot, plate, 10, 40, angle=90, depth=4)
        self.volume(part, 100 * 100 * 10 - self.slot_volume(10, 40, 4))
        self.assertEqual(holes(self.doc, "arc"), [(50.0, 35.0, 8.0, 10.0), (50.0, 65.0, 8.0, 10.0)])
        self.assertIn("4 deep from the top, its middle at x 50, y 50, 90 degrees from x", line)
        self.change(Plate_Length=140)                    # stays in the middle
        self.assertEqual(holes(self.doc, "arc"), [(70.0, 35.0, 8.0, 10.0), (70.0, 65.0, 8.0, 10.0)])

    def test_length_and_width_the_other_way_round_and_the_middle_as_a_point(self):
        bar = self.quiet(cad.plate, 120, 40, 6)
        part = self.quiet(cad.slot, bar, 40, 9, (85, 20))
        self.volume(part, 120 * 40 * 6 - self.slot_volume(9, 40, 6))
        self.assertEqual(holes(self.doc, "arc"), [(69.5, 20.0, 3.0, 9.0), (100.5, 20.0, 3.0, 9.0)])

    def test_wrong_calls(self):
        bar = self.quiet(cad.plate, 120, 40, 6)
        self.wrong(ValueError, "a slot as long as it is wide is a round hole: use cad.hole(part, 9).",
                   cad.slot, bar, 9, 9, 85, 20)
        self.wrong(ValueError, "the middle of the slot at x 185, y 20 is outside the part", cad.slot, bar, 9, 40, 185, 20)
        self.wrong(ValueError, "the slot cuts the part into 2 pieces.", cad.slot, bar, 9, 60, 60, 20, angle=90)
        text = self.wrong(TypeError, "length", cad.slot, bar, 9)
        self.assertIn("Example: cad.slot(bar, 9, 40, 85, 20)", text)
        self.assertEqual(len(self.doc.Objects), 2)
        self.assertTrue(bar.Visibility)


class Edges(Case):
    def roundings(self):
        return sorted(round(f["d"] / 2, 3) for f in bench_checks.measure(self.doc)["features"] if f["kind"] == "rounding")

    def test_fillet_of_the_four_corners(self):
        plate = self.quiet(cad.plate, 150, 100, 6)
        part, line = self.made(cad.fillet, plate, 15, "z")
        self.assertEqual(part.TypeId, "Part::Fillet")
        self.volume(part, (150 * 100 - (4 - PI) * 15 * 15) * 6)
        self.assertEqual(holes(self.doc, "rounding"), spots([(x, y, 3) for x in (15, 135) for y in (15, 85)], 30.0))
        self.one_part(part)
        self.assertFalse(plate.Visibility)
        self.assertEqual(line, "Fillet: fillet r15 on 4 edges (parallel to z) of Plate. "
                               "Not bound: the radius (FreeCAD keeps it in Fillet.Edges).")
        self.assertEqual(part.Label, "Plate")
        self.assertEqual(plate.Label, "Plate_blank")
        self.change(Plate_Length=200)                    # the plate changes, the corners stay round
        self.volume(part, (200 * 100 - (4 - PI) * 15 * 15) * 6)

    def test_chamfer_of_the_four_corners(self):
        plate = self.quiet(cad.plate, 80, 80, 10)
        part, line = self.made(cad.chamfer, plate, 10)        # "z" is the default
        self.assertEqual(part.TypeId, "Part::Chamfer")
        self.volume(part, (80 * 80 - 4 * 10 * 10 / 2) * 10)
        self.one_part(part)
        self.assertEqual(line, "Chamfer: chamfer 10 x 45 degrees on 4 edges (parallel to z) of Plate. "
                               "Not bound: the size (FreeCAD keeps it in Chamfer.Edges).")

    def test_edges_by_direction(self):
        for axis, long in (("x", 150), ("y", 100)):
            new_doc()
            self.doc = App.ActiveDocument
            plate = self.quiet(cad.plate, 150, 100, 20)
            part, line = self.made(cad.fillet, plate, 4, axis.upper())
            other = 150 * 100 / long
            self.volume(part, (other * 20 - (4 - PI) * 16) * long)
            self.assertIn("4 edges (parallel to %s)" % axis, line)

    def test_edges_around_the_top_and_the_bottom_face(self):
        # two chamfers that meet at a corner take c^2/2 for each edge, less the c^3/3 that both take
        plate = self.quiet(cad.plate, 100, 60, 20)
        part, line = self.made(cad.chamfer, plate, 3, "top")
        self.volume(part, 100 * 60 * 20 - (9 * (100 + 60) - 4 * 27 / 3))
        self.assertIn("4 edges (around the top face)", line)
        self.box(part, (0, 0, 0), (100, 60, 20))
        self.assertLess(part.Shape.Solids[0].CenterOfMass.z, 10)      # it is the top that lost material
        both, line = self.made(cad.chamfer, part, 3, "bottom")
        self.volume(both, 100 * 60 * 20 - 2 * (9 * (100 + 60) - 4 * 27 / 3))
        self.assertAlmostEqual(both.Shape.Solids[0].CenterOfMass.z, 10, places=6)

    def test_the_ends_of_a_shaft(self):
        # a ring of the section A around the axis has the volume 2 pi (distance of its middle) A
        shaft = self.quiet(cad.shaft, 40, 100)
        part, line = self.made(cad.chamfer, shaft, 2, ["top", "bottom"])
        self.volume(part, disc(40, 100) - 2 * (2 * PI * (2 * 2 / 2) * (20 - 2 / 3)))
        self.assertIn("2 edges (around the top face and around the bottom face)", line)
        shaft = self.quiet(cad.shaft, 40, 100)
        part = self.quiet(cad.fillet, shaft, 5, "top")
        # the square of the corner less a quarter circle: 25 at 20 - 2.5, less 25 pi / 4 at 15 + 20 / (3 pi)
        gone = 2 * PI * (25 * 17.5 - 25 * PI / 4 * (15 + 20 / (3 * PI)))
        self.volume(part, disc(40, 100) - gone)

    def test_holes_and_slots_do_not_count_as_edges(self):
        plate = self.quiet(cad.plate, 150, 100, 6)
        self.quiet(cad.hole_grid, plate, 9, 2, 2, edge=20)
        self.quiet(cad.slot, plate, 10, 50)
        part, line = self.made(cad.fillet, plate, 15, "z")
        self.assertIn("on 4 edges (parallel to z)", line)
        self.volume(part, (150 * 100 - (4 - PI) * 225 - 4 * PI / 4 * 81 - (PI / 4 * 100 + 10 * 40)) * 6)
        self.assertEqual(self.roundings(), [15.0] * 4)
        top, line = self.made(cad.chamfer, part, 1, "top")       # the outline, not the rims of the holes
        self.assertIn("on 8 edges (around the top face)", line)
        self.assertEqual(sorted(h[3] for h in holes(self.doc)), [9.0] * 4)
        self.one_part(top)

    def test_an_edge_next_to_a_point_and_several_choices(self):
        plate = self.quiet(cad.plate, 100, 60, 20)
        part, line = self.made(cad.fillet, plate, 5, (0, 30, 20))         # the top edge at x = 0
        self.volume(part, 100 * 60 * 20 - (1 - PI / 4) * 25 * 60)
        self.assertIn("on 1 edges (next to (0, 30, 20))", line)
        self.assertAlmostEqual(holes(self.doc, "rounding")[0][0], 5.0)
        part, line = self.made(cad.fillet, part, 5, [(100, 30, 20), (100, 30, 0)])
        self.volume(part, 100 * 60 * 20 - 3 * (1 - PI / 4) * 25 * 60)
        self.assertIn("on 2 edges", line)
        hole = self.quiet(cad.hole, part, 10)
        rim, line = self.made(cad.chamfer, hole, 1, (55, 30, 20))         # the rim of the hole at the top
        self.volume(rim, hole.Shape.Volume - 2 * PI * 0.5 * (5 + 1 / 3))
        self.one_part(rim)

    def test_all_edges(self):
        plate = self.quiet(cad.plate, 40, 30, 20)
        part, line = self.made(cad.fillet, plate, 2, "all")
        self.assertIn("on 12 edges (all)", line)
        self.one_part(part)
        self.assertLess(part.Shape.Volume, 40 * 30 * 20 - (1 - PI / 4) * 4 * 4 * (40 + 30 + 20) + 40)
        self.assertGreater(part.Shape.Volume, 40 * 30 * 20 - (1 - PI / 4) * 4 * 4 * (40 + 30 + 20))
        self.box(part, (0, 0, 0), (40, 30, 20))

    def test_other_words_for_the_corners(self):
        plate = self.quiet(cad.plate, 80, 80, 10)
        part = self.quiet(cad.chamfer, plate, 10, edges="vertical")
        self.volume(part, (80 * 80 - 200) * 10)
        part = self.quiet(cad.fillet, self.quiet(cad.plate, 80, 80, 10), r=10, edges="corners")
        self.volume(part, (80 * 80 - (4 - PI) * 100) * 10)

    def test_wrong_calls(self):
        plate = self.quiet(cad.plate, 150, 100, 6)
        text = self.wrong(ValueError, ["FreeCAD could not make a fillet of 80 on these 4 edges", "Use a smaller radius"],
                          cad.fillet, plate, 80, "z")
        self.assertIn('Example: cad.fillet(plate, 15, "z")', text)
        self.wrong(ValueError, ['the edges are "x", "y" or "z"', "not 'Edge3'."], cad.fillet, plate, 5, "Edge3")
        self.wrong(ValueError, ['the edges are "x", "y" or "z"', "not 7."], cad.chamfer, plate, 5, 7)
        self.wrong(ValueError, "a point near an edge is three numbers (x, y, z), not (10, 20).",
                   cad.fillet, plate, 5, (10, 20))
        self.wrong(ValueError, "the radius must be above 0", cad.fillet, plate, 0)
        self.wrong(ValueError, "no edges were given.", cad.fillet, plate, 5, [])
        self.assertTrue(plate.Visibility)
        self.assertEqual(plate.Label, "Plate")
        shaft = self.quiet(cad.shaft, 30, 40)
        self.wrong(ValueError, ["the part has no edge parallel to x.", '"top", "bottom", "all"'], cad.chamfer, shaft, 1, "x")
        self.assertEqual(len(self.doc.Objects), 3)


class Move(Case):
    def test_the_origin_goes_to_the_place(self):
        tube = self.quiet(cad.tube, 60, 40, 4, 200)
        moved, line = self.made(cad.move, tube, 60, 50, 8)
        self.assertEqual(moved.Name, tube.Name)
        self.box(tube, (30, 30, 8), (90, 70, 208))
        self.assertEqual(line, "Tube: moved, the origin of Tube is now at (60, 50, 8). "
                               "Params: Tube_PosX, Tube_PosY, Tube_PosZ.")
        self.quiet(cad.move, tube, (10, 0, 0))               # a place, not a distance: not 70
        self.box(tube, (-20, -20, 0), (40, 20, 200))
        self.change(Tube_PosZ=25, Tube_Length=100)
        self.box(tube, (-20, -20, 25), (40, 20, 125))
        self.one_part(tube)

    def test_a_part_that_lies_along_x_keeps_its_direction(self):
        shaft = self.quiet(cad.shaft, 30, 40, 20, 60, axis="x")
        self.quiet(cad.move, shaft, z=50)
        self.box(shaft, (0, -15, 35), (100, 15, 65))

    def test_the_newest_result_is_moved(self):
        plate = self.quiet(cad.plate, 100, 60, 8)
        part = self.quiet(cad.hole, plate, 9)
        moved, line = self.made(cad.move, plate, 0, 0, 20)
        self.assertEqual(moved.Name, part.Name)
        self.box(part, (0, 0, 20), (100, 60, 28))
        self.assertIn("the origin of %s (the newest result of Plate)" % part.Name, line)
        again = self.quiet(cad.hole, plate, 5, 20, 20)       # places are those of the document
        self.assertIn((20.0, 20.0, 24.0, 5.0), holes(self.doc))
        self.one_part(again)

    def test_wrong_calls(self):
        tube = self.quiet(cad.tube, 60, 40, 4, 200)
        self.quiet(cad.move, tube, 1, 2, 3)
        self.wrong(TypeError, "the y must be a number, not 'left'.", cad.move, tube, 60, "left", 8)
        self.wrong(ValueError, "the place is three numbers x, y, z, not (1, 2).", cad.move, tube, (1, 2))
        self.box(tube, (-29, -18, 3), (31, 22, 203))


class Join(Case):
    def test_a_tube_on_its_plate_is_one_solid(self):
        plate = self.quiet(cad.plate, 120, 100, 8)
        tube = self.quiet(cad.tube, 60, 40, 4, 200)
        self.quiet(cad.move, tube, 60, 50, 8)
        post, line = self.made(cad.join, plate, tube)
        self.volume(post, 120 * 100 * 8 + (60 * 40 - 52 * 32) * 200)
        self.box(post, (0, 0, 0), (120, 100, 208))
        self.one_part(post)
        self.assertEqual(line, "Joined: one solid from Plate and Tube.")
        self.assertFalse(plate.Visibility or tube.Visibility)
        self.change(Tube_Length=100, Plate_Thickness=8, Tube_PosX=40)
        self.volume(post, 120 * 100 * 8 + (60 * 40 - 52 * 32) * 100)
        named = self.quiet(cad.hole, post, 9, 15, 15)
        self.assertEqual(named.Label, "Joined")

    def test_the_old_variables_and_a_list(self):
        a = self.quiet(cad.plate, 50, 50, 10)
        b = self.quiet(cad.plate, 50, 50, 10)
        self.quiet(cad.move, b, 50, 0, 0)
        a2 = self.quiet(cad.hole, a, 9)
        joined = self.quiet(cad.join, [a, b], name="Bracket")
        self.assertEqual(joined.Name, "Bracket")
        self.assertEqual({o.Name for o in joined.Shapes}, {a2.Name, b.Name})
        self.volume(joined, 2 * 50 * 50 * 10 - disc(9, 10))
        self.one_part(joined)
        self.wrong(ValueError, "give two or more parts that are not yet one body.", cad.join, a, b)

    def test_parts_that_do_not_touch(self):
        a = self.quiet(cad.plate, 50, 50, 10)
        b = self.quiet(cad.plate, 50, 50, 10)
        self.quiet(cad.move, b, 80, 0, 0)
        text = self.wrong(ValueError, "the parts do not touch: the result would be 2 loose bodies.", cad.join, a, b)
        self.assertIn("Example: cad.join(plate, tube)", text)
        self.assertTrue(a.Visibility and b.Visibility)
        self.wrong(ValueError, "give two or more parts", cad.join, a)
        self.wrong(ValueError, "give two or more parts", cad.join)


class Chains(Case):
    def test_plate_holes_slot_fillet_is_one_solid_and_one_part(self):
        plate = self.quiet(cad.plate, 160, 100, 12)
        seen = []
        for function, args in ((cad.hole_grid, (9, 2, 2, 120, 60)), (cad.hole, (20,)), (cad.slot, (8, 30, 80, 20)),
                               (cad.fillet, (10, "z")), (cad.chamfer, (1, "top"))):
            part = self.quiet(function, plate, *args)        # always the first variable
            self.one_part(part)
            self.assertNotIn(part.Name, seen)
            self.assertEqual(part.Label, "Plate")
            seen.append(part.Name)
        self.assertEqual(sorted(h[3] for h in holes(self.doc)), [9.0] * 4 + [20.0])
        self.assertEqual(len(holes(self.doc, "arc")), 2)
        self.assertEqual(len(holes(self.doc, "rounding")), 4)
        # each step was made from the step before it
        chain, obj = [], part
        while obj.TypeId != "Part::Box":
            chain.append(obj.Name)
            obj = obj.Base
        self.assertEqual(chain, seen[::-1])
        # the sizes of the plate still drive all of it
        self.change(Plate_Length=200, Plate_Thickness=8)
        self.one_part(part)
        self.box(part, (0, 0, 0), (200, 100, 8))
        self.assertEqual(sorted(h[:2] for h in holes(self.doc) if h[3] == 9.0),
                         [(40.0, 20.0), (40.0, 80.0), (160.0, 20.0), (160.0, 80.0)])
        self.assertTrue(all(f.get("through", True) for f in bench_checks.measure(self.doc)["features"]))

    def test_after_a_rounded_edge_a_place_is_a_number_and_the_line_says_so(self):
        # FreeCAD's quick bounding box is too large for this part: the middle cannot be bound to it
        plate = self.quiet(cad.plate, 100, 60, 10)
        self.quiet(cad.fillet, plate, 10, "z")
        rounded = self.quiet(cad.fillet, plate, 2, "top")
        self.assertGreater(rounded.Shape.BoundBox.XLength, 100.5)
        line = self.made(cad.hole, plate, 8)[1]
        self.assertIn((50.0, 30.0, 5.0, 8.0), holes(self.doc))
        self.assertIn("Not bound: the sides of the part (the cut does not follow when the part changes).", line)
        self.change(Plate_Thickness=20)                  # the height is exact, so the hole still goes through
        self.assertTrue([f for f in bench_checks.measure(self.doc)["features"] if f["kind"] == "hole"][0]["through"])
        self.quiet(cad.hole_grid, plate, 4, 2, 2, edge=12)
        self.assertEqual(sorted(h[:2] for h in holes(self.doc) if h[3] == 4.0),
                         [(12.0, 12.0), (12.0, 48.0), (88.0, 12.0), (88.0, 48.0)])

    def test_the_file_opens_and_changes_without_the_library(self):
        plate = self.quiet(cad.plate, 100, 60, 8)
        self.quiet(cad.hole_grid, plate, 9, 2, 2, edge=10)
        self.quiet(cad.slot, plate, 6, 30)
        self.quiet(cad.fillet, plate, 8)
        frame = self.quiet(cad.frame, 300, 200, 20, 30, 2)
        self.quiet(cad.move, frame, 0, 100, 0)
        path = os.path.join(os.environ["FREECAD_USER_HOME"], "helpers.FCStd")
        self.doc.saveAs(path)
        App.closeDocument(self.doc.Name)
        modules = {name: sys.modules.pop(name) for name in list(sys.modules) if name.startswith("cadprompt")}
        try:
            doc = App.openDocument(path)
            self.assertNotIn("cadprompt.helpers", sys.modules)
            p = doc.getObject("Params")
            p.Plate_Length, p.Slot_Length, p.Frame_Length = 140, 50, 400
            doc.recompute()
            self.assertEqual([o.Name for o in doc.Objects if "Invalid" in o.State], [])
            part = doc.getObject("Fillet")
            wanted = (140 * 60 - (4 - PI) * 64 - 4 * PI / 4 * 81 - (PI / 4 * 36 + 6 * 44)) * 8
            self.assertAlmostEqual(part.Shape.Volume / wanted, 1.0, places=7)
            self.assertAlmostEqual(doc.getObject("Frame").Shape.Volume / ((20 * 30 - 16 * 26) * (2 * 380 + 2 * 180)),
                                   1.0, places=7)
            self.assertFalse(doc.getObject("Plate").Visibility)
        finally:
            sys.modules.update(modules)


# ---- the parts of the benchmark, built the intended way ---------------------------------------
def bench_spacer():
    cad.pipe(20, bore=10.5, length=15)


def bench_spacer_as_a_bar_with_a_bore():
    cad.hole(cad.shaft(20, 15), 10.5)


def bench_flange():
    part = cad.shaft(120, 12)
    cad.hole(part, 50)
    cad.bolt_circle(part, 11, 6, 90)


def bench_slot_bar():
    bar = cad.plate(120, 40, 6)
    cad.hole(bar, 9, 20, 20)
    cad.slot(bar, 9, 40, 85, 20)


def bench_slot_bar_on_the_centre_line():
    cad.plate(120, 40, 6, name="Flat bar")
    cad.hole("Flat bar", 9, x=20)
    cad.slot("Flat bar", 9, 40, x=120 - 35)


def bench_stuetze():
    plate = cad.plate(120, 100, 8, name="Fußplatte")
    tube = cad.tube(60, 40, 4, 200, name="Rohr")
    cad.move(tube, 60, 50, 8)
    cad.join(plate, tube, name="Stütze")


def bench_square_flange():
    plate = cad.plate(100, 100, 10)
    cad.fillet(plate, 12, "z")
    cad.hole(plate, 40)
    cad.bolt_circle(plate, 9, 4, 70, start=45)


def bench_square_flange_holes_first():
    plate = cad.plate(100, 100, 10)
    plate = cad.bolt_circle(plate, 9, 4, 70, 45)
    plate = cad.hole(plate, 40)
    cad.fillet(plate, 12)


def bench_counterbore():
    block = cad.plate(60, 40, 20)
    cad.hole(block, 9)
    cad.hole(block, 15, depth=9)


BENCH = {
    "01-plate": [lambda: cad.plate(120, 80, 10)],
    "02-spacer": [bench_spacer, bench_spacer_as_a_bar_with_a_bore],
    "03-vierkantrohr": [lambda: cad.tube(40, 40, 3, 250), lambda: cad.tube(40, 40, 3, 250, axis="x")],
    "04-shaft-2": [lambda: cad.shaft(30, 40, 20, 60)],
    "06-plate-4-holes": [lambda: cad.hole_grid(cad.plate(100, 60, 8), 9, 2, 2, edge=10),
                         lambda: cad.hole_grid(cad.plate(100, 60, 8), 9, 2, 2, 80, 40),
                         lambda: cad.hole(cad.plate(100, 60, 8), 9, [(10, 10), (90, 10), (10, 50), (90, 50)])],
    "08-rounded-corners": [lambda: cad.fillet(cad.plate(150, 100, 6), 15, "z")],
    "09-chamfered-corners": [lambda: cad.chamfer(cad.plate(80, 80, 10), 10, "z")],
    "10-flange": [bench_flange],
    "11-hole-grid": [lambda: cad.hole_grid(cad.plate(160, 100, 12), 9, 3, 2, 40, 50)],
    "12-welle-3": [lambda: cad.shaft(25, 30, 40, 50, 30, 45), lambda: cad.shaft([(25, 30), (40, 50), (30, 45)], axis="x")],
    "13-counterbore": [bench_counterbore],        # a "raw" request that two holes do
    "14-slot-bar": [bench_slot_bar, bench_slot_bar_on_the_centre_line],
    "18-stuetze": [bench_stuetze],
    "19-square-flange": [bench_square_flange, bench_square_flange_holes_first],
    "20-mitred-frame": [lambda: cad.frame(500, 300, 30, 30, 2)],
}


class Benchmark(unittest.TestCase):
    def test_every_request_of_the_helper_group_has_a_solution_here(self):
        wanted = {r["id"] for r in REQUESTS if r["group"] == "helper"}
        self.assertEqual(wanted - set(BENCH), set())

    def test_the_parts_pass_the_checks_of_the_benchmark(self):
        specs = {r["id"]: r for r in REQUESTS}
        for name, builds in BENCH.items():
            for build in builds:
                doc = new_doc()
                with contextlib.redirect_stdout(io.StringIO()):
                    build()
                m = bench_checks.measure(doc)
                checks = bench_checks.check(specs[name], m)
                with self.subTest(request=name, build=getattr(build, "__name__", "")):
                    self.assertEqual(bench_checks.first_failure(checks), "")
                    self.assertEqual(len(m["objects"]), 1, m["objects"])
                    self.assertEqual(m["shown_inputs"], [])


class Library(Case):
    def test_each_function_has_a_docstring_with_its_example_and_the_example_runs(self):
        self.assertEqual(sorted(cad.EXAMPLES), sorted(cad.__all__))
        self.assertLessEqual(len(cad.__all__), 13)
        for name in cad.__all__:
            new_doc()
            with contextlib.redirect_stdout(io.StringIO()):
                names = {"cad": cad, "plate": cad.plate(120, 80, 10), "bar": cad.plate(120, 40, 6),
                         "disc": cad.shaft(120, 12), "tube": cad.tube(60, 40, 4, 200)}
            function = getattr(cad, name)
            example = cad.EXAMPLES[name]
            with self.subTest(function=name):
                self.assertTrue(example.startswith("cad.%s(" % name))
                self.assertLess(len(example), 45)
                self.assertIn(example, function.__doc__)
                self.assertEqual(function.example, example)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    result = eval(example, names)
                self.assertEqual(out.getvalue().count("\n"), 1)
                self.assertTrue(result.Shape.isValid() and result.Shape.Solids)
        self.assertTrue(cad.__doc__ and "Params" in cad.__doc__)

    def test_a_name_that_is_not_there_says_what_is_there(self):
        with self.assertRaises(AttributeError) as cm:
            cad.cylinder
        self.assertIn("cad has no function cylinder. It has: plate, shaft, tube, pipe, frame, hole, hole_grid, "
                      "bolt_circle, slot, fillet, chamfer, move, join.", str(cm.exception))
        self.assertFalse(hasattr(cad, "sphere"))

    def test_a_failure_inside_freecad_leaves_nothing_behind(self):
        # the arguments are right, but FreeCAD cannot build the result: all objects of the call go away again
        plate = self.quiet(cad.plate, 100, 60, 10)
        part = self.quiet(cad.hole, plate, 9, 20, 20)
        before = state(self.doc)
        real = cad._problem
        cad._problem = lambda obj: "it is broken" if obj.TypeId == "Part::Cylinder" else real(obj)
        try:
            with self.assertRaises(ValueError) as cm, contextlib.redirect_stdout(io.StringIO()):
                cad.hole(plate, 9, 60, 30)
        finally:
            cad._problem = real
        self.assertIn("FreeCAD could not build Hole2_Tool: it is broken.", str(cm.exception))
        self.assertEqual(state(self.doc), before)
        self.assertTrue(part.Visibility)
        self.assertEqual(part.Label, "Plate")
        again = self.quiet(cad.hole, plate, 9, 60, 30)        # and the same call works afterwards
        self.assertEqual(again.Name, "Hole2")
        self.one_part(again)


suite = unittest.TestSuite()
for case in (Plate, Shaft, Tube, Pipe, Frame, Hole, HoleGrid, BoltCircle, Slot, Edges, Move, Join, Chains,
             Benchmark, Library):
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
sys.stdout.flush()
os._exit(0 if result.wasSuccessful() else 1)
