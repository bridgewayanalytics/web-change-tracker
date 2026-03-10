"""Tests for bubble.calendar_alerts module."""

import os
import unittest
import unittest.mock

from bubble.calendar_alerts import (
    ALERT_TYPE_LABELS,
    attach_alerts_to_calendar_items,
    build_calendar_alerts,
    classify_alert_type,
    flush_alerts_to_bubble,
)


class TestClassifyAlertType(unittest.TestCase):

    def test_agenda_keyword(self):
        res = {"Name": "March 2026 Agenda", "URL": "https://example.com/agenda.pdf", "notes": ""}
        self.assertEqual(classify_alert_type(res), "new_agenda")

    def test_materials_keyword(self):
        res = {"Name": "Meeting Materials", "URL": "https://example.com/doc.pdf", "notes": ""}
        self.assertEqual(classify_alert_type(res), "new_material")

    def test_minutes_keyword(self):
        res = {"Name": "Meeting Minutes", "URL": "https://example.com/min.pdf", "notes": ""}
        self.assertEqual(classify_alert_type(res), "new_material")

    def test_webex_keyword(self):
        res = {"Name": "Join Call", "URL": "https://webex.com/meet/123", "notes": ""}
        self.assertEqual(classify_alert_type(res), "new_meeting_link")

    def test_section_type_fallback(self):
        res = {"Name": "Some Link", "URL": "https://example.com", "notes": ""}
        self.assertEqual(classify_alert_type(res, section_type="event_links"), "new_meeting_link")

    def test_generic_resource(self):
        res = {"Name": "Risk Report", "URL": "https://example.com/report.pdf", "notes": ""}
        self.assertEqual(classify_alert_type(res), "new_resource")

    def test_empty_resource(self):
        self.assertEqual(classify_alert_type({}), "new_resource")


class TestAlertTypeLabels(unittest.TestCase):

    def test_all_keys_have_labels(self):
        for key in ("new_agenda", "new_material", "new_meeting_link", "new_resource"):
            self.assertIn(key, ALERT_TYPE_LABELS)

    def test_label_values(self):
        self.assertEqual(ALERT_TYPE_LABELS["new_agenda"], "Agenda Posted")
        self.assertEqual(ALERT_TYPE_LABELS["new_material"], "Materials Posted")
        self.assertEqual(ALERT_TYPE_LABELS["new_meeting_link"], "Meeting Link Posted")
        self.assertEqual(ALERT_TYPE_LABELS["new_resource"], "New Resource")


class TestBuildCalendarAlerts(unittest.TestCase):

    def test_no_related_calendar_items(self):
        resources = [{"Name": "Doc", "Related calendar items": []}]
        result = build_calendar_alerts(resources)
        self.assertEqual(result, {})

    def test_single_resource_single_calendar(self):
        resources = [
            {"Name": "March Agenda", "URL": "https://ex.com/agenda.pdf",
             "notes": "", "Related calendar items": ["cal_001"]},
        ]
        result = build_calendar_alerts(resources)
        self.assertIn("cal_001", result)
        self.assertEqual(len(result["cal_001"]), 1)
        alert = result["cal_001"][0]
        self.assertEqual(alert["Alert type"], "Agenda Posted")
        self.assertRegex(alert["date"], r"^\d{4}-\d{2}-\d{2}$")
        # Debug metadata present
        self.assertEqual(alert["__alert_key"], "new_agenda")
        self.assertEqual(alert["__resource_name"], "March Agenda")
        self.assertEqual(alert["__resource_url"], "https://ex.com/agenda.pdf")

    def test_multiple_resources_same_calendar(self):
        resources = [
            {"Name": "Agenda", "URL": "https://ex.com/agenda.pdf",
             "notes": "", "Related calendar items": ["cal_001"]},
            {"Name": "Materials Packet", "URL": "https://ex.com/materials.pdf",
             "notes": "", "Related calendar items": ["cal_001"]},
        ]
        result = build_calendar_alerts(resources)
        self.assertEqual(len(result["cal_001"]), 2)
        types = {a["Alert type"] for a in result["cal_001"]}
        self.assertEqual(types, {"Agenda Posted", "Materials Posted"})

    def test_resource_linked_to_multiple_calendars(self):
        resources = [
            {"Name": "Agenda", "URL": "https://ex.com/a.pdf",
             "notes": "", "Related calendar items": ["cal_001", "cal_002"]},
        ]
        result = build_calendar_alerts(resources)
        self.assertIn("cal_001", result)
        self.assertIn("cal_002", result)
        self.assertEqual(len(result["cal_001"]), 1)
        self.assertEqual(len(result["cal_002"]), 1)

    def test_uses_section_type_from_context(self):
        resources = [
            {"Name": "Join Link", "URL": "https://example.com/join",
             "notes": "", "Related calendar items": ["cal_001"]},
        ]
        ctx = [{"section_type": "event_links"}]
        result = build_calendar_alerts(resources, resource_context=ctx)
        self.assertEqual(result["cal_001"][0]["Alert type"], "Meeting Link Posted")

    def test_none_related_calendar_items(self):
        resources = [{"Name": "Doc", "Related calendar items": None}]
        result = build_calendar_alerts(resources)
        self.assertEqual(result, {})

    def test_skips_empty_cal_ids(self):
        resources = [
            {"Name": "Doc", "URL": "", "notes": "",
             "Related calendar items": ["", None, "cal_001"]},
        ]
        result = build_calendar_alerts(resources)
        self.assertNotIn("", result)
        self.assertIn("cal_001", result)


class TestAttachAlertsToCalendarItems(unittest.TestCase):

    def test_no_alerts(self):
        cals = [{"_id": "cal_001", "title": "Meeting", "alerts": []}]
        result = attach_alerts_to_calendar_items(cals, {})
        self.assertEqual(result[0]["alerts"], [])

    def test_attach_by_id(self):
        cals = [{"_id": "cal_001", "title": "Meeting", "alerts": []}]
        alerts = {"cal_001": [{"Alert type": "Agenda Posted", "date": "2026-03-09"}]}
        result = attach_alerts_to_calendar_items(cals, alerts)
        self.assertEqual(len(result[0]["alerts"]), 1)
        self.assertEqual(result[0]["alerts"][0]["Alert type"], "Agenda Posted")

    def test_does_not_mutate_input(self):
        cals = [{"_id": "cal_001", "title": "Meeting", "alerts": []}]
        alerts = {"cal_001": [{"Alert type": "Agenda Posted", "date": "2026-03-10"}]}
        result = attach_alerts_to_calendar_items(cals, alerts)
        self.assertEqual(cals[0]["alerts"], [])  # original unchanged
        self.assertEqual(len(result[0]["alerts"]), 1)

    def test_preserves_existing_alerts(self):
        existing = [{"Alert type": "New Resource", "date": "2026-03-08"}]
        cals = [{"_id": "cal_001", "title": "Meeting", "alerts": list(existing)}]
        new_alerts = {"cal_001": [{"Alert type": "Agenda Posted", "date": "2026-03-10"}]}
        result = attach_alerts_to_calendar_items(cals, new_alerts)
        self.assertEqual(len(result[0]["alerts"]), 2)

    def test_calendar_without_id_gets_empty_alerts(self):
        """Newly-created calendar items (no _id yet) are left with empty alerts."""
        cals = [{"title": "New Meeting", "alerts": []}]
        alerts = {"cal_999": [{"Alert type": "Agenda Posted", "date": "2026-03-10"}]}
        result = attach_alerts_to_calendar_items(cals, alerts)
        self.assertEqual(result[0]["alerts"], [])

    def test_calendar_without_alerts_field(self):
        """Safe when calendar item dict doesn't have alerts key at all."""
        cals = [{"_id": "cal_001", "title": "Meeting"}]
        alerts = {"cal_001": [{"Alert type": "Materials Posted", "date": "2026-03-10"}]}
        result = attach_alerts_to_calendar_items(cals, alerts)
        self.assertEqual(len(result[0]["alerts"]), 1)


class TestFlushAlertsToBubble(unittest.TestCase):
    """Tests for flush_alerts_to_bubble (mocked Bubble client)."""

    def setUp(self):
        self.alerts_by_cal = {
            "cal_001": [
                {"Alert type": "Agenda Posted", "date": "2026-03-10",
                 "__alert_key": "new_agenda", "__resource_name": "Agenda", "__resource_url": "https://ex.com/a.pdf"},
            ],
        }
        self.calendar_items = [{"_id": "cal_001", "title": "Meeting", "alerts": []}]

    @unittest.mock.patch.dict(os.environ, {"BUBBLE_ALERTS_ENABLED": ""}, clear=False)
    def test_skipped_when_not_enabled(self):
        result = flush_alerts_to_bubble(self.calendar_items, self.alerts_by_cal)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["patched"], 0)

    @unittest.mock.patch.dict(os.environ, {"BUBBLE_ALERTS_ENABLED": "true"}, clear=False)
    @unittest.mock.patch("bubble.client.get_client")
    def test_creates_and_patches(self, mock_get_client):
        mock_client = unittest.mock.MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.create.return_value = "alert_id_001"
        mock_client.get.return_value = {"alerts": []}

        result = flush_alerts_to_bubble(self.calendar_items, self.alerts_by_cal)

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["patched"], 1)
        self.assertEqual(result["errors"], [])

        # Verify create was called with correct Bubble fields (no __ debug keys)
        mock_client.create.assert_called_once_with(
            "Alert", {"Alert type": "Agenda Posted", "date": "2026-03-10"}
        )
        # Verify patch was called with alert ID list
        mock_client.patch.assert_called_once_with(
            "Calendar Item", "cal_001", {"alerts": ["alert_id_001"]}, scope="patch_alerts"
        )

    @unittest.mock.patch.dict(os.environ, {"BUBBLE_ALERTS_ENABLED": "true"}, clear=False)
    @unittest.mock.patch("bubble.client.get_client")
    def test_appends_to_existing_alerts(self, mock_get_client):
        mock_client = unittest.mock.MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.create.return_value = "alert_id_002"
        mock_client.get.return_value = {"alerts": ["existing_alert_id"]}

        result = flush_alerts_to_bubble(self.calendar_items, self.alerts_by_cal)

        # Should append new ID after existing
        mock_client.patch.assert_called_once_with(
            "Calendar Item", "cal_001",
            {"alerts": ["existing_alert_id", "alert_id_002"]},
            scope="patch_alerts",
        )

    @unittest.mock.patch.dict(os.environ, {"BUBBLE_ALERTS_ENABLED": "true"}, clear=False)
    @unittest.mock.patch("bubble.client.get_client")
    def test_create_failure_does_not_raise(self, mock_get_client):
        from bubble.client import BubbleAPIError

        mock_client = unittest.mock.MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.create.side_effect = BubbleAPIError("network error")

        result = flush_alerts_to_bubble(self.calendar_items, self.alerts_by_cal)

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["patched"], 0)
        self.assertEqual(len(result["errors"]), 1)

    @unittest.mock.patch.dict(os.environ, {"BUBBLE_ALERTS_ENABLED": "true"}, clear=False)
    @unittest.mock.patch("bubble.client.get_client")
    def test_empty_alerts_is_noop(self, mock_get_client):
        result = flush_alerts_to_bubble(self.calendar_items, {})
        self.assertEqual(result["created"], 0)
        mock_get_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
