"""The 20 build requests of local_bench.py, and the numbers that a correct part has.

Pure data, no FreeCAD. A request is what a metalworker, a fitter or an
apprentice types: his words, millimetres, every size that the checks need and
no word about how to model it. They go from easy to hard. Three are German.

Two groups:
    "helper"   a function of the helper library (cadprompt/helpers.py: plate,
               hole pattern, slot, stepped shaft, hollow profile, mitred frame,
               fillet and chamfer) fits the job
    "raw"      the shape, or a main feature of it, is in none of them (an angle,
               a hexagon, a triangle, a counterbore, a countersink): the model
               has to write FreeCAD code for it

The numbers of each request are in its own frame: "box" is the size along x, y
and z, and every place ("at", "com", a cut) is measured from the middle of
that box. bench_checks.py lays this frame on the document in every possible
way, so the model is free to build the part anywhere and along any axis.
Every volume is computed here by its formula; the formula is in the comment.

Do not change a request or a number after a baseline was measured with it:
the pass rates of two runs compare only with the same 20.
"""

import math

PI = math.pi


def hole(d, at, axis="z", length=None, through=True):
    """A hole of diameter d. `at` is the middle of its wall, `length` the length of the wall."""
    return {"kind": "hole", "d": d, "at": at, "axis": axis, "length": length, "through": through}


def cylinder(d, at, axis, length):
    """An outside cylinder: a step of a shaft, the rim of a disc."""
    return {"kind": "cylinder", "d": d, "at": at, "axis": axis, "length": length}


def rounding(r, at, axis, length, span=90.0):
    """A rounded outside corner. `at` is the middle of the axis of the arc."""
    return {"kind": "rounding", "d": 2 * r, "at": at, "axis": axis, "length": length, "span": span}


def arc(r, at, axis, length, span=180.0):
    """A round inside wall that is not a full hole: the end of a slot."""
    return {"kind": "arc", "d": 2 * r, "at": at, "axis": axis, "length": length, "span": span}


def centre_of_mass(pieces, middle):
    """The centre of mass of pieces [(volume, (x, y, z))] (a negative volume is a cut-out),
    measured from `middle`, the middle of the bounding box."""
    total = sum(v for v, _ in pieces)
    return tuple(sum(v * c[i] for v, c in pieces) / total - middle[i] for i in range(3))


def frustum(d1, d2, h):
    """Volume of a cone between the diameters d1 and d2: pi h / 12 (d1^2 + d1 d2 + d2^2)."""
    return PI * h / 12 * (d1 * d1 + d1 * d2 + d2 * d2)


def disc(d, h):
    """Volume of a cylinder: pi / 4 d^2 h."""
    return PI / 4 * d * d * h


BOLT_CIRCLE_90 = [(45 * math.cos(math.radians(60 * k)), 45 * math.sin(math.radians(60 * k)), 0.0) for k in range(6)]
AT_45 = 35 * math.sqrt(0.5)        # a hole on a bolt circle of 70, towards a corner

REQUESTS = [
    {"id": "01-plate", "group": "helper",
     "text": "Make a plate 120 x 80 x 10 mm.",
     "solids": 1, "box": (120, 80, 10),
     "volume": 120 * 80 * 10,                                        # l w t
     "features": []},

    {"id": "02-spacer", "group": "helper",
     "text": "I need a spacer sleeve: outside diameter 20, bore 10.5, 15 long.",
     "solids": 1, "box": (20, 20, 15), "spin": "z",
     "volume": disc(20, 15) - disc(10.5, 15),                        # pi/4 (D^2 - d^2) l
     "features": [cylinder(20, (0, 0, 0), "z", 15), hole(10.5, (0, 0, 0), "z", 15)]},

    {"id": "03-vierkantrohr", "group": "helper",
     "text": "Vierkantrohr 40 x 40 x 3, 250 mm lang, scharfkantig (ohne Eckenradius).",
     "solids": 1, "box": (40, 40, 250),
     "volume": (40 * 40 - 34 * 34) * 250,                            # (a^2 - (a - 2t)^2) l
     "features": []},

    {"id": "04-shaft-2", "group": "helper",
     "text": "Turned part with two diameters: 30 mm diameter for a length of 40, "
             "then 20 mm diameter for a length of 60.",
     "solids": 1, "box": (30, 30, 100), "spin": "z",
     "volume": disc(30, 40) + disc(20, 60),                          # the two cylinders
     # along z from 0 to 100, the middle is at 50
     "features": [cylinder(30, (0, 0, -30), "z", 40), cylinder(20, (0, 0, 20), "z", 60)]},

    {"id": "05-angle", "group": "raw",
     "text": "Angle bracket from 5 mm thick steel: one leg 60 long, the other 40 long (both measured on "
             "the outside), 30 wide. Sharp corners, no bend radius, no holes.",
     "solids": 1, "box": (60, 30, 40),
     "volume": (60 + 40 - 5) * 5 * 30,                               # (a + b - t) t w
     "features": [],
     # the leg along x (60 x 30 x 5) and the rest of the leg along z (5 x 30 x 35, from z = 5)
     "com": centre_of_mass([(60 * 30 * 5, (30, 15, 2.5)), (5 * 30 * 35, (2.5, 15, 22.5))], (30, 15, 20))},

    {"id": "06-plate-4-holes", "group": "helper",
     "text": "Plate 100 x 60 x 8 with four through holes diameter 9, one in each corner, "
             "10 mm from both edges.",
     "solids": 1, "box": (100, 60, 8),
     "volume": 100 * 60 * 8 - 4 * disc(9, 8),                        # the plate less 4 holes
     "features": [hole(9, (x, y, 0), "z", 8) for x in (-40, 40) for y in (-20, 20)]},

    {"id": "07-hexagon", "group": "raw",
     "text": "Hexagon spacer: 24 across the flats, 30 long, with a through hole diameter 13 in the centre.",
     # across the corners: s / cos 30 = 27.713
     "solids": 1, "box": (24, 24 / math.cos(math.radians(30)), 30),
     "volume": (math.sqrt(3) / 2 * 24 * 24) * 30 - disc(13, 30),     # hexagon: sqrt(3)/2 s^2
     "features": [hole(13, (0, 0, 0), "z", 30)]},

    {"id": "08-rounded-corners", "group": "helper",
     "text": "Cover plate 150 x 100 x 6. Round all four corners with radius 15.",
     "solids": 1, "box": (150, 100, 6),
     "volume": (150 * 100 - (4 - PI) * 15 * 15) * 6,                 # a corner loses r^2 (1 - pi/4)
     "features": [rounding(15, (x, y, 0), "z", 6) for x in (-60, 60) for y in (-35, 35)]},

    {"id": "09-chamfered-corners", "group": "helper",
     "text": "Plate 80 x 80 x 10 with the four corners cut off: chamfer 10 x 45 degrees.",
     "solids": 1, "box": (80, 80, 10),
     "volume": (80 * 80 - 4 * 10 * 10 / 2) * 10,                     # a corner loses c^2 / 2
     "features": [],
     "com": (0.0, 0.0, 0.0)},

    {"id": "10-flange", "group": "helper",
     "text": "Flange: disc diameter 120, 12 thick, centre bore diameter 50, and 6 holes diameter 11 "
             "equally spaced on a bolt circle of 90.",
     "solids": 1, "box": (120, 120, 12), "spin": "z",
     "volume": disc(120, 12) - disc(50, 12) - 6 * disc(11, 12),
     "features": [cylinder(120, (0, 0, 0), "z", 12), hole(50, (0, 0, 0), "z", 12)]
                 + [hole(11, at, "z", 12) for at in BOLT_CIRCLE_90]},

    {"id": "11-hole-grid", "group": "helper",
     "text": "Base plate 160 x 100 x 12 with 6 through holes diameter 9: 3 holes along the 160 side with "
             "40 pitch, in 2 rows that are 50 apart. The hole pattern is centred on the plate.",
     "solids": 1, "box": (160, 100, 12),
     "volume": 160 * 100 * 12 - 6 * disc(9, 12),
     "features": [hole(9, (x, y, 0), "z", 12) for x in (-40, 0, 40) for y in (-25, 25)]},

    {"id": "12-welle-3", "group": "helper",
     "text": "Welle mit drei Absätzen hintereinander auf einer Achse: Durchmesser 25 auf 30 Länge, dann "
             "Durchmesser 40 auf 50 Länge, dann Durchmesser 30 auf 45 Länge.",
     "solids": 1, "box": (40, 40, 125), "spin": "z",
     "volume": disc(25, 30) + disc(40, 50) + disc(30, 45),
     # along z: 0 to 30, 30 to 80, 80 to 125; the middle is at 62.5
     "features": [cylinder(25, (0, 0, -47.5), "z", 30), cylinder(40, (0, 0, -7.5), "z", 50),
                  cylinder(30, (0, 0, 40), "z", 45)]},

    {"id": "13-counterbore", "group": "raw",
     "text": "Clamping block 60 x 40 x 20. In the middle of the 60 x 40 face: a through hole diameter 9 "
             "with a counterbore diameter 15, 9 deep.",
     "solids": 1, "box": (60, 40, 20),
     "volume": 60 * 40 * 20 - disc(15, 9) - disc(9, 11),             # counterbore 9 deep, the hole in the other 11
     # z from -10 to 10: the counterbore from 1 to 10, the hole from -10 to 1
     "features": [hole(15, (0, 0, 5.5), "z", 9), hole(9, (0, 0, -4.5), "z", 11)]},

    {"id": "14-slot-bar", "group": "helper",
     "text": "Flat bar 120 x 40 x 6. On the centre line a hole diameter 9, 20 from one end. Also on the "
             "centre line a slot with round ends, 9 wide and 40 long overall; the middle of the slot is "
             "35 from the other end.",
     "solids": 1, "box": (120, 40, 6),
     # the slot: two half circles and a rectangle 9 x (40 - 9)
     "volume": (120 * 40 - PI / 4 * 81 - (PI / 4 * 81 + 9 * 31)) * 6,
     # x from -60 to 60: the hole at -40, the slot around 25, its arcs at 25 -/+ 15.5
     "features": [hole(9, (-40, 0, 0), "z", 6), arc(4.5, (9.5, 0, 0), "z", 6), arc(4.5, (40.5, 0, 0), "z", 6)]},

    {"id": "15-countersunk", "group": "raw",
     "text": "Strip 100 x 25 x 6 with two countersunk holes on the centre line, 60 apart, symmetric to "
             "the middle: through hole diameter 5.5, countersink 90 degrees (included angle) to diameter "
             "11 at the surface. Both countersinks on the same side.",
     "solids": 1, "box": (100, 25, 6),
     # 90 degrees: the cone is (11 - 5.5) / 2 = 2.75 deep; the hole is in the other 3.25
     "volume": 100 * 25 * 6 - 2 * (frustum(11, 5.5, 2.75) + disc(5.5, 3.25)),
     # z from -3 to 3: the cone from 0.25 to 3, the hole from -3 to 0.25
     "features": [hole(5.5, (x, 0, -1.375), "z", 3.25) for x in (-30, 30)]},

    {"id": "16-gusset", "group": "raw",
     "text": "Gusset plate, 8 thick: a right-angled triangle with legs 80 and 60. Cut the right-angle "
             "corner off, 15 x 15 at 45 degrees.",
     "solids": 1, "box": (80, 60, 8),
     "volume": (80 * 60 / 2 - 15 * 15 / 2) * 8,                      # the triangle less the small triangle
     "features": [],
     # the triangle (0,0) (80,0) (0,60) less the triangle (0,0) (15,0) (0,15)
     "com": centre_of_mass([(80 * 60 / 2 * 8, (80 / 3, 20, 4)), (-15 * 15 / 2 * 8, (5, 5, 4))], (40, 30, 4))},

    {"id": "17-angle-holes", "group": "raw",
     "text": "Angle bracket from 6 mm steel, sharp corners: legs 80 and 60 long (outside), 50 wide. In the "
             "80 leg two holes diameter 9, each 20 from the free end of the leg and 12 from a side edge "
             "(one near each side). In the 60 leg one hole diameter 11, 20 from the free end, in the "
             "middle of the width.",
     "solids": 1, "box": (80, 50, 60),
     "volume": (80 + 60 - 6) * 6 * 50 - 2 * disc(9, 6) - disc(11, 6),
     # the 80 leg along x (z from 0 to 6), the 60 leg along z (x from 0 to 6), the width along y;
     # the middle of the box is (40, 25, 30)
     "features": [hole(9, (20, -13, -27), "z", 6), hole(9, (20, 13, -27), "z", 6),
                  hole(11, (-37, 0, 10), "x", 6)]},

    {"id": "18-stuetze", "group": "helper",
     "text": "Stütze als Schweißteil, ein einziger Körper: Rechteckrohr 60 x 40 x 4 (scharfkantig), 200 "
             "lang, steht mittig auf einer Fußplatte 120 x 100 x 8. Die 60er Seite des Rohrs ist parallel "
             "zur 120er Seite der Platte.",
     "solids": 1, "box": (120, 100, 208),
     "volume": 120 * 100 * 8 + (60 * 40 - 52 * 32) * 200,            # the plate and the tube
     "features": [],
     # the plate from z = 0 to 8, the tube from 8 to 208; the middle of the box is at z = 104
     "com": centre_of_mass([(120 * 100 * 8, (0, 0, 4)), ((60 * 40 - 52 * 32) * 200, (0, 0, 108))], (0, 0, 104)),
     "cuts": [{"across": "z", "at": 50, "size": (60, 40), "area": 60 * 40 - 52 * 32},
              {"across": "z", "at": -100, "size": (120, 100), "area": 120 * 100}]},

    {"id": "19-square-flange", "group": "helper",
     "text": "Square flange plate 100 x 100 x 10 with the four corners rounded, radius 12. Centre hole "
             "diameter 40. Four holes diameter 9 on a bolt circle of 70, one towards each corner "
             "(at 45 degrees).",
     "solids": 1, "box": (100, 100, 10),
     "volume": (100 * 100 - (4 - PI) * 12 * 12) * 10 - disc(40, 10) - 4 * disc(9, 10),
     "features": [hole(40, (0, 0, 0), "z", 10)]
                 + [hole(9, (x, y, 0), "z", 10) for x in (-AT_45, AT_45) for y in (-AT_45, AT_45)]
                 + [rounding(12, (x, y, 0), "z", 10) for x in (-38, 38) for y in (-38, 38)]},

    {"id": "20-mitred-frame", "group": "helper",
     "text": "Frame from square tube 30 x 30 x 2 (sharp corners), outside size 500 x 300, corners mitred "
             "at 45 degrees. Keep the four bars as four separate solids, not fused.",
     "solids": 4, "box": (500, 300, 30),
     # a bar with two mitres is as long as its centre line: outside length less the width of the tube
     "volume": (30 * 30 - 26 * 26) * (2 * (500 - 30) + 2 * (300 - 30)),
     "volumes": [(30 * 30 - 26 * 26) * (500 - 30)] * 2 + [(30 * 30 - 26 * 26) * (300 - 30)] * 2,
     "features": []},
]

GROUPS = ("helper", "raw")
