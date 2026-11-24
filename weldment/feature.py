"""The FilletWeld document object: a parametric 3D bead between two faces.

Pick face A and face B of two parts that touch along an edge.  The seam is
where the faces meet; the bead is a triangle (legs z on each face, flat
outer surface) extruded along a straight seam or swept along a curved one
(tube on plate), continuous, of a given length, or intermittent n x l (e).
Both sides adds the mirror bead on the far face of part B, found by itself
unless FaceB2 says otherwise.  Everything recomputes when the parts change.
"""

import FreeCAD as App
import Part

from . import geometry

WELD_COLOR = (0.85, 0.55, 0.15)      # the colour of a bead: Inventor-ish ochre
GROUP_NAME = "Welds"
TOUCH_TOL = 0.01                     # mm: faces further apart than this do not meet


def _vec(t):
    return App.Vector(t[0], t[1], t[2])


def _tup(v):
    return (v.x, v.y, v.z)


class FilletWeld:
    """Proxy for a Part::FeaturePython weld bead."""

    def __init__(self, obj):
        obj.Proxy = self
        self.Type = "Weldment::FilletWeld"
        add = obj.addProperty
        add("App::PropertyLinkSub", "FaceA", "Weld", "First welded face (the arrow side of the symbol)")
        add("App::PropertyLinkSub", "FaceB", "Weld", "Second welded face")
        add("App::PropertyLength", "Throat", "Weld", "Throat a per ISO 2553").Throat = 4.0
        add("App::PropertyLength", "Length", "Weld", "Bead length; 0 = the whole seam").Length = 0.0
        add("App::PropertyLength", "Offset", "Weld", "Where the bead starts, measured from the seam start").Offset = 0.0
        add("App::PropertyBool", "FromEnd", "Weld", "Measure Offset and the layout from the other end of the seam").FromEnd = False
        add("App::PropertyInteger", "Count", "Intermittent", "n: number of weld elements; 0 = as many as fit").Count = 0
        add("App::PropertyLength", "Segment", "Intermittent", "l: length of one element; 0 = continuous bead").Segment = 0.0
        add("App::PropertyLength", "Gap", "Intermittent", "e: gap between elements").Gap = 0.0
        add("App::PropertyBool", "BothSides", "Weld", "Symmetrical weld: the same bead on the far side of part B").BothSides = False
        add("App::PropertyBool", "Staggered", "Intermittent", "Both sides: far-side elements shifted by half a pitch").Staggered = False
        add("App::PropertyLinkSub", "FaceB2", "Weld", "Far-side face of part B for BothSides; empty = find it")
        add("App::PropertyBool", "AllAround", "Symbol", "Weld all around (circle at the kink)").AllAround = False
        add("App::PropertyBool", "FieldWeld", "Symbol", "Site weld (flag)").FieldWeld = False
        add("App::PropertyString", "TailText", "Symbol", "Text at the tail of the symbol, e.g. the process 135").TailText = ""
        add("App::PropertyLength", "SeamLength", "Result", "Length of the seam the two faces share")
        add("App::PropertyLength", "WeldLength", "Result", "Length of bead actually laid")
        add("App::PropertyLength", "Leg", "Result", "Leg z that gives the throat in this joint")
        add("App::PropertyAngle", "Dihedral", "Result", "Opening angle between the two faces at the seam")
        add("App::PropertyVolume", "DepositVolume", "Result", "Weld metal, triangular section")
        add("App::PropertyFloat", "DepositMass", "Result", "Weld metal in kg (steel)")
        add("App::PropertyVector", "SeamStart", "Result", "Start of the seam (3D)")
        add("App::PropertyVector", "SeamEnd", "Result", "End of the seam (3D)")
        add("App::PropertyFloatList", "Intervals", "Result", "Welded intervals along the seam, start/end pairs in mm")
        add("App::PropertyString", "OtherFace", "Result", "Far-side face used for BothSides")
        add("App::PropertyBool", "Curved", "Result", "Seam is not a straight line")
        for name in ("SeamLength", "WeldLength", "Leg", "Dihedral", "DepositVolume", "DepositMass",
                     "SeamStart", "SeamEnd", "Intervals", "OtherFace", "Curved"):
            obj.setEditorMode(name, 1)
        obj.setEditorMode("Placement", 2)   # the bead lives where the seam is

    # --- FreeCAD plumbing -------------------------------------------------

    def onDocumentRestored(self, obj):
        obj.Proxy = self

    def dumps(self):
        return None

    def loads(self, state):
        return None

    # --- the work ---------------------------------------------------------

    def execute(self, obj):
        face_a = _linked_face(obj.FaceA, "FaceA")
        face_b = _linked_face(obj.FaceB, "FaceB")
        first = bead(face_a, face_b, obj.Throat.Value, obj.Length.Value, obj.Offset.Value,
                     obj.Count, obj.Segment.Value, obj.Gap.Value, obj.FromEnd)
        solids = list(first["solids"])
        welded = first["welded"]
        volume = first["volume"]
        other_name = ""
        if obj.BothSides:
            if obj.FaceB2 and obj.FaceB2[1]:
                face_b2 = _linked_face(obj.FaceB2, "FaceB2")
                other_name = obj.FaceB2[1][0]
            else:
                other_name, face_b2 = far_side_face(face_a, face_b, obj.FaceB[0])
            shift = geometry.stagger_offset(obj.Segment.Value, obj.Gap.Value) if obj.Staggered else 0.0
            second = bead(face_a, face_b2, obj.Throat.Value, obj.Length.Value, obj.Offset.Value + shift,
                          obj.Count, obj.Segment.Value, obj.Gap.Value, obj.FromEnd)
            solids += second["solids"]
            welded += second["welded"]
            volume += second["volume"]
        obj.Shape = solids[0] if len(solids) == 1 else Part.makeCompound(solids)

        obj.SeamLength = first["seam_length"]
        obj.WeldLength = welded
        obj.Leg = first["leg"]
        obj.Dihedral = first["dihedral"]
        obj.DepositVolume = volume
        obj.DepositMass = volume * geometry.STEEL_DENSITY_KG_PER_MM3
        obj.SeamStart = first["start"]
        obj.SeamEnd = first["end"]
        obj.Intervals = [c for pair in first["intervals"] for c in pair]
        obj.OtherFace = other_name
        obj.Curved = first["curved"]

    # --- for the drawing and the lists -------------------------------------

    @staticmethod
    def arrow_point(obj):
        """3D point the drawing arrow should touch: the middle of the first bead on the seam."""
        iv = list(obj.Intervals)
        if len(iv) < 2:
            return obj.SeamStart
        if obj.Curved and obj.Shape.Solids:
            return obj.Shape.Solids[0].CenterOfMass
        mid = (iv[0] + iv[1]) / 2.0
        d = obj.SeamEnd - obj.SeamStart
        if d.Length < geometry.TOL:
            return obj.SeamStart
        return obj.SeamStart + d.normalize() * mid


class ViewProviderFilletWeld:
    def __init__(self, vobj):
        vobj.Proxy = self
        vobj.ShapeColor = WELD_COLOR
        vobj.LineColor = (0.45, 0.28, 0.05)
        vobj.Transparency = 0

    def getIcon(self):
        import os
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons", "weldment_fillet.svg")

    def attach(self, vobj):
        self.Object = vobj.Object

    def updateData(self, obj, prop):
        return

    def onChanged(self, vobj, prop):
        return

    def dumps(self):
        return None

    def loads(self, state):
        return None


# --- geometry helpers that need FreeCAD -------------------------------------

def _linked_face(link, name):
    if not link or not link[1]:
        raise ValueError(f"{name}: pick a face")
    obj, subs = link
    sub = subs[0]
    shape = obj.getSubObject(sub)
    if shape is None or shape.ShapeType != "Face":
        raise ValueError(f"{name}: {obj.Label}.{sub} is not a face")
    return shape


def _outward_normal(face, point=None):
    """Outward normal of a face of a solid, at `point` or at the middle of its parameter range."""
    if point is not None:
        u, v = face.Surface.parameter(point)
    else:
        u0, u1, v0, v1 = face.ParameterRange
        u, v = (u0 + u1) / 2.0, (v0 + v1) / 2.0
    return face.normalAt(u, v)


def bead(face_a, face_b, throat, length=0.0, offset=0.0, count=0, segment=0.0, gap=0.0, from_end=False):
    """Solids of one fillet bead between two faces plus its numbers (dict)."""
    wire = seam_wire(face_a, face_b)
    seam_length = wire.Length
    straight = _is_straight(wire)
    e0 = wire.OrderedEdges[0]
    p0 = e0.valueAt(e0.FirstParameter)
    t0 = e0.tangentAt(e0.FirstParameter)
    if straight:
        start, end = _ordered(*_extreme_points([v.Point for v in wire.Vertexes]))
        d = (end - start).normalize()
        p0, t0 = start, d
    n_a = _outward_normal(face_a, p0)
    n_b = _outward_normal(face_b, p0)
    u_a, u_b, dihedral = geometry.leg_directions(_tup(t0), _tup(n_a), _tup(n_b))
    z = geometry.leg_from_throat(throat, dihedral)
    intervals = geometry.segments(seam_length, length, offset, count, segment, gap, from_end)
    if not intervals:
        raise ValueError("no weld left on the seam: check Offset, Length and the intermittent values")

    solids = []
    for s_, e_ in intervals:
        if straight:
            corner = _tup(start + d * s_)
            pts = [_vec(p) for p in geometry.triangle(corner, u_a, u_b, z)]
            section = Part.Face(Part.makePolygon(pts + [pts[0]]))
            solids.append(section.extrude(_vec(d) * (e_ - s_)))
        else:
            path = _sub_wire(wire, s_, e_)
            ps = path.OrderedEdges[0]
            corner = ps.valueAt(ps.FirstParameter)
            tangent = ps.tangentAt(ps.FirstParameter)
            ua, ub, _ = geometry.leg_directions(_tup(tangent), _tup(_outward_normal(face_a, corner)), _tup(_outward_normal(face_b, corner)))
            pts = [_vec(p) for p in geometry.triangle(_tup(corner), ua, ub, z)]
            profile = Part.makePolygon(pts + [pts[0]])
            solid = path.makePipeShell([profile], True, True)
            if not solid.isValid() or solid.Volume <= 0:
                raise ValueError("could not sweep the bead along the curved seam")
            solids.append(solid)
    welded = geometry.total_length(intervals)
    volume = sum(sol.Volume for sol in solids)
    if straight:
        first_pt, last_pt = start, end
    else:
        first_pt = p0
        last_e = wire.OrderedEdges[-1]
        last_pt = last_e.valueAt(last_e.LastParameter)
    return {"solids": solids, "welded": welded, "volume": volume, "seam_length": seam_length,
            "leg": z, "dihedral": dihedral, "start": first_pt, "end": last_pt,
            "intervals": intervals, "curved": not straight}


def far_side_face(face_a, face_b, obj_b):
    """('FaceN', face) of part B on the far side: touches face A, is not FaceB, forms a corner; longest seam wins."""
    shape = getattr(obj_b, "Shape", None)
    if shape is None or shape.isNull():
        raise ValueError(f"{obj_b.Label} has no shape to look for the far side in")
    best = None
    for i, f in enumerate(shape.Faces):
        if f.isSame(face_b) or f.isPartner(face_b):
            continue
        try:
            wire = seam_wire(face_a, f)
            p = wire.OrderedEdges[0].valueAt(wire.OrderedEdges[0].FirstParameter)
            t = wire.OrderedEdges[0].tangentAt(wire.OrderedEdges[0].FirstParameter)
            geometry.leg_directions(_tup(t), _tup(_outward_normal(face_a, p)), _tup(_outward_normal(f, p)))
        except (ValueError, Part.OCCError):
            continue
        if best is None or wire.Length > best[1].Length:
            best = (f"Face{i + 1}", wire, f)
    if best is None:
        raise ValueError("no far-side face of part B touches face A; set FaceB2 by hand")
    return best[0], best[2]


def seam_wire(face_a, face_b):
    """The wire where the two faces meet (Part.Wire).

    The exact section of the two faces (works when they touch along an edge
    or a curve); for two planes that only touch numerically, the line where
    the planes cross, clipped to both faces.
    """
    edges = [e for e in face_a.section(face_b).Edges if e.Length > geometry.TOL]
    planar = isinstance(face_a.Surface, Part.Plane) and isinstance(face_b.Surface, Part.Plane)
    if planar:
        edges = _longest_chain(edges)
    if not edges:
        gap = face_a.distToShape(face_b)[0]
        if gap > TOUCH_TOL:
            raise ValueError(f"the faces do not touch (gap {gap:.2f} mm)")
        if planar:
            edges = _plane_line_in_both(face_a, face_b)
    if not edges:
        raise ValueError("the faces touch but share no edge (parallel faces or a single point)")
    wires = Part.sortEdges(edges)
    wire = max((Part.Wire(w) for w in wires), key=lambda w: w.Length)
    if planar:
        # one clean segment from the extreme points, in the stable direction
        start, end = _ordered(*_extreme_points([v.Point for v in wire.Vertexes]))
        wire = Part.Wire([Part.LineSegment(start, end).toShape()])
    return wire


def seam(face_a, face_b):
    """Start and end point of a straight seam (kept for scripts)."""
    wire = seam_wire(face_a, face_b)
    return _ordered(*_extreme_points([v.Point for v in wire.Vertexes]))


def _is_straight(wire):
    return all(isinstance(e.Curve, (Part.Line, Part.LineSegment)) for e in wire.Edges) and len(wire.Edges) == 1


def _sub_wire(wire, s, e):
    """The part of `wire` between arc lengths s and e (edges in order, trimmed)."""
    out = []
    walked = 0.0
    for edge in wire.OrderedEdges:
        a, b = walked, walked + edge.Length
        walked = b
        lo, hi = max(s, a), min(e, b)
        if hi - lo <= geometry.TOL:
            continue
        p1 = edge.getParameterByLength(lo - a)
        p2 = edge.getParameterByLength(hi - a)
        if edge.Orientation == "Reversed":
            p1, p2 = edge.getParameterByLength(edge.Length - (hi - a)), edge.getParameterByLength(edge.Length - (lo - a))
        out.append(edge.Curve.toShape(p1, p2))
    if not out:
        raise ValueError("empty seam interval")
    return Part.Wire(out)


def _longest_chain(edges):
    """The straight edges of a section, dropping stray bits; the seam must be one line."""
    lines = [e for e in edges if isinstance(e.Curve, (Part.Line, Part.LineSegment)) and e.Length > geometry.TOL]
    if not lines:
        return []
    longest = max(lines, key=lambda e: e.Length)
    d = (longest.Vertexes[-1].Point - longest.Vertexes[0].Point).normalize()
    keep = []
    for e in lines:
        de = (e.Vertexes[-1].Point - e.Vertexes[0].Point).normalize()
        if abs(abs(de.dot(d)) - 1.0) < 1e-6 and (e.Vertexes[0].Point - longest.Vertexes[0].Point).cross(d).Length < TOUCH_TOL:
            keep.append(e)
    return keep


def _plane_line_in_both(face_a, face_b):
    pa, pb = face_a.Surface, face_b.Surface
    d = pa.Axis.cross(pb.Axis)
    if d.Length < 1e-9:
        return []
    d.normalize()
    # a point on both planes: solve in the plane spanned by the two normals
    n1, n2 = pa.Axis, pb.Axis
    c1, c2 = n1.dot(pa.Position), n2.dot(pb.Position)
    n1n2 = n1.dot(n2)
    det = 1.0 - n1n2 * n1n2
    k1 = (c1 - c2 * n1n2) / det
    k2 = (c2 - c1 * n1n2) / det
    p = n1 * k1 + n2 * k2
    reach = max(face_a.BoundBox.DiagonalLength, face_b.BoundBox.DiagonalLength) + abs((face_a.BoundBox.Center - p).dot(d)) + 1.0
    line = Part.LineSegment(p - d * reach, p + d * reach).toShape()
    in_a = line.common(face_a).Edges
    if not in_a:
        return []
    in_b = Part.makeCompound(in_a).common(face_b).Edges
    return [e for e in in_b if e.Length > geometry.TOL]


def _extreme_points(points):
    d = None
    for p in points[1:]:
        if (p - points[0]).Length > geometry.TOL:
            d = (p - points[0]).normalize()
            break
    if d is None:
        raise ValueError("degenerate seam")
    params = [(p - points[0]).dot(d) for p in points]
    return points[params.index(min(params))], points[params.index(max(params))]


def _ordered(a, b):
    """Stable seam direction: positive x, then y, then z, so Offset means the same after every recompute."""
    d = b - a
    for c in (d.x, d.y, d.z):
        if abs(c) > 1e-6:
            return (a, b) if c > 0 else (b, a)
    return a, b


# --- factory ----------------------------------------------------------------

def welds_group(doc):
    for o in doc.Objects:
        if o.Name == GROUP_NAME and o.isDerivedFrom("App::DocumentObjectGroup"):
            return o
    group = doc.addObject("App::DocumentObjectGroup", GROUP_NAME)
    group.Label = "Welds"
    return group


def make_fillet_weld(doc, face_a, face_b, throat=4.0, name="FilletWeld", **props):
    """face_a / face_b: (object, "FaceN").  Extra keyword args set properties (Length=..., Segment=...)."""
    obj = doc.addObject("Part::FeaturePython", name)
    FilletWeld(obj)
    if App.GuiUp:
        ViewProviderFilletWeld(obj.ViewObject)
    obj.FaceA = (face_a[0], [face_a[1]])
    obj.FaceB = (face_b[0], [face_b[1]])
    obj.Throat = throat
    for key, value in props.items():
        setattr(obj, key, value)
    welds_group(doc).addObject(obj)
    return obj


def all_welds(doc):
    return [o for o in doc.Objects if getattr(getattr(o, "Proxy", None), "Type", "") == "Weldment::FilletWeld"]
