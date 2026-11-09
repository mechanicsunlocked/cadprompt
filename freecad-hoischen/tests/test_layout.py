"""Tests for the pure layout rules. Run with any Python: python -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hoischen.layout import Span, assign_rows, layout, line_position, overlaps  # noqa: E402


class Rows(unittest.TestCase):
    def test_chain_shares_first_line(self):
        # three chain dimensions side by side, touching at their ends
        spans = [Span("a", 0, 20, 1), Span("b", 20, 50, 1), Span("c", 50, 100, 1)]
        self.assertEqual(assign_rows(spans), {"a": 0, "b": 0, "c": 0})

    def test_overall_goes_outside(self):
        spans = [Span("a", 0, 20, 1), Span("b", 20, 50, 1), Span("total", 0, 100, 1)]
        rows = assign_rows(spans)
        self.assertEqual(rows["a"], 0)
        self.assertEqual(rows["b"], 0)
        self.assertEqual(rows["total"], 1)

    def test_baseline_dimensions_stack_small_inside(self):
        # parallel (baseline) dimensioning from the left edge
        spans = [Span("l100", 0, 100, 1), Span("l30", 0, 30, 1), Span("l65", 0, 65, 1)]
        self.assertEqual(assign_rows(spans), {"l30": 0, "l65": 1, "l100": 2})

    def test_input_order_does_not_matter(self):
        spans = [Span("l100", 0, 100, 1), Span("l30", 0, 30, 1), Span("l65", 0, 65, 1)]
        self.assertEqual(assign_rows(spans), assign_rows(list(reversed(spans))))

    def test_reversed_points(self):
        self.assertEqual(assign_rows([Span("a", 30, 0, 1), Span("b", 100, 0, 1)]), {"a": 0, "b": 1})

    def test_partial_overlap_pushes_out(self):
        rows = assign_rows([Span("a", 0, 40, 1), Span("b", 30, 80, 1)])
        self.assertEqual(sorted(rows.values()), [0, 1])

    def test_touching_is_not_overlap(self):
        self.assertFalse(overlaps(Span("a", 0, 20, 1), Span("b", 20, 40, 1)))
        self.assertTrue(overlaps(Span("a", 0, 21, 1), Span("b", 20, 40, 1)))


class Positions(unittest.TestCase):
    def test_hoischen_numbers_above(self):
        # part outline from y=-20 to y=+20 (paper mm); first line 10 mm above, then 7 mm
        self.assertEqual(line_position(-20, 20, +1, 0), 30.0)
        self.assertEqual(line_position(-20, 20, +1, 1), 37.0)
        self.assertEqual(line_position(-20, 20, +1, 2), 44.0)

    def test_hoischen_numbers_below(self):
        self.assertEqual(line_position(-20, 20, -1, 0), -30.0)
        self.assertEqual(line_position(-20, 20, -1, 1), -37.0)

    def test_sides_are_independent(self):
        spans = [
            Span("top_small", 0, 20, 1), Span("top_total", 0, 100, 1),
            Span("bottom_total", 0, 100, -1),
        ]
        out = layout(spans, -20, 20)
        self.assertEqual(out["top_small"][0], 30.0)
        self.assertEqual(out["top_total"][0], 37.0)
        # alone on its side, so it gets the first line, not the second
        self.assertEqual(out["bottom_total"][0], -30.0)

    def test_text_centred_on_span(self):
        out = layout([Span("a", 10, 50, 1)], -5, 5)
        self.assertEqual(out["a"][1], 30.0)

    def test_custom_spacing(self):
        self.assertEqual(line_position(0, 0, 1, 2, first=12, pitch=8), 28.0)


if __name__ == "__main__":
    unittest.main()
