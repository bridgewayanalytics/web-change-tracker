"""
Unit tests for bubble.enrich_refs pure functions and apply_ai_classification.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bubble.enrich_refs import (
    apply_ai_classification,
    classify_resource_type_deterministic,
    infer_naic_group_path,
)
from bubble.enrich_refs import _parse_ai_classification_response as parse_ai_response


class TestInferNaicGroupPath(unittest.TestCase):
    def test_list_passthrough(self):
        self.assertEqual(
            infer_naic_group_path(["NAIC", "E", "Working Groups"]),
            ["NAIC", "E", "Working Groups"],
        )

    def test_string_with_separator(self):
        self.assertEqual(
            infer_naic_group_path("NAIC › E › Working Groups › X"),
            ["NAIC", "E", "Working Groups", "X"],
        )

    def test_none(self):
        self.assertEqual(infer_naic_group_path(None), [])

    def test_empty_string(self):
        self.assertEqual(infer_naic_group_path(""), [])

    def test_empty_list(self):
        self.assertEqual(infer_naic_group_path([]), [])

    def test_strips_whitespace(self):
        self.assertEqual(
            infer_naic_group_path("  NAIC  ›  E  ›  WG  "),
            ["NAIC", "E", "WG"],
        )


class TestClassifyResourceTypeDeterministic(unittest.TestCase):
    def test_agenda_materials(self):
        self.assertEqual(
            classify_resource_type_deterministic(title="Meeting Agenda", url="", notes=""),
            "Agenda/Materials",
        )
        self.assertEqual(
            classify_resource_type_deterministic(url="https://example.com/call_materials/x.pdf"),
            "Agenda/Materials",
        )
        self.assertEqual(
            classify_resource_type_deterministic(notes="webex link and materials"),
            "Agenda/Materials",
        )

    def test_in_the_weeds(self):
        self.assertEqual(
            classify_resource_type_deterministic(notes="Technical deep-dive and exposure draft"),
            "In the weeds",
        )

    def test_news(self):
        self.assertEqual(
            classify_resource_type_deterministic(title="Committee News Update"),
            "News",
        )

    def test_uncertain_returns_none(self):
        self.assertIsNone(
            classify_resource_type_deterministic(title="Random Document", url="", notes=""),
        )

    def test_all_empty_returns_none(self):
        self.assertIsNone(classify_resource_type_deterministic())


class TestParseAiClassificationResponse(unittest.TestCase):
    def test_valid_json(self):
        out = parse_ai_response('{"type1_node_name": "News", "topic_node_path": ["NAIC", "E"], "confidence": 0.85}')
        self.assertIsNotNone(out)
        self.assertEqual(out["type1_node_name"], "News")
        self.assertEqual(out["topic_node_path"], ["NAIC", "E"])
        self.assertEqual(out["confidence"], 0.85)

    def test_invalid_json_returns_none(self):
        self.assertIsNone(parse_ai_response("not json"))
        self.assertIsNone(parse_ai_response(""))

    def test_markdown_code_block_stripped(self):
        out = parse_ai_response('```json\n{"type1_node_name": "Agenda/Materials", "topic_node_path": [], "confidence": 0.7}\n```')
        self.assertIsNotNone(out)
        self.assertEqual(out["type1_node_name"], "Agenda/Materials")
        self.assertEqual(out["confidence"], 0.7)

    def test_confidence_clamped(self):
        out = parse_ai_response('{"type1_node_name": null, "topic_node_path": [], "confidence": 1.5}')
        self.assertEqual(out["confidence"], 0.0)

    def test_missing_confidence_defaults_zero(self):
        out = parse_ai_response('{"type1_node_name": "News", "topic_node_path": []}')
        self.assertEqual(out["confidence"], 0.0)


class TestApplyAiClassification(unittest.TestCase):
    def test_below_threshold_returns_none(self):
        t1, topic = apply_ai_classification(
            {},
            {},
            type1_nodes_by_name={"News": "n1"},
            topic_tree_id=None,
            confidence_threshold=0.7,
            ai_response={"type1_node_name": "News", "topic_node_path": [], "confidence": 0.5},
        )
        self.assertIsNone(t1)
        self.assertIsNone(topic)

    def test_above_threshold_and_type1_exists(self):
        t1, topic = apply_ai_classification(
            {},
            {},
            type1_nodes_by_name={"Agenda/Materials": "tid-123"},
            topic_tree_id=None,
            confidence_threshold=0.7,
            ai_response={"type1_node_name": "Agenda/Materials", "topic_node_path": [], "confidence": 0.83},
        )
        self.assertEqual(t1, "tid-123")
        self.assertIsNone(topic)

    def test_type1_name_not_in_map_returns_none(self):
        t1, topic = apply_ai_classification(
            {},
            {},
            type1_nodes_by_name={"News": "n1"},
            topic_tree_id=None,
            ai_response={"type1_node_name": "Other", "topic_node_path": [], "confidence": 0.9},
        )
        self.assertIsNone(t1)
        self.assertIsNone(topic)

    def test_none_ai_response_returns_none(self):
        t1, topic = apply_ai_classification(
            {},
            {},
            type1_nodes_by_name={},
            topic_tree_id=None,
            ai_response=None,
        )
        self.assertIsNone(t1)
        self.assertIsNone(topic)


if __name__ == "__main__":
    unittest.main()
