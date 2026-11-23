# RUN: freecadcmd
"""Tests of cadprompt/welding.py (the tools find_joints and make_weld) in a headless FreeCAD:

    FREECAD_USER_HOME=<fresh folder> freecadcmd tests/test_welding.py

The welds are the Weldment workbench in weldment/; its own geometry is tested
in freecad-weldment. Here: that the AI's tools reach it, safely.
"""

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

import FreeCAD as App                       # noqa: E402
from cadprompt import approval, local_prompt, welding  # noqa: E402
from cadprompt import local_tools as T      # noqa: E402


def t_joint():
    """Plate 200 x 100 x 10, upright 10 x 60 x 80 standing on it at x 95..105, y 20..80."""
    for d in list(App.listDocuments()):
        App.closeDocument(d)
    T.reset()
    doc = App.newDocument("Weld")
    doc.UndoMode = 1
    plate = doc.addObject("Part::Box", "Plate")
    plate.Length, plate.Width, plate.Height = 200, 100, 10
    up = doc.addObject("Part::Box", "Upright")
    up.Length, up.Width, up.Height = 10, 60, 80
    up.Placement.Base = App.Vector(95, 20, 10)
    doc.recompute()
    return doc


class Text(unittest.TestCase):
    """No FreeCAD needed for these."""

    def test_describe_says_what_will_be_welded(self):
        self.assertEqual(welding.describe({"face_a": "Plate.Face6", "face_b": "Upright.Face1", "throat": 4}),
                         "Fillet weld a4, whole seam between Plate.Face6 and Upright.Face1")
        line = welding.describe({"face_a": "A.Face1", "face_b": "B.Face2", "throat": "3.5", "segment": 50,
                                 "gap": 25, "count": 3, "both_sides": True, "tail_text": "135"})
        self.assertEqual(line, "Fillet weld a3,5 3 × 50 (25) between A.Face1 and B.Face2: both sides, process 135")

    def test_the_models_text_never_becomes_code(self):
        evil = 'X.Face1", 1); import subprocess; ("'
        code = welding.weld_code({"face_a": evil, "face_b": "B.Face1", "tail_text": "')\nimport os #"})
        lines = code.strip().splitlines()
        self.assertEqual(len(lines), 2, code)
        self.assertEqual(T._not_allowed(code), "", code)       # only literals: nothing for the guard to refuse
        import ast
        call = ast.parse(code).body[1].value
        self.assertEqual(call.args[1].value, evil)
        self.assertTrue(all(isinstance(a, ast.Constant) for a in call.args[1:]))

    def test_options_take_strings_and_commas_and_leave_out_the_rest(self):
        throat, props = welding.options({"throat": "3,5", "segment": "100", "both_sides": "yes", "length": "",
                                         "colour": "red", "count": 2.7})
        self.assertEqual((throat, props), (3.5, {"Count": 2, "Segment": 100.0, "BothSides": True}))
        self.assertEqual(welding.options({})[0], welding.DEFAULT_THROAT)
        self.assertEqual(welding.options({"throat": 0})[0], welding.DEFAULT_THROAT)

    def test_make_weld_is_a_tool_that_asks_and_the_bridge_has_both(self):
        names = [t["function"]["name"] for t in local_prompt.TOOLS]
        self.assertIn("find_joints", names)
        self.assertIn("make_weld", names)
        self.assertIn("make_weld", local_prompt.CHANGING)
        self.assertNotIn("find_joints", local_prompt.CHANGING)       # it only looks
        self.assertEqual(approval.shown("make_weld", {"face_a": "P.Face6", "face_b": "U.Face1"}),
                         "Fillet weld a4, whole seam between P.Face6 and U.Face1")


class Tools(unittest.TestCase):
    def test_find_joints_lists_the_corners_of_the_t_joint(self):
        t_joint()
        out = T.run_tool("/p", "find_joints", {})
        self.assertIn("Plate.Face6 + Upright.Face1", out)
        self.assertIn("Plate.Face6 + Upright.Face2", out)
        self.assertIn("seam 60.0 mm, opening angle 90°", out)
        self.assertIn("seam 10.0 mm", out)                       # the two short ends of the upright
        self.assertNotIn("Upright.Face5", out)                   # its bottom lies on the plate: no corner
        self.assertNotIn("welded already", out)

    def test_apart_parts_have_no_joints(self):
        doc = t_joint()
        doc.Upright.Placement.Base = App.Vector(95, 20, 30)
        doc.recompute()
        self.assertTrue(T.run_tool("/p", "find_joints", {}).startswith("No joints"))

    def test_make_weld_welds_reports_and_is_one_undo_step(self):
        doc = t_joint()
        out = T.run_tool("/p", "make_weld", {"face_a": "Plate.Face6", "face_b": "Upright.Face1", "throat": 4})
        self.assertNotIn("ERROR", out)
        self.assertIn("WELD FilletWeld", out)
        self.assertIn("seam 60.0 mm, welded 60.0 mm", out)
        self.assertIn("+ new FilletWeld", out)
        weld = doc.getObject("FilletWeld")
        self.assertAlmostEqual(weld.DepositVolume.Value, 960.0, places=3)     # (a√2)² / 2 x 60
        self.assertIn("[welded already]", T.run_tool("/p", "find_joints", {}).split("Upright.Face1")[1].splitlines()[0])
        doc.undo()
        doc.recompute()
        self.assertIsNone(doc.getObject("FilletWeld"))

    def test_labels_work_and_intermittent_both_sides(self):
        doc = t_joint()
        doc.Upright.Label = "Rib"
        out = T.run_tool("/p", "make_weld", {"face_a": "Plate.Face6", "face_b": "Rib.Face1", "throat": 3,
                                             "segment": 20, "gap": 10, "both_sides": True})
        self.assertNotIn("ERROR", out)
        weld = doc.getObject("FilletWeld")
        self.assertTrue(weld.BothSides and weld.OtherFace == "Face2", weld.OtherFace)
        self.assertEqual(len(weld.Shape.Solids), 4)

    def test_a_bad_face_is_taken_back_and_says_how_to_do_it(self):
        doc = t_joint()
        for args, words in (({"face_a": "Plate.Face6", "face_b": "Upright.Face5"}, "ERROR"),   # no corner
                            ({"face_a": "Plate.Face99", "face_b": "Upright.Face1"}, "has no Face99"),
                            ({"face_a": "Plate", "face_b": "Upright.Face1"}, "find_joints"),
                            ({"face_a": "Nope.Face1", "face_b": "Upright.Face1"}, "no object 'Nope'"),
                            ({"face_a": "Plate.Face6", "face_b": "Plate.Face1"}, "a weld joins two parts"),
                            ({"face_b": "Upright.Face1"}, "needs face_a and face_b")):
            out = T.run_tool("/p", "make_weld", args)
            self.assertIn("ERROR", out, args)
            self.assertIn(words, out, args)
            names = sorted(o.Name for o in doc.Objects)
            self.assertEqual(names, ["Plate", "Upright"], (args, out))     # no weld, no empty Welds group left


suite = unittest.TestSuite()
for case in (Text, Tools):
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
sys.stdout.flush()
os._exit(0 if result.wasSuccessful() else 1)
