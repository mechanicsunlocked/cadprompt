# SPDX-License-Identifier: MIT
"""Welds for the AI: the tools find_joints and make_weld.

The welds themselves are the Weldment workbench (weldment/, a pinned copy of
freecad-weldment). This module only turns what a model can say ("weld the
upright to the plate, a4") into calls of it:

- find_joints() lists the corners where two parts meet, with the two face
  names, so a model never has to guess "Face6".
- make_weld runs as run_python with a short, fixed code (weld_code), so it
  gets the same undo step, the same take-back on an error and the same report.
  The user approves the one line of describe(), as for a drawing.

The text functions at the top need no FreeCAD; the rest runs inside it.
"""

# The tool's arguments and the FilletWeld properties they set.
OPTIONS = {
    "length": ("Length", float),        # one bead of this length; 0 = the whole seam
    "offset": ("Offset", float),
    "count": ("Count", int),            # intermittent n x l (e)
    "segment": ("Segment", float),
    "gap": ("Gap", float),
    "both_sides": ("BothSides", bool),
    "staggered": ("Staggered", bool),
    "all_around": ("AllAround", bool),
    "field_weld": ("FieldWeld", bool),
    "tail_text": ("TailText", str),
}
DEFAULT_THROAT = 4.0
MAX_JOINTS = 40


def _number(value, default=0.0):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _truth(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def options(args):
    """(throat, {property: value}) from the tool's arguments; unknown and empty ones are left out."""
    args = args if isinstance(args, dict) else {}
    throat = _number(args.get("throat"), DEFAULT_THROAT) or DEFAULT_THROAT
    props = {}
    for key, (prop, kind) in OPTIONS.items():
        value = args.get(key)
        if value is None or value == "":
            continue
        if kind is bool:
            value = _truth(value)
        elif kind is int:
            value = int(_number(value))
        elif kind is float:
            value = _number(value)
        else:
            value = str(value)[:40]
        if value or kind is bool:
            props[prop] = value
    return throat, props


def _num(v):
    return ("%.1f" % v).rstrip("0").rstrip(".").replace(".", ",")


def describe(args):
    """The one line that the user approves."""
    args = args if isinstance(args, dict) else {}
    throat, props = options(args)
    what = "a%s" % _num(throat)
    if props.get("Segment"):
        n = props.get("Count") or "n"
        what += " %s × %s (%s)" % (n, _num(props["Segment"]), _num(props.get("Gap", 0.0)))
    elif props.get("Length"):
        what += ", %s mm long" % _num(props["Length"])
    else:
        what += ", whole seam"
    extra = [w for w, on in (("both sides", props.get("BothSides")), ("staggered", props.get("Staggered")),
                             ("all around", props.get("AllAround")), ("site weld", props.get("FieldWeld"))) if on]
    if props.get("TailText"):
        extra.append("process " + props["TailText"])
    return "Fillet weld %s between %s and %s%s" % (
        what, str(args.get("face_a", "")) or "?", str(args.get("face_b", "")) or "?",
        (": " + ", ".join(extra)) if extra else "")


def weld_code(args):
    """The Python that make_weld runs. Every value goes in as a literal: nothing of
    the model's text becomes code."""
    args = args if isinstance(args, dict) else {}
    throat, props = options(args)
    return ("from cadprompt import welding\n"
            "welding.weld(doc, %r, %r, %r, **%r)\n"
            % (str(args.get("face_a", "")), str(args.get("face_b", "")), throat, props))


# ---- inside FreeCAD ------------------------------------------------------------

def _find(doc, name):
    obj = doc.getObject(name)
    if obj is None:
        found = doc.getObjectsByLabel(name) or [o for o in doc.Objects if o.Label.lower() == name.lower()]
        obj = found[0] if found else None
    return obj


def resolve(doc, text):
    """(object, "FaceN") from "Object.FaceN" (name or label). Raises ValueError with a hint."""
    obj_name, _, face = str(text).strip().rpartition(".")
    if not obj_name or not face.startswith("Face") or not face[4:].isdigit():
        raise ValueError("'%s' is not a face: write Object.FaceN, for example Plate.Face6. "
                         "find_joints() lists the faces that meet." % text)
    obj = _find(doc, obj_name)
    if obj is None:
        raise ValueError("there is no object '%s' in this document" % obj_name)
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull() or int(face[4:]) > len(shape.Faces) or int(face[4:]) < 1:
        raise ValueError("%s has no %s (it has %d faces)" % (obj.Label, face, len(shape.Faces) if shape else 0))
    return obj, face


def weld(doc, face_a, face_b, throat=DEFAULT_THROAT, **props):
    """Make one fillet weld, recompute, print its numbers. Raises on a weld that cannot be built,
    so run_python takes the step back."""
    from weldment.feature import make_fillet_weld
    if doc is None:
        raise ValueError("there is no open document to weld in")
    a, b = resolve(doc, face_a), resolve(doc, face_b)
    if a[0] == b[0]:
        raise ValueError("both faces are on %s: a weld joins two parts" % a[0].Label)
    obj = make_fillet_weld(doc, a, b, throat=float(throat), **props)
    doc.recompute()
    if "Invalid" in obj.State or obj.Shape.isNull():
        raise ValueError("the weld could not be built between %s and %s" % (face_a, face_b))
    print("WELD %s (%s): a%s, leg z %.1f mm, opening angle %.0f°, seam %.1f mm, welded %.1f mm, "
          "weld metal %.0f g%s" % (
              obj.Name, obj.Label, _num(obj.Throat.Value), obj.Leg.Value, obj.Dihedral.Value,
              obj.SeamLength.Value, obj.WeldLength.Value, obj.DepositMass * 1000.0,
              ", far side %s" % obj.OtherFace if obj.BothSides and obj.OtherFace else ""))
    return obj


def _part_of(obj):
    """The object a weld's face link means as a part: a feature inside a Body is that Body."""
    for parent in getattr(obj, "InList", []):
        if parent.TypeId == "PartDesign::Body" and obj in getattr(parent, "Group", []):
            return parent
    return obj


def assembly(doc, obj):
    """(parts, welds) of the welded group that `obj` belongs to; ([obj], []) when no weld touches it.

    `obj` can be a part or a weld. The group is every part that welds join to
    `obj`, directly or through other parts, and those welds, in document order.
    """
    try:
        from weldment.feature import all_welds
    except Exception:
        return [obj], []
    links = []
    for w in all_welds(doc):
        try:
            ends = {_part_of(w.FaceA[0]), _part_of(w.FaceB[0])}
        except Exception:
            continue
        links.append((w, ends))
    start = obj
    for w, ends in links:
        if w == obj:
            start = next(iter(ends))
    parts, welds, grew = {start}, [], True
    while grew:
        grew = False
        for w, ends in links:
            if w not in welds and ends & parts:
                welds.append(w)
                parts |= ends
                grew = True
    if not welds:
        return [obj], []
    order = {o.Name: i for i, o in enumerate(doc.Objects)}
    return (sorted(parts, key=lambda o: order.get(o.Name, 0)),
            sorted(welds, key=lambda o: order.get(o.Name, 0)))


def _members(doc):
    from weldment.feature import all_welds
    welds = set(o.Name for o in all_welds(doc))
    out = []
    for o in doc.Objects:
        if o.Name in welds or not o.isDerivedFrom("Part::Feature") and o.TypeId != "PartDesign::Body":
            continue
        shape = getattr(o, "Shape", None)
        try:
            if shape is None or shape.isNull() or not shape.Solids:
                continue
        except Exception:
            continue
        if any(p.TypeId == "PartDesign::Body" for p in o.InList) and o.TypeId != "PartDesign::Body":
            continue        # a feature inside a Body: the Body is the part
        if [p for p in o.InList if p.isDerivedFrom("Part::Feature") and p.Name not in welds]:
            continue        # used by a Cut, a Fusion: that one is the part
        if getattr(o, "Visibility", True) is False:
            continue
        out.append(o)
    return out


def joints(doc, limit=MAX_JOINTS):
    """The corners where two parts meet: dicts with face_a, face_b, seam, angle, curved, welded."""
    import Part
    from weldment import geometry
    from weldment.feature import _outward_normal, _tup, all_welds, seam_wire

    welded = set()
    for w in all_welds(doc):
        try:
            welded.add(frozenset(("%s.%s" % (w.FaceA[0].Name, w.FaceA[1][0]),
                                  "%s.%s" % (w.FaceB[0].Name, w.FaceB[1][0]))))
        except Exception:
            pass
    parts = _members(doc)
    found = []
    for i, a in enumerate(parts):
        box_a = a.Shape.BoundBox
        box_a.enlarge(0.1)
        for b in parts[i + 1:]:
            if not box_a.intersect(b.Shape.BoundBox):
                continue
            for ia, fa in enumerate(a.Shape.Faces):
                ba = fa.BoundBox
                ba.enlarge(0.1)
                for ib, fb in enumerate(b.Shape.Faces):
                    if not ba.intersect(fb.BoundBox):
                        continue
                    try:
                        wire = seam_wire(fa, fb)
                        e = wire.OrderedEdges[0]
                        p = e.valueAt(e.FirstParameter)
                        _ua, _ub, angle = geometry.leg_directions(
                            _tup(e.tangentAt(e.FirstParameter)), _tup(_outward_normal(fa, p)),
                            _tup(_outward_normal(fb, p)))
                    except (ValueError, Part.OCCError, ZeroDivisionError):
                        continue
                    if wire.Length < 1.0 or not 20.0 <= angle <= 160.0:
                        continue
                    names = ("%s.Face%d" % (a.Name, ia + 1), "%s.Face%d" % (b.Name, ib + 1))
                    found.append({"face_a": names[0], "face_b": names[1], "parts": (a.Label, b.Label),
                                  "seam": wire.Length, "angle": angle,
                                  "curved": not (len(wire.Edges) == 1 and isinstance(
                                      wire.Edges[0].Curve, (Part.Line, Part.LineSegment))),
                                  "welded": frozenset(names) in welded})
                    if len(found) >= limit:
                        return found
    return found


def find_joints(doc):
    """The tool's text."""
    if doc is None:
        return "There is no open document."
    try:
        import weldment  # noqa: F401
    except Exception as exc:
        return "ERROR: the weld tools are not available: %s" % exc
    found = joints(doc)
    if not found:
        return ("No joints: no two visible solids meet along an edge where a fillet weld could go. "
                "The parts must touch (one standing on the other, not apart and not inside each other).")
    lines = ["JOINTS where two parts meet (face_a, face_b for make_weld; face_a is the arrow side):"]
    for j in found:
        lines.append("- %s + %s  (%s to %s): seam %.1f mm%s, opening angle %.0f°%s" % (
            j["face_a"], j["face_b"], j["parts"][0], j["parts"][1], j["seam"],
            " curved" if j["curved"] else "", j["angle"], "  [welded already]" if j["welded"] else ""))
    if len(found) >= MAX_JOINTS:
        lines.append("(the list stops at %d joints)" % MAX_JOINTS)
    return "\n".join(lines)
