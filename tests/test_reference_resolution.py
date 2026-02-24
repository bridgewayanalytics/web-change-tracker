"""Tests for bubble.reference_resolution (get_resolution_summary, format_resolution_summary)."""

import unittest

from bubble.reference_resolution import (
    clear_records,
    format_resolution_summary,
    get_records,
    get_resolution_summary,
    record_resolution,
)


class TestGetResolutionSummary(unittest.TestCase):
    def setUp(self):
        clear_records()

    def test_empty_records(self):
        self.assertEqual(get_resolution_summary(), {})

    def test_aggregates_by_field(self):
        record_resolution("organization", "Organization", ["id1"], [], "resolved")
        record_resolution("organization", "Organization", [], [], "no_match")
        record_resolution("type1", "Type1", ["id2"], ["id2"], "ai_override")
        by_field = get_resolution_summary()
        self.assertEqual(by_field["Organization"], {"resolved": 1, "unresolved": 1})
        self.assertEqual(by_field["Type1"], {"resolved": 1, "unresolved": 0})

    def test_resolved_statuses(self):
        record_resolution("x", "F", [], [], "resolved")
        record_resolution("x", "F", [], [], "RESOLVED")
        record_resolution("x", "F", [], [], "ai_override")
        record_resolution("x", "F", [], [], "no_match")
        by_field = get_resolution_summary()
        self.assertEqual(by_field["F"], {"resolved": 3, "unresolved": 1})


class TestFormatResolutionSummary(unittest.TestCase):
    def test_empty(self):
        out = format_resolution_summary({})
        self.assertIn("Reference resolution", out)
        self.assertEqual(out.strip(), "Reference resolution (resolved / unresolved):")

    def test_formats_lines(self):
        by_field = {"Organization": {"resolved": 2, "unresolved": 1}, "Type1": {"resolved": 0, "unresolved": 3}}
        out = format_resolution_summary(by_field)
        self.assertIn("Organization", out)
        self.assertIn("2 resolved, 1 unresolved", out)
        self.assertIn("Type1", out)
        self.assertIn("0 resolved, 3 unresolved", out)
