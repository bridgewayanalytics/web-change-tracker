"""Tests for bubble.calendar_alerts module."""

import unittest

from bubble.calendar_alerts import (
    attach_alerts_to_calendar_items,
    build_calendar_alerts,
    classify_alert_type,
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
        self.assertEqual(alert["type"], "new_agenda")
        self.assertEqual(alert["resource_name"], "March Agenda")
        self.assertEqual(alert["resource_url"], "https://ex.com/agenda.pdf")
        self.assertIn("detected_at", alert)

    def test_multiple_resources_same_calendar(self):
        resources = [
            {"Name": "Agenda", "URL": "https://ex.com/agenda.pdf",
             "notes": "", "Related calendar items": ["cal_001"]},
            {"Name": "Materials Packet", "URL": "https://ex.com/materials.pdf",
             "notes": "", "Related calendar items": ["cal_001"]},
        ]
        result = build_calendar_alerts(resources)
        self.assertEqual(len(result["cal_001"]), 2)
        types = {a["type"] for a in result["cal_001"]}
        self.assertEqual(types, {"new_agenda", "new_material"})

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
        self.assertEqual(result["cal_001"][0]["type"], "new_meeting_link")

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
        cals = [{"_id": "cal_001", "title": "Meeting", "Alerts": []}]
        result = attach_alerts_to_calendar_items(cals, {})
        self.assertEqual(result[0]["Alerts"], [])

    def test_attach_by_id(self):
        cals = [{"_id": "cal_001", "title": "Meeting", "Alerts": []}]
        alerts = {"cal_001": [{"type": "new_agenda", "resource_name": "Agenda",
                               "resource_url": "https://ex.com", "detected_at": "2026-03-09T00:00:00Z"}]}
        result = attach_alerts_to_calendar_items(cals, alerts)
        self.assertEqual(len(result[0]["Alerts"]), 1)
        self.assertEqual(result[0]["Alerts"][0]["type"], "new_agenda")

    def test_does_not_mutate_input(self):
        cals = [{"_id": "cal_001", "title": "Meeting", "Alerts": []}]
        alerts = {"cal_001": [{"type": "new_agenda", "resource_name": "A",
                               "resource_url": "", "detected_at": ""}]}
        result = attach_alerts_to_calendar_items(cals, alerts)
        self.assertEqual(cals[0]["Alerts"], [])  # original unchanged
        self.assertEqual(len(result[0]["Alerts"]), 1)

    def test_preserves_existing_alerts(self):
        existing = [{"type": "new_resource", "resource_name": "Old", "resource_url": "", "detected_at": ""}]
        cals = [{"_id": "cal_001", "title": "Meeting", "Alerts": list(existing)}]
        new_alerts = {"cal_001": [{"type": "new_agenda", "resource_name": "New",
                                   "resource_url": "", "detected_at": ""}]}
        result = attach_alerts_to_calendar_items(cals, new_alerts)
        self.assertEqual(len(result[0]["Alerts"]), 2)

    def test_calendar_without_id_gets_empty_alerts(self):
        """Newly-created calendar items (no _id yet) are left with empty Alerts."""
        cals = [{"title": "New Meeting", "Alerts": []}]
        alerts = {"cal_999": [{"type": "new_agenda", "resource_name": "A",
                               "resource_url": "", "detected_at": ""}]}
        result = attach_alerts_to_calendar_items(cals, alerts)
        self.assertEqual(result[0]["Alerts"], [])

    def test_calendar_without_alerts_field(self):
        """Safe when calendar item dict doesn't have Alerts key at all."""
        cals = [{"_id": "cal_001", "title": "Meeting"}]
        alerts = {"cal_001": [{"type": "new_material", "resource_name": "M",
                               "resource_url": "", "detected_at": ""}]}
        result = attach_alerts_to_calendar_items(cals, alerts)
        self.assertEqual(len(result[0]["Alerts"]), 1)


if __name__ == "__main__":
    unittest.main()
