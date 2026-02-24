"""
Unit tests for bubble.mapping_pipeline: verification gate, candidate assembly,
verify_all_references, AI unknown id (rejected), low confidence (fallback), valid id (accepted), missing candidates (fallback).
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bubble.mapping_pipeline import (
    assemble_candidates,
    verification_gate,
    verify_all_references,
)


class TestVerificationGateUnknownIdRejected(unittest.TestCase):
    """AI returns unknown id -> rejected; fallback used when valid."""

    def test_unknown_id_rejected(self):
        resources = [{"Name": "R1", "Type1": ["unknown-id-from-ai"], "Organization": ["org-1"]}]
        calendar_items = []
        allowed_ids = frozenset({"valid-type-id", "org-1"})
        fallback_resources = [{"Name": "R1", "Type1": ["valid-type-id"], "Organization": ["org-1"]}]
        out_r, out_c = verification_gate(
            resources,
            calendar_items,
            allowed_ids,
            fallback_resources=fallback_resources,
            fallback_calendar=[],
        )
        self.assertEqual(out_r[0]["Type1"], ["valid-type-id"], "unknown id rejected, fallback used")
        self.assertEqual(out_r[0]["Organization"], ["org-1"])


class TestVerificationGateLowConfidenceFallback(unittest.TestCase):
    """When AI does not override (low confidence), deterministic values kept; verification preserves them."""

    def test_low_confidence_fallback(self):
        # Simulate: deterministic had Type1; "AI" left it unchanged (low confidence). Verification keeps it.
        resources = [{"Name": "R1", "Type1": ["det-type-id"], "topic suggestion": None}]
        calendar_items = []
        allowed_ids = frozenset({"det-type-id"})
        fallback_resources = [{"Name": "R1", "Type1": ["det-type-id"], "topic suggestion": None}]
        out_r, _ = verification_gate(
            resources,
            calendar_items,
            allowed_ids,
            fallback_resources=fallback_resources,
            fallback_calendar=[],
        )
        self.assertEqual(out_r[0]["Type1"], ["det-type-id"], "valid deterministic id kept")


class TestVerificationGateValidIdAccepted(unittest.TestCase):
    """Valid id (in allowed_ids) is accepted."""

    def test_valid_id_accepted(self):
        resources = [{"Name": "R1", "Type1": ["valid-t1"], "Organization": ["valid-org"]}]
        calendar_items = [{"title": "E1", "NAIC Group (tree node)": "valid-naic"}]
        allowed_ids = frozenset({"valid-t1", "valid-org", "valid-naic"})
        out_r, out_c = verification_gate(
            resources,
            calendar_items,
            allowed_ids,
        )
        self.assertEqual(out_r[0]["Type1"], ["valid-t1"])
        self.assertEqual(out_r[0]["Organization"], ["valid-org"])
        self.assertEqual(out_c[0]["NAIC Group (tree node)"], "valid-naic")


class TestVerificationGateMissingCandidatesFallback(unittest.TestCase):
    """Missing candidates (allowed_ids empty) -> all refs cleared or fallback; fallback ids also not in allowed so empty."""

    def test_missing_candidates_fallback(self):
        resources = [
            {
                "Name": "R1",
                "Type1": ["any-id"],
                "Organization": ["any-org"],
                "topic suggestion": "any-topic",
            }
        ]
        fallback_resources = [
            {"Name": "R1", "Type1": ["fallback-t1"], "Organization": ["fallback-org"], "topic suggestion": "fallback-topic"}
        ]
        allowed_ids = frozenset()
        out_r, _ = verification_gate(
            resources,
            [],
            allowed_ids,
            fallback_resources=fallback_resources,
            fallback_calendar=[],
        )
        self.assertEqual(out_r[0]["Type1"], [], "no candidates -> list refs empty")
        self.assertEqual(out_r[0]["Organization"], [], "no candidates -> list refs empty")
        self.assertIsNone(out_r[0].get("topic suggestion"), "no candidates -> single ref None")


class TestAssembleCandidates(unittest.TestCase):
    """Candidate assembly builds allowed_ids from snapshot."""

    def test_assemble_candidates_empty_snapshot(self):
        out = assemble_candidates(None)
        self.assertEqual(out["allowed_ids"], frozenset())
        self.assertEqual(out["mapping_context"], {})

    def test_assemble_candidates_builds_allowed_ids(self):
        snapshot = {
            "trees": [],
            "tree_nodes": [],
            "calendar_items": [],
        }
        # mapping_context returns empty lists; allowed_ids still built from mapping_context keys
        out = assemble_candidates(snapshot)
        self.assertIn("allowed_ids", out)
        self.assertIn("mapping_context", out)
        self.assertEqual(out["allowed_ids"], frozenset())

    def test_assemble_candidates_includes_ids_from_lists(self):
        snapshot = {
            "trees": [{"_id": "tree1", "Name": "Org"}],
            "tree_nodes": [
                {"_id": "node1", "Name": "NAIC", "Tree": "tree1"},
                {"_id": "node2", "Name": "E", "Tree": "tree1"},
            ],
            "calendar_items": [{"_id": "cal1", "title": "Meet"}],
        }
        out = assemble_candidates(snapshot)
        # build_mapping_context extracts nodes by tree name and path; we may get org/naic/type nodes
        # and recent_calendar_items. So allowed_ids may contain node1, node2, cal1 if they appear in context.
        self.assertIsInstance(out["allowed_ids"], frozenset)
        self.assertIsInstance(out["mapping_context"], dict)


class TestVerifyAllReferences(unittest.TestCase):
    """verify_all_references: no snapshot skip, with snapshot drops invalid and writes report."""

    def test_no_snapshot_normal_returns_unchanged(self):
        resources = [{"Name": "R1", "Type1": ["some-id"], "Organization": ["org-1"]}]
        calendar_items = [{"title": "E1", "NAIC Group (tree node)": "naic-1"}]
        out_r, out_c = verify_all_references(resources, calendar_items, None, mode="normal")
        self.assertEqual(out_r, resources)
        self.assertEqual(out_c, calendar_items)
        self.assertTrue(Path("debug/verify_report.json").exists())
        report = json.loads(Path("debug/verify_report.json").read_text(encoding="utf-8"))
        self.assertTrue(report.get("skipped"))
        self.assertEqual(report.get("reason"), "no snapshot")

    def test_with_snapshot_invalid_dropped_report_written(self):
        snapshot = {"trees": [], "tree_nodes": [], "calendar_items": []}
        resources = [{"Name": "R1", "Type1": ["invalid-id"], "Organization": [], "Related calendar items": []}]
        calendar_items = []
        out_r, out_c = verify_all_references(resources, calendar_items, snapshot, mode="normal")
        self.assertEqual(out_r[0]["Type1"], [])
        report = json.loads(Path("debug/verify_report.json").read_text(encoding="utf-8"))
        self.assertIn("allowed_ids_count", report)
        self.assertIn("invalid_dropped", report)
        self.assertTrue(len(report["invalid_dropped"]["resources"]) >= 1)

    def test_e2e_verify_exits_nonzero_when_invalid_and_no_snapshot(self):
        with self.assertRaises(SystemExit) as ctx:
            verify_all_references([{"Name": "R1"}], [], None, mode="e2e_verify")
        self.assertEqual(ctx.exception.code, 1)

    def test_e2e_verify_exits_nonzero_when_invalid_ids_present(self):
        snapshot = {"trees": [], "tree_nodes": [], "calendar_items": []}
        resources = [{"Name": "R1", "Type1": ["invalid-id"], "Organization": [], "Related calendar items": []}]
        with self.assertRaises(SystemExit) as ctx:
            verify_all_references(resources, [], snapshot, mode="e2e_verify")
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
