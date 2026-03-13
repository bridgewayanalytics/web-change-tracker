"""Tests for bubble.calendar_alerts module."""

import json
import os
import unittest
import unittest.mock

from bubble.calendar_alerts import (
    ALERT_TYPE_LABELS,
    ALERTS_LOCAL_FILE,
    attach_alerts_to_calendar_items,
    build_calendar_alerts,
    classify_alert_type,
    upload_alerts_to_s3,
    write_alerts_local,
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
        # New fields
        self.assertEqual(alert["Related calendar item"], "cal_001")
        self.assertEqual(alert["Trigger URL"], "https://ex.com/agenda.pdf")
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
        # Each alert references its own calendar item
        self.assertEqual(result["cal_001"][0]["Related calendar item"], "cal_001")
        self.assertEqual(result["cal_002"][0]["Related calendar item"], "cal_002")

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


class TestWriteAlertsLocal(unittest.TestCase):
    """Tests for write_alerts_local (local JSON file)."""

    def setUp(self):
        self.alerts_by_cal = {
            "cal_001": [
                {"Alert type": "Agenda Posted", "date": "2026-03-10",
                 "Related calendar item": "cal_001", "Trigger URL": "https://ex.com/a.pdf",
                 "__alert_key": "new_agenda", "__resource_name": "Agenda", "__resource_url": "https://ex.com/a.pdf"},
            ],
        }

    def test_writes_flat_alert_list(self):
        write_alerts_local(self.alerts_by_cal)
        self.assertTrue(ALERTS_LOCAL_FILE.exists())
        data = json.loads(ALERTS_LOCAL_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["Alert type"], "Agenda Posted")
        self.assertEqual(data[0]["Related calendar item"], "cal_001")
        self.assertEqual(data[0]["Trigger URL"], "https://ex.com/a.pdf")

    def tearDown(self):
        if ALERTS_LOCAL_FILE.exists():
            ALERTS_LOCAL_FILE.unlink()


class TestUploadAlertsToS3(unittest.TestCase):
    """Tests for upload_alerts_to_s3 (mocked boto3)."""

    def setUp(self):
        self.alerts_by_cal = {
            "cal_001": [
                {"Alert type": "Agenda Posted", "date": "2026-03-10",
                 "Related calendar item": "cal_001", "Trigger URL": "https://ex.com/a.pdf",
                 "__alert_key": "new_agenda", "__resource_name": "Agenda", "__resource_url": "https://ex.com/a.pdf"},
            ],
        }
        self.run_timestamp = 1710300000

    @unittest.mock.patch.dict(os.environ, {"BUBBLE_ARTIFACT_BUCKET": ""}, clear=False)
    def test_skipped_when_no_bucket(self):
        result = upload_alerts_to_s3(self.alerts_by_cal, self.run_timestamp)
        self.assertEqual(result["uploaded"], 0)

    def test_empty_alerts_is_noop(self):
        result = upload_alerts_to_s3({}, self.run_timestamp)
        self.assertEqual(result["uploaded"], 0)

    @unittest.mock.patch.dict(os.environ, {"BUBBLE_ARTIFACT_BUCKET": "my-bucket"}, clear=False)
    def test_uploads_to_s3(self):
        import importlib
        import sys

        mock_boto3 = unittest.mock.MagicMock()
        mock_client = unittest.mock.MagicMock()
        mock_boto3.client.return_value = mock_client

        with unittest.mock.patch.dict(sys.modules, {"boto3": mock_boto3}):
            # Need to re-import so the function picks up mocked boto3
            result = upload_alerts_to_s3(self.alerts_by_cal, self.run_timestamp)

        self.assertEqual(result["uploaded"], 2)  # latest + versioned
        self.assertEqual(result["errors"], [])
        self.assertEqual(mock_client.put_object.call_count, 2)

        # Verify keys
        calls = mock_client.put_object.call_args_list
        keys = {c.kwargs["Key"] for c in calls}
        self.assertTrue(any(k == "alerts/latest.json" for k in keys))
        self.assertTrue(any(k.startswith("alerts/runs/") for k in keys))

    def tearDown(self):
        if ALERTS_LOCAL_FILE.exists():
            ALERTS_LOCAL_FILE.unlink()


if __name__ == "__main__":
    unittest.main()
