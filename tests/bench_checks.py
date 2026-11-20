"""Measures the part in a FreeCAD document and compares it with the numbers of a request.

Helper for local_bench.py and test_local_bench.py. Needs FreeCAD, no window.

Which objects are "the part": every object that has a solid, that is not
switched off (Visibility) and that no other solid object is made from. A box
that a Part::Cut uses as its Base is not the part, the cut is. A tool cylinder
that was never used for a cut is a part of the result, and so is a first try
that was left in the document: the user sees them. The inputs are found
through the link properties (Base, Tool, Shapes, Group, ...), not through
expressions and not through the face that a sketch sits on. A mirror keeps its
source (Part::Mirroring makes a second body). FreeCAD's window hides the
inputs of a cut, a fuse and a compound itself; it does not hide the base of a
Part::Fillet, Part::Chamfer or Part::Mirroring (measured with 1.1.4).
Without a window nothing is hidden, so the rule here is the same for all of
them, and part_objects() also gives the names that the window would still
show (`shown_inputs`), for the report.

What is compared, all of it whatever the place of the part and whichever axis
it lies along (turned by right angles, mirrored):

    solids      the number of solids of the part
    box         the three sizes of the bounding box, sorted
    volume      against the number that the request computed by formula
    bodies      the volume of each solid, when the part has more than one
    joined      the solids touch and do not overlap
    features    every cylindrical face of the part: holes (with "through" or
                "blind"), outside cylinders, rounded corners, inside arcs,
                each with its diameter. One too many fails as one too few.
    places      where they are and how long: measured from the middle of the
                bounding box, in the frame of the request. Also the centre of
                mass and the size of a cut through the part, where a request
                gives them.

A hole is found from the geometry, never from a name: cylindrical faces whose
material is outside, with the same axis and radius, that close the full circle.
"through" means that nothing of the part is on the axis of the hole, from one
side of the part to the other (so a counterbore counts as through, and a hole
with a floor does not).
"""

import itertools
import math

import FreeCAD as App
import Part

TOL_SIZE = 0.05      # mm: sizes of the bounding box, the length of a hole or a step
TOL_PLACE = 0.1      # mm: the place of a hole, of the centre of mass
TOL_DIAMETER = 0.02  # mm
TOL_ANGLE = 2.0      # degrees: how far round an arc goes
TOL_VOLUME = 0.001   # of the volume: the shapes are exact, so 0.1 % is wide enough
FULL = 359.0         # degrees: from here on an arc is a full cylinder

ATTACHED = ("AttachmentSupport", "Support", "_Body")     # links that do not use an object up
COPIES = ("Part::Mirroring",)                             # their source stays a body of its own
NOT_HIDDEN = ("Part::Fillet", "Part::Chamfer", "Part::Mirroring")   # FreeCAD's window leaves their input on
AXES = {"x": 0, "y": 1, "z": 2}


# ---- which objects are the part ------------------------------------------------
def _solids(obj):
    shape = getattr(obj, "Shape", None)
    try:
        return list(shape.Solids) if shape is not None and not shape.isNull() else []
    except Exception:
        return []


def _linked(value):
    """The document objects in the value of a link property, whatever its kind."""
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _linked(item)
    elif isinstance(value, App.DocumentObject):
        yield value


def _inputs(obj):
    """The objects that `obj` is made from."""
    out = []
    for name in obj.PropertiesList:
        if name in ATTACHED or "Link" not in obj.getTypeIdOfProperty(name):
            continue
        try:
            targets = list(_linked(obj.getPropertyByName(name)))
        except Exception:
            continue
        for target in targets:
            # a feature of a body points back at its body: that is its place, not its input
            if target is not obj and obj not in (getattr(target, "Group", None) or []):
                out.append(target)
    return out


def part_objects(doc):
    """(the objects that are the part, names of inputs that FreeCAD's window would still show)."""
    solid = [o for o in doc.Objects if _solids(o)]
    used, shown = set(), set()
    for obj in solid:
        if obj.TypeId in COPIES:
            continue
        for target in _inputs(obj):
            used.add(target.Name)
            if obj.TypeId in NOT_HIDDEN and getattr(target, "Visibility", True) and _solids(target):
                shown.add(target.Name)
    part = [o for o in solid if o.Name not in used and getattr(o, "Visibility", True)]
    return part, sorted(shown)


# ---- cylinders -------------------------------------------------------------------
def _edge_points(face):
    points = [v.Point for v in face.Vertexes]
    for edge in face.Edges:
        try:
            points += edge.discretize(9)
        except Exception:
            pass                     # an edge without length (the tip of a cone)
    return points


def _cylinder_faces(solids):
    out = []
    for solid in solids:
        for face in solid.Faces:
            surf = face.Surface
            if not isinstance(surf, Part.Cylinder):
                continue
            u0, u1, v0, v1 = face.ParameterRange
            axis = App.Vector(surf.Axis)
            axis.normalize()
            spoke = surf.value((u0 + u1) / 2, (v0 + v1) / 2) - surf.Center
            spoke = spoke - axis * spoke.dot(axis)             # from the axis to the surface
            # the normal of a face points out of the material
            convex = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2).dot(spoke) > 0
            out.append({"r": surf.Radius, "axis": axis, "convex": convex, "span": u1 - u0,
                        "foot": surf.Center - axis * surf.Center.dot(axis),     # the point of the axis next to the origin
                        "points": _edge_points(face)})
    return out


def _same_cylinder(a, b):
    return (a["convex"] == b["convex"] and abs(a["r"] - b["r"]) < 1e-4
            and abs(abs(a["axis"].dot(b["axis"])) - 1) < 1e-7 and (a["foot"] - b["foot"]).Length < 1e-3)


def _see_through(solids, foot, axis, box):
    """True when nothing of the part is on this axis, from one side of the part to the other."""
    along = [App.Vector(x, y, z).dot(axis) for x in (box.XMin, box.XMax) for y in (box.YMin, box.YMax)
             for z in (box.ZMin, box.ZMax)]
    low, high = min(along) - 1, max(along) + 1
    steps = min(4000, max(50, int((high - low) / 0.2)))
    for i in range(steps + 1):
        point = foot + axis * (low + (high - low) * i / steps)
        for solid in solids:
            if solid.BoundBox.isInside(point) and solid.isInside(point, 1e-6, False):
                return False
    return True


def _features(solids, box):
    """Every cylinder of the part: {"kind", "d", "span", "length", "through", "at", "axis"}.

    kind: "hole" (material outside, full circle), "arc" (material outside, a part
    of the circle: the end of a slot, an inside corner), "cylinder" (material
    inside, full circle), "rounding" (material inside, a part of the circle).
    "at" is the middle of its axis, measured from the middle of the bounding box.
    """
    groups = []
    for face in _cylinder_faces(solids):
        group = next((g for g in groups if _same_cylinder(g[0], face)), None)
        if group is None:
            groups.append([face])
        else:
            group.append(face)
    centre, out = box.Center, []
    for group in groups:
        axis, foot = group[0]["axis"], group[0]["foot"]
        pieces = sorted(((min(ts), max(ts), f["span"]) for f, ts in
                         ((f, [p.dot(axis) for p in f["points"]]) for f in group)), key=lambda p: p[0])
        runs = []                    # faces one after the other along the axis are one feature
        for piece in pieces:
            if runs and piece[0] <= runs[-1][1] + 1e-3:
                runs[-1][1] = max(runs[-1][1], piece[1])
                runs[-1][2].append(piece)
            else:
                runs.append([piece[0], piece[1], [piece]])
        for start, end, members in runs:
            # how far round: the faces that lie side by side at one height, added up
            stations = [start + (end - start) * (k + 0.5) / 5 for k in range(5)]
            span = max(sum(p[2] for p in members if p[0] - 1e-6 <= t <= p[1] + 1e-6) for t in stations)
            span = min(360.0, math.degrees(span))
            full, convex = span >= FULL, group[0]["convex"]
            kind = ("cylinder" if full else "rounding") if convex else ("hole" if full else "arc")
            middle = foot + axis * ((start + end) / 2)
            feature = {"kind": kind, "d": 2 * group[0]["r"], "span": 360.0 if full else span,
                       "length": end - start, "at": tuple(middle - centre), "axis": tuple(axis)}
            if kind == "hole":
                feature["through"] = _see_through(solids, foot, axis, box)
            out.append(feature)
    return out


# ---- measure ---------------------------------------------------------------------
def measure(doc):
    """The numbers of the part in `doc`, or None when the document has no solid."""
    objects, shown = part_objects(doc) if doc is not None else ([], [])
    solids = [s for o in objects for s in _solids(o)]
    if not solids:
        return None
    whole = Part.makeCompound(solids)
    box = whole.BoundBox
    volume = sum(s.Volume for s in solids)
    mass = App.Vector()
    for s in solids:
        mass += App.Vector(s.CenterOfMass) * s.Volume
    return {"objects": [o.Name for o in objects], "shown_inputs": shown, "solids": solids, "bound": box,
            "box": (box.XLength, box.YLength, box.ZLength), "volume": volume,
            "volumes": sorted(s.Volume for s in solids), "valid": all(s.isValid() for s in solids),
            "features": _features(solids, box), "com": tuple(mass * (1.0 / volume) - box.Center)}


def summary(m):
    """The measured numbers as plain data, for the JSON report."""
    if m is None:
        return None
    return {"objects": m["objects"], "shown_inputs": m["shown_inputs"], "solids": len(m["solids"]),
            "box": [round(v, 3) for v in m["box"]], "volume": round(m["volume"], 3),
            "volumes": [round(v, 3) for v in m["volumes"]], "com": [round(v, 3) for v in m["com"]],
            "features": [dict(f, d=round(f["d"], 3), span=round(f["span"], 1), length=round(f["length"], 3),
                              at=[round(v, 3) for v in f["at"]], axis=[round(v, 4) for v in f["axis"]])
                         for f in m["features"]]}


# ---- compare ---------------------------------------------------------------------
def _name(f):
    """A feature in words: "through hole d9", "rounding r15 (90 deg)"."""
    if f["kind"] == "hole":
        return "%s hole d%g" % ("through" if f.get("through", True) else "blind", round(f["d"], 2))
    if f["kind"] == "cylinder":
        return "cylinder d%g" % round(f["d"], 2)
    return "%s r%g (%d deg)" % (f["kind"], round(f["d"] / 2, 2), round(f["span"]))


def _count(features):
    names = {}
    for f in features:
        names[_name(f)] = names.get(_name(f), 0) + 1
    return ", ".join("%d x %s" % (n, name) for name, n in sorted(names.items())) or "none"


def _point(values):
    return "(%s)" % ", ".join("%.2f" % v for v in values)


def _turned(v, spin, angle):
    """v turned by `angle` about the axis with the index `spin`."""
    if spin is None or not angle:
        return tuple(v)
    a, b = [k for k in range(3) if k != spin]
    out = list(v)
    out[a] = v[a] * math.cos(angle) - v[b] * math.sin(angle)
    out[b] = v[a] * math.sin(angle) + v[b] * math.cos(angle)
    return tuple(out)


def _frames(box, found_box):
    """Every way to lay the axes of the request on the axes of the document so that the
    box fits: (perm, signs), where document axis k carries request axis perm[k]."""
    for perm in itertools.permutations(range(3)):
        if all(abs(box[perm[k]] - found_box[k]) <= TOL_SIZE for k in range(3)):
            for signs in itertools.product((1, -1), repeat=3):
                yield perm, signs


def _put(v, frame):
    perm, signs = frame
    return tuple(signs[k] * v[perm[k]] for k in range(3))


def _back(v, frame):
    perm, signs = frame
    out = [0.0, 0.0, 0.0]
    for k in range(3):
        out[perm[k]] = signs[k] * v[k]
    return tuple(out)


def _alike(want, found):
    if want["kind"] != found["kind"] or abs(want["d"] - found["d"]) > TOL_DIAMETER:
        return False
    if abs(want.get("span", 360.0) - found["span"]) > TOL_ANGLE:
        return False
    return want.get("through", True) == found.get("through", True)


def _distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _cut_through(m, k, where):
    """(area, sizes along the three document axes) of the part where the plane across
    document axis k at `where` cuts it: a slab 0.02 mm thick stands for the plane."""
    key = (k, round(where, 4))
    cache = m.setdefault("_cuts", {})
    if key not in cache:
        box, thick = m["bound"], 0.02
        low = [box.XMin - 1, box.YMin - 1, box.ZMin - 1]
        size = [box.XLength + 2, box.YLength + 2, box.ZLength + 2]
        low[k], size[k] = where - thick / 2, thick
        slab = Part.makeBox(size[0], size[1], size[2], App.Vector(*low))
        area, lows, highs = 0.0, [], []
        for solid in m["solids"]:
            piece = solid.common(slab)
            if not piece.isNull() and piece.Volume > 1e-9:
                area += piece.Volume / thick
                b = piece.BoundBox
                lows.append((b.XMin, b.YMin, b.ZMin))
                highs.append((b.XMax, b.YMax, b.ZMax))
        sizes = tuple(max(h[i] for h in highs) - min(lo[i] for lo in lows) for i in range(3)) if lows else (0, 0, 0)
        cache[key] = (area, sizes)
    return cache[key]


def _places(spec, m, frame, angle):
    """(number of things in their place, the first thing that is not) for one way to lay
    the request on the document."""
    spin = AXES.get(spec.get("spin"))
    free, good, first = list(m["features"]), 0, ""
    for want in spec.get("features", []):
        at = _put(_turned(want["at"], spin, angle), frame)
        axis = [0.0, 0.0, 0.0]
        axis[AXES[want["axis"]]] = 1.0
        axis = _put(_turned(axis, spin, angle), frame)
        best = None
        for f in free:
            if not _alike(want, f) or abs(abs(sum(a * b for a, b in zip(axis, f["axis"]))) - 1) > 1e-4:
                continue
            off = _distance(at, f["at"])
            if best is None or off < best[0]:
                best = (off, f)
        length = want.get("length")
        if best and best[0] <= TOL_PLACE and (length is None or abs(length - best[1]["length"]) <= TOL_SIZE):
            free.remove(best[1])
            good += 1
        elif not first:
            if best is None:
                first = "%s: none along that axis" % _name(want)
            elif best[0] > TOL_PLACE:
                first = "%s: wanted at %s from the middle of the part, the nearest is %.2f mm away" % (
                    _name(want), _point(want["at"]), best[0])
            else:
                first = "%s: its length is %.2f, wanted %.2f" % (_name(want), best[1]["length"], length)
    if "com" in spec:
        at = _put(_turned(spec["com"], spin, angle), frame)
        if _distance(at, m["com"]) <= TOL_PLACE:
            good += 1
        elif not first:
            first = "centre of mass: %.2f mm from where it must be, %s from the middle of the part" % (
                _distance(at, m["com"]), _point(spec["com"]))
    for cut in spec.get("cuts", []):
        perm, signs = frame
        j = AXES[cut["across"]]
        k = perm.index(j)
        area, sizes = _cut_through(m, k, m["bound"].Center[k] + signs[k] * cut["at"])
        others = [i for i in range(3) if i != j]
        found = tuple(sizes[perm.index(i)] for i in others)
        if abs(area - cut["area"]) <= max(TOL_VOLUME * cut["area"], 0.05) \
                and all(abs(a - b) <= TOL_SIZE for a, b in zip(found, cut["size"])):
            good += 1
        elif not first:
            first = "cut across %s at %g: %.2f x %.2f with area %.1f, wanted %g x %g with area %.1f" % (
                cut["across"], cut["at"], found[0], found[1], area, cut["size"][0], cut["size"][1], cut["area"])
    return good, first


def _angles(spec, m, frame):
    """The angles to try about the axis that a round part can be turned about."""
    spin = AXES.get(spec.get("spin"))
    if spin is None:
        return [0.0]
    a, b = [k for k in range(3) if k != spin]
    lead = next((w for w in spec.get("features", []) if math.hypot(w["at"][a], w["at"][b]) > TOL_PLACE), None)
    if lead is None:
        return [0.0]
    out = []
    for f in m["features"]:
        if _alike(lead, f):
            at = _back(f["at"], frame)
            out.append(math.atan2(at[b], at[a]) - math.atan2(lead["at"][b], lead["at"][a]))
    return out or [0.0]


def _joined(solids):
    """'' when every solid touches another one and no two overlap, else what is wrong."""
    n = len(solids)
    reach, overlap = {0}, 0.0
    touch = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            if solids[i].distToShape(solids[j])[0] < 0.01:
                touch[i].add(j)
                touch[j].add(i)
                try:
                    overlap = max(overlap, solids[i].common(solids[j]).Volume)
                except Exception:
                    pass
    todo = [0]
    while todo:
        for j in touch[todo.pop()]:
            if j not in reach:
                reach.add(j)
                todo.append(j)
    if len(reach) < n:
        return "%d of %d solids do not touch the others" % (n - len(reach), n)
    if overlap > 1.0:
        return "two solids overlap by %.0f mm3" % overlap
    return ""


def check(spec, m):
    """The checks of one request on a measured part: [{"check", "ok", "found", "wanted"}].

    `spec` is an entry of bench_requests.REQUESTS, `m` is measure(doc). The
    first entry that is not ok is the reason that the benchmark prints.
    """
    if m is None:
        return [{"check": "solids", "ok": False, "found": "no solid in the document",
                 "wanted": "%d" % spec["solids"]}]
    out = []

    def add(name, ok, found, wanted):
        out.append({"check": name, "ok": bool(ok), "found": found, "wanted": wanted})

    add("solids", len(m["solids"]) == spec["solids"] and m["valid"],
        "%d%s (%s)" % (len(m["solids"]), "" if m["valid"] else ", not valid", ", ".join(m["objects"])),
        "%d" % spec["solids"])
    box, found_box = sorted(spec["box"]), sorted(m["box"])
    add("box", all(abs(a - b) <= TOL_SIZE for a, b in zip(box, found_box)),
        " x ".join("%.2f" % v for v in found_box), " x ".join("%.2f" % v for v in box))
    tolerance = spec.get("volume_tolerance", TOL_VOLUME) * spec["volume"]
    add("volume", abs(m["volume"] - spec["volume"]) <= tolerance, "%.1f" % m["volume"], "%.1f" % spec["volume"])
    if "volumes" in spec:
        wanted = sorted(spec["volumes"])
        add("bodies", len(wanted) == len(m["volumes"])
            and all(abs(a - b) <= TOL_VOLUME * a for a, b in zip(wanted, m["volumes"])),
            ", ".join("%.1f" % v for v in m["volumes"]), ", ".join("%.1f" % v for v in wanted))
    if spec["solids"] > 1 and len(m["solids"]) > 1:
        problem = _joined(m["solids"])
        add("joined", not problem, problem or "the solids touch", "the solids touch and do not overlap")
    wanted = spec.get("features", [])
    add("features", _count(wanted) == _count(m["features"]), _count(m["features"]), _count(wanted))
    things = len(wanted) + ("com" in spec) + len(spec.get("cuts", []))
    if things:
        best = (-1, "the bounding box does not fit, so the places were not compared")
        for frame in _frames(spec["box"], m["box"]):
            for angle in _angles(spec, m, frame):
                good, first = _places(spec, m, frame, angle)
                if good > best[0]:
                    best = (good, first)
            if best[0] == things:
                break
        add("places", best[0] == things, best[1] or "all %d in place" % things, "all %d in place" % things)
    return out


def first_failure(checks):
    """One line for the table: the first check that failed, with the two numbers."""
    for c in checks:
        if not c["ok"]:
            return "%s: %s; wanted %s" % (c["check"], c["found"], c["wanted"])
    return ""
