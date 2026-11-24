# RUN: freecadcmd
"""Proves the checks of the benchmark (local_bench.py) without a model:

    FREECAD_USER_HOME=<fresh folder> freecadcmd tests/test_local_bench.py

For each of the 20 requests there is a reference solution here, plain FreeCAD
Python that builds a correct part, and at least one wrong part with a usual
mistake. The correct part must pass every check; the wrong one must fail the
check that is named with it. The code runs through run_python, as the code of
a model does. Then a scripted server plays the model for some requests, to
prove that the benchmark drives the real engine and reports each kind of end.

The reference solutions are only here. local_bench.py does not load this
file, so nothing of it can reach a model.
"""

import os
import sys

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

from cadprompt import local_tools               # noqa: E402
import bench_checks                             # noqa: E402
import local_bench                              # noqa: E402
from bench_requests import GROUPS, REQUESTS     # noqa: E402
from local_fake_server import FakeServer, answer, call, error, slow    # noqa: E402

passed, failed = [], []


def say(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    say(("ok    " if ok else "FAIL  ") + name + ("" if ok else "   <- %s" % (detail,)))


# ---- the reference solutions -------------------------------------------------------
# GOOD[id] builds a correct part. WRONG[id] is a list of (the mistake, the check that
# must fail, code). `doc`, App, Part and math are there, as in run_python.

BOX = 'b = doc.addObject("Part::Box", "%s")\nb.Length, b.Width, b.Height = %s\n'

TUBE_40 = ('o = doc.addObject("Part::Box", "Outside")\no.Length, o.Width, o.Height = 40, 40, 250\n'
           'i = doc.addObject("Part::Box", "Inside")\ni.Length, i.Width, i.Height = %(a)s, %(a)s, 250\n'
           'i.Placement.Base = App.Vector(%(t)s, %(t)s, 0)\n'
           'c = doc.addObject("Part::Cut", "Tube")\nc.Base, c.Tool = o, i\n')

SHAFT_2 = ('a = doc.addObject("Part::Cylinder", "D30")\na.Radius, a.Height = 15, %(l1)s\n'
           'b = doc.addObject("Part::Cylinder", "D20")\nb.Radius, b.Height = 10, %(l2)s\n'
           'b.Placement.Base = App.Vector(0, 0, %(l1)s)\n')
FUSE_AB = 'f = doc.addObject("Part::MultiFuse", "Shaft")\nf.Shapes = [a, b]\n'

ANGLE_5 = ('pts = [App.Vector(x, 0, z) for x, z in ((0, 0), (60, 0), (60, 5), (5, 5), (5, 40), (0, 40), (0, 0))]\n'
           'Part.show(Part.Face(Part.makePolygon(pts)).extrude(App.Vector(0, 30, 0)), "Angle")\n')

# a plate with drilled holes: Part::Cut of a box and a fuse of cylinders
DRILLED = ('plate = doc.addObject("Part::Box", "Blank")\n'
           'plate.Length, plate.Width, plate.Height = %(size)s\n'
           'tools = []\n'
           'for x, y, r, z, h in %(holes)s:\n'
           '    c = doc.addObject("Part::Cylinder", "Drill")\n'
           '    c.Radius, c.Height = r, h\n'
           '    c.Placement.Base = App.Vector(x, y, z)\n'
           '    tools.append(c)\n'
           '%(more)s'
           'f = doc.addObject("Part::MultiFuse", "Drills")\nf.Shapes = tools\n'
           'cut = doc.addObject("Part::Cut", "Plate")\ncut.Base, cut.Tool = plate, f\n')


def drilled(size, holes, more=""):
    return DRILLED % {"size": size, "holes": repr(list(holes)), "more": more}


def corners(pitch_x, pitch_y, height=8):
    return [(x, y, 4.5, 0, height) for x in (50 - pitch_x / 2, 50 + pitch_x / 2)
            for y in (30 - pitch_y / 2, 30 + pitch_y / 2)]


HEXAGON = ('r = %s\n'
           'pts = [App.Vector(r * math.cos(math.radians(60 * k)), r * math.sin(math.radians(60 * k)), 0) '
           'for k in range(7)]\n'
           'bar = Part.Face(Part.makePolygon(pts)).extrude(App.Vector(0, 0, 30))\n'
           'Part.show(bar.cut(Part.makeCylinder(6.5, 30)), "Spacer")\n')

# the upright edges of a box, by their direction: the numbers of the edges are not the same in every FreeCAD
EDGED = ('box = doc.addObject("Part::Box", "Blank")\nbox.Length, box.Width, box.Height = %(size)s\n'
         'doc.recompute()\n'
         'up = [i + 1 for i, e in enumerate(box.Shape.Edges) if abs(e.tangentAt(e.FirstParameter).z) > 0.99]\n'
         'top = [i + 1 for i, e in enumerate(box.Shape.Edges) if abs(e.tangentAt(e.FirstParameter).z) < 0.01'
         ' and e.BoundBox.ZMin > 1]\n'
         'f = doc.addObject("Part::%(kind)s", "Plate")\nf.Base = box\n'
         'f.Edges = [(i, %(r)s, %(r)s) for i in %(which)s]\n')


def edged(size, kind, r, which="up"):
    return EDGED % {"size": size, "kind": kind, "r": r, "which": which}


FLANGE = ('d = doc.addObject("Part::Cylinder", "Disc")\nd.Radius, d.Height = 60, 12\n'
          'b = doc.addObject("Part::Cylinder", "Bore")\nb.Radius, b.Height = 25, 12\n'
          'tools = [b]\n'
          'for angle in %(angles)s:\n'
          '    h = doc.addObject("Part::Cylinder", "Hole")\n'
          '    h.Radius, h.Height = 5.5, 12\n'
          '    h.Placement.Base = App.Vector(%(r)s * math.cos(math.radians(angle)), '
          '%(r)s * math.sin(math.radians(angle)), 0)\n'
          '    tools.append(h)\n'
          'f = doc.addObject("Part::MultiFuse", "Tools")\nf.Shapes = tools\n'
          'c = doc.addObject("Part::Cut", "Flange")\nc.Base, c.Tool = d, f\n')

# PartDesign: a body with a pad and a pocket
GRID = ('body = doc.addObject("PartDesign::Body", "BasePlate")\n'
        'sk = body.newObject("Sketcher::SketchObject", "Outline")\n'
        'pts = [App.Vector(0, 0, 0), App.Vector(160, 0, 0), App.Vector(160, 100, 0), App.Vector(0, 100, 0)]\n'
        'for i in range(4):\n'
        '    sk.addGeometry(Part.LineSegment(pts[i], pts[(i + 1) %% 4]))\n'
        'pad = body.newObject("PartDesign::Pad", "Pad")\npad.Profile = sk\npad.Length = 12\n'
        'doc.recompute()\n'
        'hs = body.newObject("Sketcher::SketchObject", "Holes")\n'
        'hs.Placement = App.Placement(App.Vector(0, 0, 12), App.Rotation())\n'
        'for x in %(xs)s:\n'
        '    for y in %(ys)s:\n'
        '        hs.addGeometry(Part.Circle(App.Vector(x, y, 0), App.Vector(0, 0, 1), 4.5))\n'
        'pocket = body.newObject("PartDesign::Pocket", "Pocket")\npocket.Profile = hs\n'
        'pocket.Type = "ThroughAll"\n')

# lies along x, not along z
WELLE = ('s = None\nx = 0\n'
         'for d, l in %s:\n'
         '    step = Part.makeCylinder(d / 2, l, App.Vector(x, 0, 0), App.Vector(1, 0, 0))\n'
         '    s = step if s is None else s.fuse(step)\n'
         '    x += l\n'
         'Part.show(s, "Welle")\n')

SLOT_BAR = drilled("120, 40, 6", [(20, 20, 4.5, 0, 6), ("A", 20, 4.5, 0, 6), ("B", 20, 4.5, 0, 6)],
                   'between = doc.addObject("Part::Box", "Between")\n'
                   'between.Length, between.Width, between.Height = B - A, 9, 6\n'
                   'between.Placement.Base = App.Vector(A, 15.5, 0)\n%s')


def slot_bar(a, b, between=True):
    code = SLOT_BAR % ("tools.append(between)\n" if between else "doc.removeObject(between.Name)\n")
    return "A, B = %s, %s\n" % (a, b) + code.replace("'A'", "A").replace("'B'", "B")


COUNTERSUNK = ('strip = doc.addObject("Part::Box", "Blank")\nstrip.Length, strip.Width, strip.Height = 100, 25, 6\n'
               'tools = []\n'
               'for x, up in %s:\n'
               '    h = doc.addObject("Part::Cylinder", "Drill")\n'
               '    h.Radius, h.Height = 2.75, 6\n'
               '    h.Placement.Base = App.Vector(x, 12.5, 0)\n'
               '    tools.append(h)\n'
               '    if up is None:\n'
               '        continue\n'
               '    k = doc.addObject("Part::Cone", "Sink")\n'
               '    k.Radius1, k.Radius2, k.Height = (2.75, 5.5, 2.75) if up else (5.5, 2.75, 2.75)\n'
               '    k.Placement.Base = App.Vector(x, 12.5, 3.25 if up else 0)\n'
               '    tools.append(k)\n'
               'f = doc.addObject("Part::MultiFuse", "Tools")\nf.Shapes = tools\n'
               'c = doc.addObject("Part::Cut", "Strip")\nc.Base, c.Tool = strip, f\n')

GUSSET = ('pts = [App.Vector(x, y, 0) for x, y in %s]\n'
          'Part.show(Part.Face(Part.makePolygon(pts + pts[:1])).extrude(App.Vector(0, 0, 8)), "Gusset")\n')

ANGLE_HOLES = ('pts = [App.Vector(x, 0, z) for x, z in ((0, 0), (80, 0), (80, 6), (6, 6), (6, 60), (0, 60), (0, 0))]\n'
               's = Part.Face(Part.makePolygon(pts)).extrude(App.Vector(0, 50, 0))\n'
               'for y in %(ys)s:\n'
               '    s = s.cut(Part.makeCylinder(4.5, 6, App.Vector(60, y, 0), App.Vector(0, 0, 1)))\n'
               's = s.cut(Part.makeCylinder(%(r)s, 6, App.Vector(0, 25, 40), App.Vector(1, 0, 0)))\n'
               'Part.show(s, "Angle")\n')

STUETZE = ('plate = doc.addObject("Part::Box", "Fussplatte")\n'
           'plate.Length, plate.Width, plate.Height = 120, 100, 8\n'
           'o = doc.addObject("Part::Box", "Aussen")\no.Length, o.Width, o.Height = %(a)s, %(b)s, 200\n'
           'o.Placement.Base = App.Vector(%(x)s, %(y)s, 8)\n'
           'i = doc.addObject("Part::Box", "Innen")\ni.Length, i.Width, i.Height = %(a)s - 8, %(b)s - 8, 200\n'
           'i.Placement.Base = App.Vector(%(x)s + 4, %(y)s + 4, 8)\n'
           'tube = doc.addObject("Part::Cut", "Rohr")\ntube.Base, tube.Tool = o, i\n')
FUSE_STUETZE = 'f = doc.addObject("Part::MultiFuse", "Stuetze")\nf.Shapes = [plate, tube]\n'

SQUARE_FLANGE = (edged("100, 100, 10", "Fillet", 12).replace('"Plate"', '"Rounded"')
                 + 'tools = []\n'
                   'for x, y, r in %s:\n'
                   '    c = doc.addObject("Part::Cylinder", "Drill")\n'
                   '    c.Radius, c.Height = r, 10\n'
                   '    c.Placement.Base = App.Vector(50 + x, 50 + y, 0)\n'
                   '    tools.append(c)\n'
                   'm = doc.addObject("Part::MultiFuse", "Drills")\nm.Shapes = tools\n'
                   'cut = doc.addObject("Part::Cut", "Flange")\ncut.Base, cut.Tool = f, m\n')
K = 35 * 0.5 ** 0.5

# a bar with a mitre at both ends: the outside prism less the prism of the hollow
FRAME = ('def bar(length):\n'
         '    def prism(inset):\n'
         '        pts = [(inset, inset), (length - inset, inset), (length - 30 + inset, 30 - inset), '
         '(30 - inset, 30 - inset), (inset, inset)]\n'
         '        face = Part.Face(Part.makePolygon([App.Vector(x, y, inset) for x, y in pts]))\n'
         '        return face.extrude(App.Vector(0, 0, 30 - 2 * inset))\n'
         '    return prism(0).cut(prism(2))\n'
         'def tube(length):\n'
         '    return Part.makeBox(length, 30, 30).cut(Part.makeBox(length, 26, 26, App.Vector(0, 2, 2)))\n'
         'def put(shape, angle, x, y):\n'
         '    shape.Placement = App.Placement(App.Vector(x, y, 0), App.Rotation(App.Vector(0, 0, 1), angle))\n'
         '    return shape\n')
MITRED = ('bars = [bar(500), put(bar(500), 180, 500, 300), put(bar(300), 90, 500, 0), put(bar(300), -90, 0, 300)]\n')
SHOW_BARS = 'for n, b in enumerate(bars):\n    Part.show(b, "Bar%d" % (n + 1))\n'

GOOD = {
    "01-plate": BOX % ("Plate", "120, 80, 10"),
    "02-spacer": ('o = doc.addObject("Part::Cylinder", "Outside")\no.Radius, o.Height = 10, 15\n'
                  'i = doc.addObject("Part::Cylinder", "Bore")\ni.Radius, i.Height = 5.25, 15\n'
                  'c = doc.addObject("Part::Cut", "Spacer")\nc.Base, c.Tool = o, i\n'),
    "03-vierkantrohr": TUBE_40 % {"a": 34, "t": 3},
    "04-shaft-2": SHAFT_2 % {"l1": 40, "l2": 60} + FUSE_AB,
    "05-angle": ANGLE_5,
    "06-plate-4-holes": drilled("100, 60, 8", corners(80, 40)),
    "07-hexagon": HEXAGON % "24 / math.sqrt(3)",
    "08-rounded-corners": edged("150, 100, 6", "Fillet", 15),
    "09-chamfered-corners": edged("80, 80, 10", "Chamfer", 10),
    "10-flange": FLANGE % {"angles": [15 + 60 * k for k in range(6)], "r": 45},
    "11-hole-grid": GRID % {"xs": (40, 80, 120), "ys": (25, 75)},
    "12-welle-3": WELLE % "((25, 30), (40, 50), (30, 45))",
    "13-counterbore": drilled("60, 40, 20", [(30, 20, 4.5, 0, 20), (30, 20, 7.5, 11, 9)]),
    "14-slot-bar": slot_bar(69.5, 100.5),
    "15-countersunk": COUNTERSUNK % "((20, True), (80, True))",
    "16-gusset": GUSSET % "((15, 0), (80, 0), (0, 60), (0, 15))",
    "17-angle-holes": ANGLE_HOLES % {"ys": (12, 38), "r": 5.5},
    "18-stuetze": STUETZE % {"a": 60, "b": 40, "x": 30, "y": 30} + FUSE_STUETZE,
    "19-square-flange": SQUARE_FLANGE % repr([(0, 0, 20), (K, K, 4.5), (K, -K, 4.5), (-K, K, 4.5), (-K, -K, 4.5)]),
    "20-mitred-frame": FRAME + MITRED + SHOW_BARS,
}

WRONG = {
    "01-plate": [("8 thick, not 10", "box", BOX % ("Plate", "120, 80, 8"))],
    "02-spacer": [("the bore is not through", "features", GOOD["02-spacer"].replace("5.25, 15", "5.25, 10")),
                  ("a solid pin", "features", 'o = doc.addObject("Part::Cylinder", "Pin")\no.Radius, o.Height = 10, 15\n')],
    "03-vierkantrohr": [("wall 4, not 3", "volume", TUBE_40 % {"a": 32, "t": 4}),
                        ("a solid bar", "volume", BOX % ("Bar", "40, 40, 250"))],
    "04-shaft-2": [("the two lengths changed places", "places", SHAFT_2 % {"l1": 60, "l2": 40} + FUSE_AB),
                   ("two loose cylinders", "solids", SHAFT_2 % {"l1": 40, "l2": 60}),
                   ("the second step is 25 thick", "features",
                    (SHAFT_2 % {"l1": 40, "l2": 60}).replace("10, 60", "12.5, 60") + FUSE_AB)],
    "05-angle": [("a T, the upright leg in the middle", "places",
                  BOX % ("Base", "60, 30, 5")
                  + 'w = doc.addObject("Part::Box", "Web")\nw.Length, w.Width, w.Height = 5, 30, 35\n'
                    'w.Placement.Base = App.Vector(27.5, 0, 5)\n'
                    'f = doc.addObject("Part::MultiFuse", "Angle")\nf.Shapes = [b, w]\n'),
                 ("6 thick, not 5", "volume", ANGLE_5.replace("(60, 5), (5, 5), (5, 40)", "(60, 6), (6, 6), (6, 40)"))],
    "06-plate-4-holes": [("three holes", "features", drilled("100, 60, 8", corners(80, 40)[:3])),
                         ("15 from the edges, not 10", "places", drilled("100, 60, 8", corners(70, 30))),
                         ("the holes are 5 deep", "features", drilled("100, 60, 8", corners(80, 40, 5))),
                         ("the drills were made but never cut out", "solids",
                          drilled("100, 60, 8", corners(80, 40)).split("f = doc.addObject")[0])],
    "07-hexagon": [("24 across the corners", "box", HEXAGON % "12"),
                   ("a round sleeve", "features", 'Part.show(Part.makeCylinder(12, 30).cut(Part.makeCylinder(6.5, 30)))\n')],
    "08-rounded-corners": [("radius 10", "features", edged("150, 100, 6", "Fillet", 10)),
                           ("chamfers, no radius", "features", edged("150, 100, 6", "Chamfer", 15)),
                           ("the top edges were rounded, not the corners", "features",
                            edged("150, 100, 6", "Fillet", 3, "top"))],
    "09-chamfered-corners": [("rounded, not chamfered", "features", edged("80, 80, 10", "Fillet", 10)),
                             ("chamfer 8", "volume", edged("80, 80, 10", "Chamfer", 8)),
                             ("the top edges were chamfered, not the corners", "volume",
                              edged("80, 80, 10", "Chamfer", 5, "top"))],
    "10-flange": [("5 holes", "features", FLANGE % {"angles": [72 * k for k in range(5)], "r": 45}),
                  ("bolt circle 80", "places", FLANGE % {"angles": [60 * k for k in range(6)], "r": 40}),
                  ("not equally spaced", "places", FLANGE % {"angles": [0, 60, 120, 180, 240, 290], "r": 45})],
    "11-hole-grid": [("the two pitches changed places", "places", GRID % {"xs": (55, 105), "ys": (10, 50, 90)}),
                     ("four holes", "features", GRID % {"xs": (40, 120), "ys": (25, 75)})],
    "12-welle-3": [("the steps in another order", "places", WELLE % "((25, 30), (30, 45), (40, 50))"),
                   ("the middle step is 40 long", "box", WELLE % "((25, 30), (40, 40), (30, 45))")],
    "13-counterbore": [("counterbore 5 deep", "places",
                        drilled("60, 40, 20", [(30, 20, 4.5, 0, 20), (30, 20, 7.5, 15, 5)])),
                       ("the hole stops 5 above the bottom", "features",
                        drilled("60, 40, 20", [(30, 20, 4.5, 5, 15), (30, 20, 7.5, 11, 9)])),
                       ("no counterbore", "features",
                        'Part.show(Part.makeBox(60, 40, 20).cut(Part.makeCylinder(4.5, 20, App.Vector(30, 20, 0))))\n')],
    "14-slot-bar": [("the slot is 40 between its centres", "places", slot_bar(65, 105)),
                    ("two holes, nothing cut between them", "features", slot_bar(69.5, 100.5, between=False)),
                    ("the slot is at the wrong end, over the hole", "places", slot_bar(19.5, 50.5))],
    "15-countersunk": [("one countersink on each side", "places", COUNTERSUNK % "((20, True), (80, False))"),
                       ("no countersink", "places", COUNTERSUNK % "((20, None), (80, None))"),
                       ("50 apart", "places", COUNTERSUNK % "((25, True), (75, True))")],
    "16-gusset": [("the corner is not cut off", "volume", GUSSET % "((0, 0), (80, 0), (0, 60))"),
                  ("a sharp corner was cut off, not the right-angle corner", "volume",
                   GUSSET % "((0, 0), (65, 0), (65, 11.25), (0, 60))")],
    "17-angle-holes": [("the two holes on one side of the leg", "places", ANGLE_HOLES % {"ys": (12, 25), "r": 5.5}),
                       ("the hole in the 60 leg is 9, not 11", "features", ANGLE_HOLES % {"ys": (12, 38), "r": 4.5})],
    "18-stuetze": [("the tube is turned by 90 degrees", "places",
                    STUETZE % {"a": 40, "b": 60, "x": 40, "y": 20} + FUSE_STUETZE),
                   ("plate and tube are two bodies", "solids", STUETZE % {"a": 60, "b": 40, "x": 30, "y": 30}),
                   ("the tube is 10 off the middle", "places",
                    STUETZE % {"a": 60, "b": 40, "x": 40, "y": 30} + FUSE_STUETZE)],
    "19-square-flange": [("the four holes are on the axes, not towards the corners", "places",
                          SQUARE_FLANGE % repr([(0, 0, 20), (35, 0, 4.5), (-35, 0, 4.5), (0, 35, 4.5), (0, -35, 4.5)])),
                         ("no centre hole", "features",
                          SQUARE_FLANGE % repr([(K, K, 4.5), (K, -K, 4.5), (-K, K, 4.5), (-K, -K, 4.5)]))],
    "20-mitred-frame": [("one fused body", "solids",
                         FRAME + MITRED + 'Part.show(bars[0].fuse(bars[1:]), "Frame")\n'),
                        ("butt joints, no mitres", "bodies",
                         FRAME + 'bars = [tube(500), put(tube(500), 0, 0, 270), put(tube(240), 90, 30, 30), '
                                 'put(tube(240), 90, 500, 30)]\n' + SHOW_BARS),
                        ("the bars run past each other at the corners", "bodies",
                         FRAME + 'bars = [tube(500), put(tube(500), 0, 0, 270), put(tube(300), 90, 30, 0), '
                                 'put(tube(300), 90, 500, 0)]\n' + SHOW_BARS),
                        ("three bars", "solids", FRAME + MITRED + SHOW_BARS.replace("(bars)", "(bars[:3])"))],
}


def build(code):
    """A new document with the part that `code` makes. Returns (document, what run_python said)."""
    local_bench.close_all()
    local_tools.reset()
    doc = App.newDocument("Model")
    doc.UndoMode = 1
    return doc, local_tools.run_python(code)


def judged(req, code):
    doc, out = build(code)
    checks, part = local_bench.measure_and_check(req, doc)
    return out, checks, part


def not_ok(checks):
    return [c["check"] for c in checks if not c["ok"]]


def test_data():
    say("--- the 20 requests")
    ids = [r["id"] for r in REQUESTS]
    check("there are 20 requests, each with its own id", len(REQUESTS) == 20 and len(set(ids)) == 20, ids)
    check("each one is in one of the two groups", all(r["group"] in GROUPS for r in REQUESTS))
    raw = [r["id"] for r in REQUESTS if r["group"] == "raw"]
    check("at least 5 are for raw FreeCAD code", len(raw) >= 5, raw)
    check("each one has a reference solution and a wrong part",
          set(GOOD) == set(ids) and set(WRONG) == set(ids) and all(WRONG[i] for i in ids),
          set(ids) ^ set(GOOD) ^ set(WRONG))
    check("each one has its text and its numbers",
          all(r["text"].strip() and r["volume"] > 0 and len(r["box"]) == 3 and r["solids"] >= 1 for r in REQUESTS))
    check("the ids go up, from easy to hard", ids == sorted(ids), ids)


def test_references():
    for req in REQUESTS:
        say("--- %s" % req["id"])
        out, checks, part = judged(req, GOOD[req["id"]])
        check("%s: the reference solution runs" % req["id"], "ERROR" not in out, out)
        check("%s: the reference solution passes every check" % req["id"], checks and not not_ok(checks),
              bench_checks.first_failure(checks))
        for mistake, expected, code in WRONG[req["id"]]:
            out, checks, part = judged(req, code)
            bad = not_ok(checks)
            check("%s: %s -> fails '%s'" % (req["id"], mistake, expected),
                  "ERROR" not in out and expected in bad, (bad, bench_checks.first_failure(checks), out[-300:]))


def test_the_part():
    """Which objects count as the part."""
    say("--- which objects are the part")
    plate = [r for r in REQUESTS if r["id"] == "01-plate"][0]
    drilled_plate = [r for r in REQUESTS if r["id"] == "06-plate-4-holes"][0]
    rounded = [r for r in REQUESTS if r["id"] == "08-rounded-corners"][0]

    out, checks, part = judged(plate, GOOD["01-plate"] + BOX % ("FirstTry", "100, 80, 10"))
    check("a first try that was left in the document counts: two solids", "solids" in not_ok(checks), checks)

    out, checks, part = judged(plate, GOOD["01-plate"] + BOX % ("FirstTry", "100, 80, 10") + "b.Visibility = False\n")
    check("an object that is switched off does not count", not not_ok(checks) and part["objects"] == ["Plate"], checks)

    code = (GOOD["01-plate"] + 'plate = b\n' + BOX % ("Note", "1, 1, 1")
            + 'b.setExpression("Length", "Plate.Length")\nb.Visibility = False\n'
              'sk = doc.addObject("Sketcher::SketchObject", "Sketch")\n'
              'sk.AttachmentSupport = [(plate, "Face6")]\nsk.MapMode = "FlatFace"\n'
              'p = doc.addObject("App::VarSet", "Params")\n')
    out, checks, part = judged(plate, code)
    check("an expression, a sketch on a face and a VarSet do not use the plate up",
          "ERROR" not in out and not not_ok(checks) and part["objects"] == ["Plate"], (out, checks))

    # the cut at the level of shapes: the result is shown, the blank and the drills stay in the document
    shapes = ('s = Part.makeBox(100, 60, 8)\n'
              'for x in (10, 90):\n'
              '    for y in (10, 50):\n'
              '        s = s.cut(Part.makeCylinder(4.5, 8, App.Vector(x, y, 0)))\n'
              'Part.show(s, "Plate")\n')
    out, checks, part = judged(drilled_plate, BOX % ("Blank", "100, 60, 8") + shapes)
    check("a blank that stays beside the result counts: two solids", "solids" in not_ok(checks), checks)
    out, checks, part = judged(drilled_plate, shapes)
    check("the same part from shapes alone passes", not not_ok(checks), checks)

    out, checks, part = judged(rounded, GOOD["08-rounded-corners"])
    check("the base of a fillet is not the part, and is named as still shown in FreeCAD's window",
          part["objects"] == ["Plate"] and part["shown_inputs"] == ["Blank"], part)
    out, checks, part = judged(rounded, GOOD["08-rounded-corners"] + "box.Visibility = False\n")
    check("... and is not named when the code switched it off", part["shown_inputs"] == [] and not not_ok(checks), part)

    doc, out = build(BOX % ("Left", "10, 10, 10")
                     + 'm = doc.addObject("Part::Mirroring", "Right")\nm.Source = b\n'
                       'm.Base = App.Vector(20, 0, 0)\nm.Normal = App.Vector(1, 0, 0)\n')
    objects, shown = bench_checks.part_objects(doc)
    check("a mirror keeps its source: both are the part", sorted(o.Name for o in objects) == ["Left", "Right"],
          (out, [o.Name for o in objects]))

    doc, out = build("x = 1\n")
    checks, part = local_bench.measure_and_check(plate, doc)
    check("an empty document fails with 'no solid'", not_ok(checks) == ["solids"] and part is None
          and "no solid" in bench_checks.first_failure(checks), checks)
    checks, part = local_bench.measure_and_check(plate, None)
    check("no document at all fails the same way", not_ok(checks) == ["solids"], checks)

    # the part can lie anywhere and along any axis
    turned = (GOOD["06-plate-4-holes"]
              + 'doc.recompute()\ns = cut.Shape.copy()\n'
                's.Placement = App.Placement(App.Vector(300, -20, 55), App.Rotation(App.Vector(1, 0, 0), 90))\n'
                'doc.removeObject(cut.Name)\ndoc.removeObject(f.Name)\ndoc.removeObject(plate.Name)\n'
                'for t in tools:\n    doc.removeObject(t.Name)\n'
                'Part.show(s, "Plate")\n')
    out, checks, part = judged(drilled_plate, turned)
    check("the same plate, standing upright somewhere else, passes", "ERROR" not in out and not not_ok(checks),
          (out, checks))
    checks = judged(drilled_plate, drilled("100, 60, 8", corners(80, 40)[:3]))[1]
    line = bench_checks.first_failure([c for c in checks if c["check"] == "features"])
    check("the reason names the check and both numbers",
          line == "features: 3 x through hole d9; wanted 4 x through hole d9", line)


def test_engine():
    """The benchmark drives the real engine: a scripted server plays the model."""
    say("--- the benchmark and the engine")
    app, user = local_bench.setup()
    srv = FakeServer()
    config = local_bench.engine_config(srv.url, "fake-model")
    check("the settings are those of a local model, with the address and the model of the run",
          config == {"kind": "openai", "url": srv.url, "model": "fake-model", "key": ""}, config)
    plate = [r for r in REQUESTS if r["id"] == "01-plate"][0]

    srv.add(call("run_python", {"code": GOOD["01-plate"]}, "c1"), call("get_document_state", {}, "c2"),
            answer("The plate is ready."))
    n = len(srv.requests)
    kept = os.path.join(os.environ["FREECAD_USER_HOME"], "kept")
    sample = local_bench.run_request(app, plate, config, 30, "t-good", kept)
    first = srv.requests[n]["body"]
    check("a correct part passes: 3 model turns, 2 tool calls",
          sample["ok"] and sample["reason"] == "" and sample["turns"] == 3 and sample["tool_calls"] == 2
          and sample["answer"] == "The plate is ready.", sample)
    check("the engine sent the product's system prompt, the product's tools and the request as it is",
          first["messages"][0]["content"] == local_bench.local_prompt.system_prompt("build")
          and first["tools"] == local_bench.local_prompt.TOOLS
          and first["messages"][1:] == [{"role": "user", "content": plate["text"]}], str(first)[:300])
    check("the Run question was asked for the step that changes things, and the script said Run",
          user.asked == 1 and not local_bench.approval.auto(), user.asked)
    check("the sample keeps the conversation and the measured part",
          [m["role"] for m in sample["messages"]] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
          and sample["part"]["box"] == [120.0, 80.0, 10.0] and sample["part"]["volume"] == 96000.0, sample["part"])
    again = App.openDocument(os.path.join(kept, "t-good.FCStd"))
    check("a copy of the document is kept, and it measures the same",
          sample["model_file"] == "t-good.FCStd"
          and not not_ok(local_bench.measure_and_check(plate, again)[0]), sample["model_file"])

    srv.add(call("run_python", {"code": WRONG["01-plate"][0][2]}, "c1"), answer("Done."))
    n = len(srv.requests)
    sample = local_bench.run_request(app, plate, config, 30, "t-wrong")
    check("a wrong part fails with the check and both numbers",
          not sample["ok"] and sample["reason"] == "box: 8.00 x 80.00 x 120.00; wanted 10.00 x 80.00 x 120.00",
          sample["reason"])
    check("each request starts from nothing: a new conversation and an empty document",
          [m["role"] for m in srv.requests[n]["body"]["messages"]] == ["system", "user"]
          and sample["part"]["objects"] == ["Plate"], sample["part"])

    srv.add(call("run_python", {"code": GOOD["01-plate"]}, "c1"), call("get_document_state", {}, "c2"),
            call("get_document_state", {}, "c3"))
    sample = local_bench.run_request(app, plate, dict(config, max_steps=3), 30, "t-steps")
    check("the step limit fails the sample, and says that the part itself is right",
          not sample["ok"] and sample["reason"] == "step limit (3); the part passes the checks"
          and sample["part_ok"] and not sample["ended_well"], sample["reason"])

    srv.add(call("run_python", {"code": GOOD["01-plate"]}, "c1"), error(500, "the GPU ran out of memory"))
    sample = local_bench.run_request(app, plate, config, 30, "t-error")
    check("an error of the server fails the sample with its text",
          not sample["ok"] and sample["reason"].startswith("engine error: The model server reported an error")
          and "GPU ran out of memory" in sample["reason"], sample["reason"])

    srv.add(call("run_python", {"code": "1 / 0"}, "c1"), answer("Done."), answer("Done."), answer("It is done."))
    sample = local_bench.run_request(app, plate, config, 30, "t-problem")
    check("a problem that the model did not repair fails the sample",
          not sample["ok"] and sample["reason"].startswith("ended with a problem that the model did not repair; "
                                                           "solids: no solid in the document"), sample["reason"])

    srv.add(slow(20, answer("Late.")))
    sample = local_bench.run_request(app, plate, config, 1.5, "t-slow")
    check("a request that takes too long is stopped and fails",
          not sample["ok"] and sample["reason"].startswith("time limit of the benchmark (1 s)")
          and 1.4 < sample["seconds"] < 6, (sample["reason"], sample["seconds"]))

    srv.add(call("run_python", {"code": GOOD["01-plate"]}, "c1"), answer("Done."))
    again = local_bench.run_request(app, plate, config, 30, "t-good")
    check("a project name that was used before gets a new folder, not the old conversation",
          again["ok"] and [m["role"] for m in again["messages"]] == ["user", "assistant", "tool", "assistant"],
          again["messages"])

    problem = local_bench.warm_up(dict(config, url="http://127.0.0.1:9/v1"))
    check("a server that is not there is reported before the first request", "does not answer" in problem, problem)
    srv.add(answer("ready"))
    check("the warm-up question passes with a server that answers", local_bench.warm_up(config) == "")


def test_report():
    say("--- the table")
    def sample(i, group, number, ok, reason=""):
        return {"id": i, "group": group, "pass": number, "ok": ok, "reason": reason, "turns": 4, "tool_calls": 3,
                "seconds": 20.0, "part": {"shown_inputs": ["Blank"] if i == "08-b" else []}}
    samples = [sample("01-a", "helper", 1, True), sample("05-c", "raw", 1, False, "box: 1; wanted 2"),
               sample("08-b", "helper", 1, True), sample("01-a", "helper", 2, True),
               sample("05-c", "raw", 2, True), sample("08-b", "helper", 2, True)]
    line = local_bench.row(samples[1], 6)
    check("a row has the id, the group, FAIL, the steps, the seconds and the reason",
          line.split() == ["05-c", "raw", "FAIL", "4", "3", "20.0", "box:", "1;", "wanted", "2"], line)
    check("the head has a word for each column", len(local_bench.head(6).split("  ")) >= 7)
    lines = local_bench.totals(samples, 2)
    check("the totals: rate of all, of each pass with the difference, of each group, of each request",
          lines[0].startswith("pass rate: 5 of 6 (83 %)    mean 4.0 model turns, 3.0 tool calls, 20 s")
          and lines[1] == "each pass: 2 of 3, 3 of 3    difference between the passes: 1"
          and lines[2] == "group helper: 4 of 4 (100 %)" and lines[3] == "group raw   : 1 of 2 (50 %)"
          and lines[4] == "each request, passed of 2: 01 2  05 1  08 2" and lines[5].startswith("note: in 2 samples"),
          lines)
    numbers = local_bench.summary(samples, 2)
    check("the same numbers for the JSON file",
          numbers["passed"] == 5 and numbers["samples"] == 6 and numbers["each_pass"] == [2, 3]
          and numbers["groups"] == {"helper": {"passed": 4, "samples": 4}, "raw": {"passed": 1, "samples": 2}}
          and numbers["requests"] == {"01-a": 2, "05-c": 1, "08-b": 2}, numbers)
    check("a selection takes ids or parts of ids",
          [r["id"] for r in local_bench.selected("01, flange")] == ["01-plate", "10-flange", "19-square-flange"]
          and len(local_bench.selected("")) == 20 and local_bench.selected("nothing") == [])
    local_bench._key[0] = "sk-made-up-key"
    path = os.path.join(os.environ["FREECAD_USER_HOME"], "report.json")
    local_bench.write_json(path, {"answer": "the key is sk-made-up-key", "sign": "Ø 9"})
    with open(path, encoding="utf-8") as f:
        text = f.read()
    local_bench._key[0] = ""
    check("the key of the run is never written to the results", "sk-made-up" not in text and "(the key)" in text
          and "Ø 9" in text, text)


code = 1
try:
    test_data()
    test_references()
    test_the_part()
    test_engine()
    test_report()
    say("")
    say("%d checks passed, %d failed" % (len(passed), len(failed)))
    for name in failed:
        say("FAILED: " + name)
    code = 1 if failed else 0
except Exception:
    import traceback
    say("EXCEPTION in the test:\n" + traceback.format_exc())
sys.stdout.flush()
os._exit(code)
