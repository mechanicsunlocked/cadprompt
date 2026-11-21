# RUN: freecad-gui
"""Tests of cadprompt/helpers.py that need FreeCAD's window (no display needed):

    FREECAD_USER_HOME=<fresh folder> QT_QPA_PLATFORM=offscreen freecad tests/test_helpers_gui.py

With its window FreeCAD switches the inputs of a cut and of a fuse off itself,
at the moment the link is set. freecadcmd does not do that, so only this test
can see whether a helper call that fails shows the part again.

The result goes to <FREECAD_USER_HOME>/test_helpers_gui.log. The fresh folder
must hold the user.cfg that run_all.py writes (no first-start dialog, no
notifications).
"""

import contextlib
import io
import math
import os
import sys
import traceback
import unittest

if not os.environ.get("FREECAD_USER_HOME"):
    os._exit(2)

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(os.getcwd(), "tests")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import FreeCAD as App                           # noqa: E402
from PySide import QtCore, QtWidgets            # noqa: E402

from cadprompt import helpers as cad            # noqa: E402
from cadprompt import local_tools               # noqa: E402
import bench_checks                             # noqa: E402

LOG = os.path.join(os.environ["FREECAD_USER_HOME"], "test_helpers_gui.log")
QtCore.QLoggingCategory.setFilterRules("default.warning=false")


def shown(doc):
    """The names of the objects that the user sees: the switch of the object and that of its view."""
    QtWidgets.QApplication.processEvents()
    return sorted(o.Name for o in doc.Objects if o.Visibility and o.ViewObject is not None and o.ViewObject.Visibility)


def parts(doc):
    return [o.Name for o in bench_checks.part_objects(doc)[0]]


class Case(unittest.TestCase):
    def setUp(self):
        for name in list(App.listDocuments()):
            App.closeDocument(name)
        self.doc = App.newDocument("Model")

    def quiet(self, function, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return function(*args, **kwargs)

    def fails(self, words, function, *args, **kwargs):
        """The call raises ValueError with these words, and the user sees what he saw before."""
        before, objects = shown(self.doc), [o.Name for o in self.doc.Objects]
        self.assertTrue(before)
        with self.assertRaises(ValueError) as cm:
            self.quiet(function, *args, **kwargs)
        self.assertIn(words, str(cm.exception))
        self.assertEqual([o.Name for o in self.doc.Objects], objects)
        self.assertEqual(shown(self.doc), before)


class Window(Case):
    def test_the_window_switches_the_inputs_of_a_cut_off_itself(self):
        # the fact that this file is for; when a new FreeCAD does not do it, the other tests still hold
        a = self.doc.addObject("Part::Box", "A")
        b = self.doc.addObject("Part::Cylinder", "B")
        self.doc.recompute()
        self.assertTrue(a.Visibility and b.Visibility)
        cut = self.doc.addObject("Part::Cut", "C")
        cut.Base, cut.Tool = a, b
        self.assertTrue(App.GuiUp)
        self.assertIsNotNone(a.ViewObject)
        if a.Visibility:
            self.skipTest("this FreeCAD does not switch the base of a cut off when the link is set")


class Failed(Case):
    def test_a_hole_that_takes_nothing_away(self):
        plate = self.quiet(cad.plate, 100, 60, 8)
        tube = self.quiet(cad.tube, 40, 40, 3, 100)
        self.quiet(cad.move, tube, 200, 0, 0)
        self.assertEqual(shown(self.doc), ["Params", "Plate", "Tube"])
        self.fails("the hole takes nothing away", cad.hole, tube, 6)
        self.assertEqual(parts(self.doc), ["Plate", "Tube"])
        self.assertTrue(plate.Visibility and tube.Visibility)

    def test_a_hole_and_a_slot_that_cut_the_part_into_pieces(self):
        ring = self.quiet(cad.pipe, 100, 10, 10)
        a = 45 * math.cos(math.radians(45))
        self.fails("the hole cuts the part into 2 pieces", cad.hole, ring, 30, [(a, a), (-a, -a)])
        self.assertEqual(parts(self.doc), ["Pipe"])
        disc = self.quiet(cad.shaft, 100, 10)
        self.quiet(cad.move, disc, 200, 0, 0)
        self.fails("the slot cuts the part into 2 pieces", cad.slot, disc, 9, 110, angle=45)
        self.assertEqual(parts(self.doc), ["Pipe", "Shaft"])

    def test_a_join_of_parts_that_do_not_touch(self):
        plate = self.quiet(cad.plate, 100, 60, 8)
        far = self.quiet(cad.plate, 50, 50, 8)
        self.quiet(cad.move, far, 300, 0, 0)
        self.fails("the parts do not touch", cad.join, plate, far)
        self.assertEqual(parts(self.doc), ["Plate", far.Name])

    def test_a_fillet_that_freecad_cannot_make(self):
        plate = self.quiet(cad.plate, 100, 60, 8)
        self.fails("FreeCAD could not make a fillet of 50", cad.fillet, plate, 50, "z")
        self.assertEqual(parts(self.doc), ["Plate"])

    def test_a_part_that_was_off_stays_off(self):
        plate = self.quiet(cad.plate, 100, 60, 8)
        tube = self.quiet(cad.tube, 40, 40, 3, 100)
        self.quiet(cad.move, tube, 200, 0, 0)
        tube.Visibility = False                          # the user switched it off
        self.fails("the hole takes nothing away", cad.hole, tube, 6)
        self.assertFalse(tube.Visibility)
        self.assertTrue(plate.Visibility)
        self.assertEqual(shown(self.doc), ["Params", "Plate"])

    def test_the_code_of_the_model_catches_the_error(self):
        # run_python does not take the run back then: the helper itself must show the part again
        text = local_tools.run_python(
            "plate = cad.plate(100, 60, 8)\n"
            "tube = cad.move(cad.tube(40, 40, 3, 100), 50, 30, 8)\n"
            "try:\n"
            "    cad.hole(tube, 6)\n"
            "except Exception as e:\n"
            "    print('failed:', e)\n")
        self.assertIn("failed: cad.hole: the hole takes nothing away", text)
        self.assertNotIn("ERROR", text)
        doc = App.ActiveDocument
        self.assertEqual(shown(doc), ["Params", "Plate", "Tube"])
        self.assertEqual(parts(doc), ["Plate", "Tube"])
        text = local_tools.run_python(
            "try:\n"
            "    cad.join('Plate', cad.move('Tube', 300, 0, 0))\n"
            "except Exception as e:\n"
            "    print('failed:', e)\n")
        self.assertIn("failed: cad.join: the parts do not touch", text)
        self.assertEqual(shown(doc), ["Params", "Plate", "Tube"])
        self.assertEqual(parts(doc), ["Plate", "Tube"])


class Worked(Case):
    def test_after_a_chain_only_the_part_is_shown(self):
        plate = self.quiet(cad.plate, 120, 100, 8)
        for function, args in ((cad.hole_grid, (9, 2, 2, 80, 60)), (cad.slot, (8, 30)), (cad.fillet, (10, "z"))):
            part = self.quiet(function, plate, *args)
            self.assertEqual(shown(self.doc), sorted(["Params", part.Name]))
        tube = self.quiet(cad.tube, 60, 40, 4, 200)
        self.quiet(cad.move, tube, 60, 50, 8)
        post = self.quiet(cad.join, plate, tube)
        self.assertEqual(shown(self.doc), ["Joined", "Params"])
        self.assertEqual(parts(self.doc), [post.Name])


def run():
    code = 1
    try:
        with open(LOG, "w", encoding="utf-8") as stream:
            suite = unittest.TestSuite()
            for case in (Window, Failed, Worked):
                suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
            code = 0 if result.wasSuccessful() else 1
    except Exception:
        with open(LOG, "a", encoding="utf-8") as stream:
            stream.write(traceback.format_exc())
    os._exit(code)


run()
