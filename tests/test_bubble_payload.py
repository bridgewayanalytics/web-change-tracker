"""
Unit tests for bubble_payload module. Ensures schema keys are preserved.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bubble_payload import build_bubble_payload
from bubble_resources import BUBBLE_RESOURCE_FIELDS
from spike import BUBBLE_PAYLOAD_FILE, _write_bubble_payload


def _mock_change_events_with_additions() -> list[dict]:
    """Change events with added docs and event_links (simulated run)."""
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
                    "meetings": {"added": [], "removed": []},
                },
            },
        },
    ]


class TestBubblePayload(unittest.TestCase):
    def test_build_payload_has_exact_schema_keys(self) -> None:
        """Payload objects have exactly the Bubble Resource schema keys, no extras."""
        events = _mock_change_events_with_additions()
        payload = build_bubble_payload(events)
        self.assertGreaterEqual(len(payload), 1, "Should have at least one item")
        obj = payload[0]
        self.assertEqual(set(obj.keys()), set(BUBBLE_RESOURCE_FIELDS), "Keys must match schema exactly")
        for k in BUBBLE_RESOURCE_FIELDS:
            self.assertIn(k, obj)

    def test_write_payload_file_exists_and_valid(self) -> None:
        """_write_bubble_payload creates last_bubble_payload.json with valid schema."""
        events = _mock_change_events_with_additions()
        _write_bubble_payload(events)
        self.assertTrue(BUBBLE_PAYLOAD_FILE.exists(), "last_bubble_payload.json should exist")
        data = json.loads(BUBBLE_PAYLOAD_FILE.read_text(encoding="utf-8"))
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)
        obj = data[0]
        self.assertEqual(set(obj.keys()), set(BUBBLE_RESOURCE_FIELDS))

    def test_empty_changes_writes_empty_array(self) -> None:
        """No additions produces empty array."""
        payload = build_bubble_payload([])
        self.assertEqual(payload, [])
        _write_bubble_payload([])
        data = json.loads(BUBBLE_PAYLOAD_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data, [])


if __name__ == "__main__":
    unittest.main()
