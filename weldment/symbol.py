"""ISO 2553 weld symbol text, pure Python.

A fillet weld reads   a4  |>  3 x 100 (50)   on the reference line: the
section dimension (throat a or leg z) left of the symbol, the length or the
intermittent pattern n x l (e) to the right.  Nothing on the right means the
whole seam.  Both sides: the same symbol mirrored on the other side of the
reference line.
"""

from dataclasses import dataclass


@dataclass
class WeldSpec:
    throat: float = 4.0        # a, mm
    leg_dimension: bool = False  # dimension as z (leg) instead of a (throat)
    leg: float = 0.0           # z, mm (used when leg_dimension)
    length: float = 0.0        # one bead of this length; 0 = whole seam
    count: int = 0             # intermittent: n
    segment: float = 0.0       # intermittent: l (0 = continuous)
    gap: float = 0.0           # intermittent: e
    both_sides: bool = False
    staggered: bool = False    # both sides, elements offset by half a pitch
    all_around: bool = False
    field: bool = False
    tail: str = ""


def number(value, decimal_comma=True):
    """4.0 -> '4', 3.5 -> '3,5' (DIN style comma) or '3.5'."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text.replace(".", ",") if decimal_comma else text


def size_text(spec, decimal_comma=True):
    if spec.leg_dimension:
        return "z" + number(spec.leg, decimal_comma)
    return "a" + number(spec.throat, decimal_comma)


def length_text(spec, decimal_comma=True, times="×"):
    if spec.segment > 0.0:
        n = number(spec.count, decimal_comma) if spec.count > 0 else "n"
        text = f"{n}{times}{number(spec.segment, decimal_comma)}"
        if spec.gap > 0.0:
            text += f" ({number(spec.gap, decimal_comma)})"
        return text
    if spec.length > 0.0:
        return number(spec.length, decimal_comma)
    return ""


def tile_texts(spec, decimal_comma=True):
    """Texts for the arrow-side tile and, if both sides, the other-side tile."""
    arrow = {"left": size_text(spec, decimal_comma), "right": length_text(spec, decimal_comma)}
    other = dict(arrow) if spec.both_sides else None
    return arrow, other


def description(spec, decimal_comma=True):
    """One line for lists and labels: 'a4 fillet, 3x100 (50), both sides'."""
    parts = [size_text(spec, decimal_comma) + " fillet"]
    right = length_text(spec, decimal_comma)
    if right:
        parts.append(right)
    if spec.both_sides:
        parts.append("both sides, staggered" if spec.staggered and spec.segment > 0 else "both sides")
    if spec.all_around:
        parts.append("all around")
    if spec.field:
        parts.append("site weld")
    return ", ".join(parts)
