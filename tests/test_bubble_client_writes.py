"""Tests for BubbleClient write methods (create, patch) and allowlist."""

import unittest
import unittest.mock

from bubble.client import BubbleAPIError, BubbleClient


def _make_client() -> BubbleClient:
    """Create a BubbleClient with dummy credentials (no real HTTP)."""
    return BubbleClient(
        base_url="https://test.bubbleapps.io",
        api_key="test-key",
    )


class TestWriteAllowlist(unittest.TestCase):

    def test_create_alert_allowed(self):
        client = _make_client()
        # Should not raise the allowlist error
        with unittest.mock.patch.object(client, "_request", return_value={"id": "abc123"}):
            result = client.create("Alert", {"Alert type": "Agenda Posted", "date": "2026-03-10"})
            self.assertEqual(result, "abc123")

    def test_create_resource_blocked(self):
        client = _make_client()
        with self.assertRaises(BubbleAPIError) as ctx:
            client.create("Resource", {"Name": "test"})
        self.assertIn("Write not allowed", str(ctx.exception))

    def test_create_calendar_item_blocked(self):
        client = _make_client()
        with self.assertRaises(BubbleAPIError):
            client.create("Calendar Item", {"title": "test"})

    def test_patch_alerts_allowed(self):
        client = _make_client()
        with unittest.mock.patch.object(client, "_request", return_value={}):
            # Should not raise
            client.patch("Calendar Item", "id_001", {"alerts": ["a1"]}, scope="patch_alerts")

    def test_patch_other_scope_blocked(self):
        client = _make_client()
        with self.assertRaises(BubbleAPIError) as ctx:
            client.patch("Calendar Item", "id_001", {"title": "new"}, scope="patch_title")
        self.assertIn("Write not allowed", str(ctx.exception))

    def test_patch_resource_blocked(self):
        client = _make_client()
        with self.assertRaises(BubbleAPIError):
            client.patch("Resource", "id_001", {"Name": "new"}, scope="patch_alerts")


class TestCreateMethod(unittest.TestCase):

    def test_returns_id(self):
        client = _make_client()
        with unittest.mock.patch.object(client, "_request", return_value={"id": "new_id_123"}):
            result = client.create("Alert", {"Alert type": "Agenda Posted"})
            self.assertEqual(result, "new_id_123")

    def test_raises_on_missing_id(self):
        client = _make_client()
        with unittest.mock.patch.object(client, "_request", return_value={"status": "success"}):
            with self.assertRaises(BubbleAPIError) as ctx:
                client.create("Alert", {"Alert type": "test"})
            self.assertIn("did not return an id", str(ctx.exception))

    def test_posts_to_correct_path(self):
        client = _make_client()
        with unittest.mock.patch.object(client, "_request", return_value={"id": "x"}) as mock_req:
            client.create("Alert", {"Alert type": "test"})
            mock_req.assert_called_once_with(
                "POST", "alert", json_body={"Alert type": "test"}
            )


class TestPatchMethod(unittest.TestCase):

    def test_patches_correct_path(self):
        client = _make_client()
        with unittest.mock.patch.object(client, "_request", return_value={}) as mock_req:
            client.patch("Calendar Item", "cal_001", {"alerts": ["a"]}, scope="patch_alerts")
            mock_req.assert_called_once_with(
                "PATCH", "calendaritem/cal_001", json_body={"alerts": ["a"]}
            )


if __name__ == "__main__":
    unittest.main()
