"""
Unit tests for bubble payload module. Ensures schema keys are preserved.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bubble.payload import build_resource_payload, build_calendar_item_payload, validate_payload, strip_debug_keys
from bubble.schemas import CALENDAR_ITEM_SCHEMA_FIELDS, FULL_RESOURCE_SCHEMA_FIELDS
from spike import BUBBLE_RESOURCES_FILE, BUBBLE_CALENDAR_ITEMS_FILE, _write_bubble_payload


def _mock_change_events_with_additions() -> list[dict]:
    """Change events with added docs, event_links, and meetings."""
    return [
        {
            "target_id": "naic.e.life_rbc_wg",
            "label": "Life Risk-Based Capital Working Group",
            "url": "https://content.naic.org/committees/e/life-risk-based-capital-wg",
            "org_path": ["NAIC", "E", "Working Groups"],
            "change": {
                "first_run": False,
                "page_changed": False,
                "by_type": {
                    "docs": {
                        "added": [{"title": "FAKE TEST DOC", "url": "https://example.com/fake.pdf"}],
                        "removed": [],
                    },
                    "event_links": {
                        "added": [{"title": "FAKE TEST EVENT", "url": "https://example.com/fake-event"}],
                        "removed": [],
                    },
                    "events": {"added": [], "removed": []},
                    "meetings": {
                        "added": [{
                            "title": "FAKE TEST MEETING",
                            "date_text": "Tuesday, January 15, 2025",
                            "time_text": "1:00 PM ET",
                            "expected_duration": "1 hr",
                            "notes": None,
                        }],
                        "removed": [],
                    },
                },
            },
        },
    ]


class TestBubblePayload(unittest.TestCase):
    def test_resource_payload_has_exact_schema_keys(self) -> None:
        """Resource payload objects have exactly the schema keys, no extras."""
        events = _mock_change_events_with_additions()
        payload = build_resource_payload(events)
        self.assertGreaterEqual(len(payload), 1, "Should have at least one Resource item")
        obj = payload[0]
        self.assertEqual(set(obj.keys()), set(FULL_RESOURCE_SCHEMA_FIELDS))
        for k in FULL_RESOURCE_SCHEMA_FIELDS:
            self.assertIn(k, obj)

    def test_calendar_item_payload_has_exact_schema_keys(self) -> None:
        """Calendar Item payload objects have exactly the schema keys."""
        events = _mock_change_events_with_additions()
        payload = build_calendar_item_payload(events)
        self.assertGreaterEqual(len(payload), 1, "Should have at least one Calendar Item")
        obj = payload[0]
        self.assertEqual(set(obj.keys()), set(CALENDAR_ITEM_SCHEMA_FIELDS))

    def test_validate_payload_strips_extras_and_fills_missing(self) -> None:
        """validate_payload removes extra keys and sets missing to null."""
        obj = {"title": "x", "extra_key": "bad", "date": "2025-01-15"}
        result = validate_payload(CALENDAR_ITEM_SCHEMA_FIELDS, obj)
        self.assertNotIn("extra_key", result)
        self.assertEqual(result["title"], "x")
        self.assertEqual(result["date"], "2025-01-15")
        for k in CALENDAR_ITEM_SCHEMA_FIELDS:
            self.assertIn(k, result)

    def test_write_payload_files_exist_and_valid(self) -> None:
        """_write_bubble_payload creates both JSON files with valid schema."""
        events = _mock_change_events_with_additions()
        _write_bubble_payload(events)
        self.assertTrue(BUBBLE_RESOURCES_FILE.exists())
        self.assertTrue(BUBBLE_CALENDAR_ITEMS_FILE.exists())
        resources = json.loads(BUBBLE_RESOURCES_FILE.read_text(encoding="utf-8"))
        calendar = json.loads(BUBBLE_CALENDAR_ITEMS_FILE.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(resources), 1)
        self.assertGreaterEqual(len(calendar), 1)
        self.assertEqual(set(resources[0].keys()), set(FULL_RESOURCE_SCHEMA_FIELDS))
        self.assertEqual(set(calendar[0].keys()), set(CALENDAR_ITEM_SCHEMA_FIELDS))

    def test_empty_changes_writes_empty_arrays(self) -> None:
        """No additions produces empty arrays in both files."""
        self.assertEqual(build_resource_payload([]), [])
        self.assertEqual(build_calendar_item_payload([]), [])
        _write_bubble_payload([])
        self.assertEqual(json.loads(BUBBLE_RESOURCES_FILE.read_text(encoding="utf-8")), [])
        self.assertEqual(json.loads(BUBBLE_CALENDAR_ITEMS_FILE.read_text(encoding="utf-8")), [])

    def test_strip_debug_keys_removes_leading_double_underscore(self) -> None:
        """strip_debug_keys removes __meeting_meta, __key, __source; keeps other keys."""
        obj = {"Name": "Doc", "URL": "https://example.com/x.pdf", "__meeting_meta": {"date_iso": "2026-03-02"}, "__key": "abc"}
        out = strip_debug_keys(obj)
        self.assertEqual(out, {"Name": "Doc", "URL": "https://example.com/x.pdf"})
        self.assertIn("__meeting_meta", obj, "Original unchanged")


if __name__ == "__main__":
    unittest.main()
