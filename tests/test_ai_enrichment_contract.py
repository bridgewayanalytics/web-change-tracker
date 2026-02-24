"""
Unit tests for AI enrichment contract.
Validates output keys are subset of schema and required keys exist.
Does NOT call OpenAI - mocks the client.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bubble.ai_enrichment import (
    REFERENCE_FIELDS,
    enrich_calendar_items,
    enrich_resources,
)
from bubble.schemas import CALENDAR_ITEM_SCHEMA_FIELDS, FULL_RESOURCE_SCHEMA_FIELDS


def _mock_resource_list(n: int) -> list[dict]:
    """Minimal valid Resource payload items."""
    return [
        {
            "archive": False,
            "Available To Vector Store": True,
            "Chunk Overlap": 200,
            "Chunk Size": 1000,
            "date": None,
            "Date display": None,
            "Name": "Test Doc",
            "notes": "New doc",
            "Organization": "NAIC",
            "parent": "NAIC › E › Working Groups › Life RBC WG",
            "Related calendar items": [],
            "URL": "https://example.com/doc.pdf",
        }
        for _ in range(n)
    ]


def _mock_calendar_item_list(n: int) -> list[dict]:
    """Minimal valid Calendar Item payload items."""
    return [
        {
            "Agenda": [],
            "attached agenda items": [],
            "color": None,
            "date": "Tuesday, January 15, 2025 1:00 PM ET",
            "End time": None,
            "event description": None,
            "full day": "no",
            "has topic": None,
            "length": "1 hr",
            "location": None,
            "NAIC Date/Meeting Type": None,
            "NAIC Group (legacy)": None,
            "NAIC Group (tree node)": "NAIC › E › Working Groups › Life RBC WG",
            "no agenda type": None,
            "Outlook Event UID": None,
            "Outlook last sync": None,
            "outlook_icaluid": None,
            "phone_number_and_ac…": None,
            "Relevant Documents": [],
            "subtopic": None,
            "Timezone Code": "America/New_York",
            "title": "Life RBC WG Meeting",
        }
        for _ in range(n)
    ]


def _mock_context(items_count: int) -> dict:
    return {
        "items": [
            {"org_id": "x", "org_path": ["NAIC", "E", "Working Groups"], "label": "Life RBC WG", "url": "https://naic.org/x"}
            for _ in range(items_count)
        ]
    }


class TestAIEnrichmentContract(unittest.TestCase):
    """Validate enrichment output schema contract. Uses mocked OpenAI."""

    @patch("bubble.ai_enrichment._call_openai_for_resources")
    def test_enrich_resources_output_keys_subset_of_schema(self, mock_call: unittest.mock.Mock) -> None:
        """Enriched Resource output has only allowed schema keys."""
        resources = _mock_resource_list(1)
        mock_call.return_value = [
            {
                "archive": False,
                "Available To Vector Store": True,
                "Chunk Overlap": 200,
                "Chunk Size": 1000,
                "date": None,
                "Date display": None,
                "Name": "Enriched Name",
                "notes": "New doc",
                "Organization": "NAIC",
                "parent": "NAIC › E › Life RBC WG",
                "Related calendar items": [],
                "URL": "https://example.com/doc.pdf",
            }
        ]
        out = enrich_resources(resources, _mock_context(1))
        self.assertEqual(len(out), 1)
        obj = out[0]
        self.assertLessEqual(
            set(obj.keys()), set(FULL_RESOURCE_SCHEMA_FIELDS), "Output keys must be subset of schema"
        )

    @patch("bubble.ai_enrichment._call_openai_for_resources")
    def test_enrich_resources_required_keys_exist(self, mock_call: unittest.mock.Mock) -> None:
        """Enriched Resource output includes all required schema keys."""
        resources = _mock_resource_list(1)
        mock_call.return_value = [dict(r) for r in resources]
        out = enrich_resources(resources, _mock_context(1))
        obj = out[0]
        for k in FULL_RESOURCE_SCHEMA_FIELDS:
            self.assertIn(k, obj, f"Required key {k!r} must exist in output")

    @patch("bubble.ai_enrichment._call_openai_for_calendar_items")
    def test_enrich_calendar_items_output_keys_subset_of_schema(self, mock_call: unittest.mock.Mock) -> None:
        """Enriched Calendar Item output has only allowed schema keys."""
        items = _mock_calendar_item_list(1)
        mock_call.return_value = [
            {
                **items[0],
                "has topic": "yes",
                "subtopic": "Risk-Based Capital",
                "NAIC Date/Meeting Type": "Working Group",
            }
        ]
        out = enrich_calendar_items(items, _mock_context(1))
        self.assertEqual(len(out), 1)
        obj = out[0]
        self.assertLessEqual(
            set(obj.keys()), set(CALENDAR_ITEM_SCHEMA_FIELDS), "Output keys must be subset of schema"
        )

    @patch("bubble.ai_enrichment._call_openai_for_calendar_items")
    def test_enrich_calendar_items_required_keys_exist(self, mock_call: unittest.mock.Mock) -> None:
        """Enriched Calendar Item output includes all required schema keys."""
        items = _mock_calendar_item_list(1)
        mock_call.return_value = [dict(i) for i in items]
        out = enrich_calendar_items(items, _mock_context(1))
        obj = out[0]
        for k in CALENDAR_ITEM_SCHEMA_FIELDS:
            self.assertIn(k, obj, f"Required key {k!r} must exist in output")

    @patch("bubble.ai_enrichment._call_openai_for_resources")
    def test_enrich_resources_on_failure_returns_input_unchanged(self, mock_call: unittest.mock.Mock) -> None:
        """On OpenAI failure, return input unchanged."""
        resources = _mock_resource_list(1)
        mock_call.side_effect = Exception("API error")
        out = enrich_resources(resources, _mock_context(1))
        self.assertEqual(out, resources)

    @patch("bubble.ai_enrichment._call_openai_for_calendar_items")
    def test_enrich_calendar_items_on_failure_returns_input_unchanged(
        self, mock_call: unittest.mock.Mock
    ) -> None:
        """On OpenAI failure, return input unchanged."""
        items = _mock_calendar_item_list(1)
        mock_call.side_effect = Exception("API error")
        out = enrich_calendar_items(items, _mock_context(1))
        self.assertEqual(out, items)

    def test_enrich_resources_empty_returns_empty(self) -> None:
        """Empty input returns empty output without calling OpenAI."""
        out = enrich_resources([], {})
        self.assertEqual(out, [])

    def test_enrich_calendar_items_empty_returns_empty(self) -> None:
        """Empty input returns empty output without calling OpenAI."""
        out = enrich_calendar_items([], {})
        self.assertEqual(out, [])

    @patch("bubble.ai_enrichment._call_openai_for_resources")
    def test_reference_fields_never_overwritten_by_ai_resources(self, mock_call: unittest.mock.Mock) -> None:
        """Reference fields on Resources are never merged from AI output; base values are preserved."""
        base_org = ["naic-node-123"]
        base_type1 = ["type1-news-id"]
        base_related = ["cal-item-456"]
        base_parent = "NAIC › E › Life RBC WG"
        resources = _mock_resource_list(1)
        resources[0]["Organization"] = base_org
        resources[0]["Type1"] = base_type1
        resources[0]["Related calendar items"] = base_related
        resources[0]["parent"] = base_parent

        mock_call.return_value = [
            dict(resources[0])
            | {
                "Name": "AI Override Name",
                "notes": "AI override notes",
                "Organization": ["ai-org-id"],
                "Type1": ["ai-type1-id"],
                "topic suggestion": "ai-topic-id",
                "Related calendar items": ["ai-cal-id"],
                "parent": "AI parent path",
            }
        ]
        out = enrich_resources(resources, _mock_context(1))
        self.assertEqual(len(out), 1)
        obj = out[0]
        self.assertEqual(obj["Organization"], base_org, "Organization must not be overwritten by AI")
        self.assertEqual(obj["Type1"], base_type1, "Type1 must not be overwritten by AI")
        self.assertEqual(obj["Related calendar items"], base_related, "Related calendar items must not be overwritten by AI")
        self.assertEqual(obj["parent"], base_parent, "parent must not be overwritten by AI")
        self.assertIsNone(obj.get("topic suggestion"), "topic suggestion from AI must be dropped, base had None")
        self.assertEqual(obj["Name"], "AI Override Name", "Non-reference fields are still merged")
        self.assertEqual(obj["notes"], "AI override notes")

    @patch("bubble.ai_enrichment._call_openai_for_calendar_items")
    def test_reference_fields_never_overwritten_by_ai_calendar(self, mock_call: unittest.mock.Mock) -> None:
        """Reference fields on Calendar Items are never merged from AI output; base values are preserved."""
        base_naic_group = "naic-group-node-789"
        base_agenda = [{"url": "https://example.com/agenda.pdf", "title": "Agenda"}]
        items = _mock_calendar_item_list(1)
        items[0]["NAIC Group (tree node)"] = base_naic_group
        items[0]["Agenda"] = base_agenda
        items[0]["Relevant Documents"] = []

        mock_call.return_value = [
            dict(items[0])
            | {
                "title": "AI Refined Title",
                "subtopic": "AI subtopic",
                "NAIC Group (tree node)": "ai-naic-group-id",
                "Agenda": [{"url": "https://ai.com/x", "title": "AI Agenda"}],
                "Relevant Documents": ["ai-doc-id"],
            }
        ]
        out = enrich_calendar_items(items, _mock_context(1))
        self.assertEqual(len(out), 1)
        obj = out[0]
        self.assertEqual(
            obj["NAIC Group (tree node)"],
            base_naic_group,
            "NAIC Group (tree node) must not be overwritten by AI",
        )
        self.assertEqual(obj["Agenda"], base_agenda, "Agenda must not be overwritten by AI")
        self.assertEqual(obj["Relevant Documents"], [], "Relevant Documents must not be overwritten by AI")
        self.assertEqual(obj["title"], "AI Refined Title", "Non-reference fields are still merged")
        self.assertEqual(obj["subtopic"], "AI subtopic")

    def test_reference_fields_constant_complete(self) -> None:
        """REFERENCE_FIELDS contains expected Bubble reference field names."""
        expected = {
            "Organization",
            "Type",
            "Type1",
            "topic suggestion",
            "NAIC Group (tree node)",
            "Related calendar items",
            "Agenda",
            "attached agenda items",
            "Relevant Documents",
            "parent",
        }
        self.assertEqual(REFERENCE_FIELDS, expected)


if __name__ == "__main__":
    unittest.main()
