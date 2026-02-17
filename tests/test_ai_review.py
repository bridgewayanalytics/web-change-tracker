"""
Unit tests for ai_review module. Mocks OpenAI to assert key preservation.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ai_review import normalize_bubble_resources


def _sample_resources() -> list[dict]:
    return [
        {
            "Name": "Doc 1",
            "notes": "Some notes",
            "date": "Mar 3, 2024 5:00 pm",
            "Date display": "Full date",
            "URL": "https://example.com/1",
            "Organization": "org1",
            "parent": "p1",
        },
        {
            "Name": "Doc 2",
            "notes": "",
            "date": "",
            "Date display": "",
            "URL": "https://example.com/2",
            "Organization": "org2",
        },
    ]


def _mock_completion(content: str) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class TestAIReview(unittest.TestCase):
    def test_ai_normalize_preserves_keys(self) -> None:
        """After AI step, output must have same keys per object as input."""
        resources = _sample_resources()
        ai_output = [{**r, "Name": "Improved " + r["Name"], "notes": r["notes"] or "(none)"} for r in resources]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _mock_completion(json.dumps(ai_output))
        with patch.dict(os.environ, {"AI_SCHEMA_REVIEW": "1", "OPENAI_API_KEY": "sk-test"}):
            with patch("openai.OpenAI", return_value=mock_client):
                result = normalize_bubble_resources(resources)
        self.assertEqual(len(result), len(resources))
        for orig, out in zip(resources, result):
            self.assertEqual(set(out.keys()), set(orig.keys()), "Keys must not change")
            for k in orig:
                self.assertIn(k, out)

    def test_ai_normalize_strips_extra_keys_when_ai_adds_them(self) -> None:
        """If AI returns extra keys, we strip them and preserve only original keys."""
        resources = _sample_resources()
        ai_output = [{**r, "Name": "Improved", "extra_key": "should be stripped"} for r in resources]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _mock_completion(json.dumps(ai_output))
        with patch.dict(os.environ, {"AI_SCHEMA_REVIEW": "1", "OPENAI_API_KEY": "sk-test"}):
            with patch("openai.OpenAI", return_value=mock_client):
                result = normalize_bubble_resources(resources)
        for orig, out in zip(resources, result):
            self.assertEqual(set(out.keys()), set(orig.keys()))
            self.assertNotIn("extra_key", out)

    def test_ai_skipped_when_disabled(self) -> None:
        """When AI_SCHEMA_REVIEW != 1, input is returned unchanged."""
        resources = _sample_resources()
        with patch.dict(os.environ, {"AI_SCHEMA_REVIEW": "0"}, clear=False):
            result = normalize_bubble_resources(resources)
        self.assertIs(result, resources)

    def test_ai_skipped_when_no_key(self) -> None:
        """When OPENAI_API_KEY not set, input is returned unchanged."""
        resources = _sample_resources()
        with patch.dict(os.environ, {"AI_SCHEMA_REVIEW": "1", "OPENAI_API_KEY": ""}, clear=False):
            result = normalize_bubble_resources(resources)
        self.assertEqual(result, resources)


if __name__ == "__main__":
    unittest.main()
