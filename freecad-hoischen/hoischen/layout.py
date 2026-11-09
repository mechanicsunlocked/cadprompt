"""Where linear dimensions go, by the book (Hoischen / DIN 406-11).

Pure Python, no FreeCAD import, so it can be tested anywhere.

The rule, as Hoischen teaches it:
  * the first dimension line sits about 10 mm from the body edges,
  * every further parallel dimension line about 7 mm beyond the previous one,
  * small dimensions inside, large ones outside, so extension lines never cross
    dimension lines,
  * dimensions that do not overlap share a line (chain dimensioning).

All distances are millimetres on the paper, so they do not change with the
view scale. That is the point of the rule: a 1:5 drawing and a 2:1 drawing look
the same to the person reading them.
"""

from dataclasses import dataclass

FIRST_OFFSET = 10.0  # body edge -> first dimension line, mm on paper
PITCH = 7.0          # dimension line -> next parallel dimension line, mm on paper
TOUCH_TOL = 0.01     # spans that only touch at an end may share a line


@dataclass(frozen=True)
class Span:
    """One linear dimension, seen along the axis it measures.

    key   any hashable id (the FreeCAD object name in practice)
    lo/hi start and end along the measured axis, paper mm (order does not matter)
    side  +1 = above / right of the part, -1 = below / left
    """

    key: object
    lo: float
    hi: float
    side: int

    @property
    def start(self):
        return min(self.lo, self.hi)

    @property
    def end(self):
        return max(self.lo, self.hi)

    @property
    def length(self):
        return self.end - self.start

    @property
    def middle(self):
        return (self.start + self.end) / 2.0


def overlaps(a, b, tol=TOUCH_TOL):
    """True when two spans overlap by more than a touch at their ends."""
    return a.start < b.end - tol and b.start < a.end - tol


def assign_rows(spans, tol=TOUCH_TOL):
    """Row number per span key: 0 is the line nearest the part.

    Shortest first. Each span goes one row further out than the outermost
    already-placed span it overlaps, so a dimension always sits outside every
    dimension it contains, and dimensions that sit side by side share row 0
    (or whatever row they both reach). Ties are broken by position, so the
    result is the same every time for the same drawing.
    """
    rows = {}
    placed = []
    for span in sorted(spans, key=lambda s: (s.length, s.start, str(s.key))):
        row = 0
        for other in placed:
            if overlaps(span, other, tol):
                row = max(row, rows[other.key] + 1)
        rows[span.key] = row
        placed.append(span)
    return rows


def line_position(edge_low, edge_high, side, row, first=FIRST_OFFSET, pitch=PITCH):
    """Coordinate of a dimension line across the measured axis.

    edge_low / edge_high are the part's outline extent across that axis
    (bottom/top for horizontal dimensions, left/right for vertical ones).
    """
    distance = first + pitch * row
    if side >= 0:
        return edge_high + distance
    return edge_low - distance


def layout(spans, edge_low, edge_high, first=FIRST_OFFSET, pitch=PITCH, tol=TOUCH_TOL):
    """Place every span. Returns {key: (line_coordinate, along_axis_middle, row)}.

    Each side of the part is laid out on its own, so dimensions above the part
    never push the ones below it outwards.
    """
    result = {}
    for side in (+1, -1):
        group = [s for s in spans if (s.side >= 0) == (side > 0)]
        rows = assign_rows(group, tol)
        for s in group:
            result[s.key] = (
                line_position(edge_low, edge_high, side, rows[s.key], first, pitch),
                s.middle,
                rows[s.key],
            )
    return result
