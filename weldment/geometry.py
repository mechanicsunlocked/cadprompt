"""Fillet weld geometry, pure Python: no FreeCAD needed, unit-tested on its own.

Terms as in ISO 2553 / EN ISO 2553 (and the shop): the throat `a` is the
height of the largest isosceles triangle that fits in the weld section, the
leg `z` is the length of its sides along the parts.  For the usual 90 degree
T-joint z = a * sqrt(2).  Intermittent welds are given as n x l (e): n
elements of length l with a gap e between them.

Vectors are plain (x, y, z) tuples so the module stays free of FreeCAD.
"""

import math

STEEL_DENSITY_KG_PER_MM3 = 7.85e-6   # S235 / S355, close enough for costing
TOL = 1e-7


# --- vectors --------------------------------------------------------------

def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    return math.sqrt(dot(a, a))


def unit(a):
    n = norm(a)
    if n < TOL:
        raise ValueError("zero-length vector")
    return scale(a, 1.0 / n)


# --- section ----------------------------------------------------------------

def leg_from_throat(a, dihedral_deg=90.0):
    """Leg z for throat a in a joint whose faces open at `dihedral_deg`."""
    _check_dihedral(dihedral_deg)
    return a / math.cos(math.radians(dihedral_deg) / 2.0)


def throat_from_leg(z, dihedral_deg=90.0):
    _check_dihedral(dihedral_deg)
    return z * math.cos(math.radians(dihedral_deg) / 2.0)


def section_area(z, dihedral_deg=90.0):
    """Area of the triangular weld section with two legs z (flat face, no reinforcement)."""
    _check_dihedral(dihedral_deg)
    return 0.5 * z * z * math.sin(math.radians(dihedral_deg))


def deposit(z, dihedral_deg, total_length, density=STEEL_DENSITY_KG_PER_MM3):
    """(volume mm3, mass kg) of weld metal for `total_length` mm of bead."""
    volume = section_area(z, dihedral_deg) * total_length
    return volume, volume * density


def _check_dihedral(deg):
    if not 0.0 < deg < 180.0:
        raise ValueError(f"dihedral angle must be between 0 and 180 degrees, got {deg}")


# --- where the weld sits ----------------------------------------------------

def leg_directions(seam_dir, normal_a, normal_b):
    """Unit vectors along each face, from the seam into the open corner.

    `normal_a` / `normal_b` are the outward normals of the two welded faces
    (outward = into the air, which is where the weld goes).  The leg on face
    A runs perpendicular to the seam inside face A, on the side where face
    B's normal points, and vice versa.  Returns (u_a, u_b, dihedral_deg).
    """
    d = unit(seam_dir)
    na = unit(normal_a)
    nb = unit(normal_b)
    ua = unit(cross(d, na))
    if dot(ua, nb) < 0:
        ua = scale(ua, -1.0)
    ub = unit(cross(d, nb))
    if dot(ub, na) < 0:
        ub = scale(ub, -1.0)
    if abs(dot(ua, nb)) < 1e-6 or abs(dot(ub, na)) < 1e-6:
        raise ValueError("the two faces are parallel or do not form a corner along the seam")
    cos_t = max(-1.0, min(1.0, dot(ua, ub)))
    return ua, ub, math.degrees(math.acos(cos_t))


def triangle(corner, u_a, u_b, z):
    """The three corners of the weld section: root, toe on A, toe on B."""
    return corner, add(corner, scale(u_a, z)), add(corner, scale(u_b, z))


# --- along the seam ---------------------------------------------------------

def segments(seam_length, length=0.0, offset=0.0, count=0, segment=0.0, gap=0.0, from_end=False):
    """Intervals (start, end) in mm along the seam that get weld metal.

    length <= 0 and segment <= 0: continuous over the whole seam from `offset`.
    length > 0: one bead of that length from `offset`.
    segment > 0: intermittent n x l (e); count <= 0 means as many as fit.
    from_end: measure `offset` and the layout from the far end instead.
    Everything is clipped to the seam; empty intervals are dropped.
    """
    if seam_length <= TOL:
        return []
    offset = max(0.0, offset)
    if segment > 0.0:
        pitch = segment + max(0.0, gap)
        if count <= 0:
            count = int(math.floor((seam_length - offset + max(0.0, gap) + TOL) / pitch))
        raw = [(offset + i * pitch, offset + i * pitch + segment) for i in range(count)]
    else:
        end = seam_length if length <= 0.0 else offset + length
        raw = [(offset, end)]
    out = []
    for s, e in raw:
        s, e = max(0.0, s), min(seam_length, e)
        if e - s > TOL:
            out.append((s, e))
    if from_end:
        out = [(seam_length - e, seam_length - s) for s, e in reversed(out)]
    return out


def total_length(intervals):
    return sum(e - s for s, e in intervals)


# --- both sides, staggered -------------------------------------------------

def stagger_offset(segment, gap):
    """Shift of the far-side elements for a staggered intermittent weld: half a pitch."""
    return (segment + max(0.0, gap)) / 2.0 if segment > 0.0 else 0.0


# --- members (cut list) -----------------------------------------------------

def miter_angle(axis, end_normal):
    """Angle in degrees between a member's axis and the normal of an end face: 0 = square cut, 45 = miter."""
    a = unit(axis)
    n = unit(end_normal)
    c = min(1.0, abs(dot(a, n)))
    return math.degrees(math.acos(c))


def group_rows(rows, keys):
    """Collapse identical rows (same values in `keys`) into one with a 'qty' count, first seen first."""
    out = []
    index = {}
    for row in rows:
        k = tuple(row[key] for key in keys)
        if k in index:
            out[index[k]]["qty"] += 1
            out[index[k]]["labels"].append(row["label"])
        else:
            index[k] = len(out)
            new = dict(row)
            new["qty"] = 1
            new["labels"] = [row["label"]]
            out.append(new)
    return out
