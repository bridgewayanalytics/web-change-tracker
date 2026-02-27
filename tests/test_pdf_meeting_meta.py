"""
Unit tests for scrape.pdf_meeting_meta (deterministic PDF meeting metadata extraction).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrape.pdf_meeting_meta import (
    MeetingMeta,
    extract_meeting_metadata_from_pdf,
    validate_meeting_meta,
)

FIXTURE_PDF = Path(__file__).resolve().parent / "fixtures" / "RTF 3.2.2026 Materials.pdf"


class TestExtractMeetingMetadataFromPdf(unittest.TestCase):
    def test_returns_none_for_empty_bytes(self):
        self.assertIsNone(extract_meeting_metadata_from_pdf("https://example.com/doc.pdf", b""))

    def test_returns_none_for_none_date(self):
        # Minimal PDF that produces no parseable date (single page with no date line)
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf not installed")
        from io import BytesIO
        w = PdfWriter()
        w.add_blank_page(width=72, height=72)
        buf = BytesIO()
        w.write(buf)
        pdf_bytes = buf.getvalue()
        result = extract_meeting_metadata_from_pdf("", pdf_bytes)
        self.assertIsNone(result, "Expected None when PDF has no date")

    @unittest.skipUnless(FIXTURE_PDF.exists(), f"Fixture PDF not found: {FIXTURE_PDF}")
    def test_extracts_from_fixture_rtf_3_2_2026_materials(self):
        """Using sample PDF fixture RTF 3.2.2026 Materials.pdf; assert group_name and date_iso."""
        pdf_bytes = FIXTURE_PDF.read_bytes()
        result = extract_meeting_metadata_from_pdf(
            "https://example.com/RTF_3.2.2026_Materials.pdf",
            pdf_bytes,
        )
        self.assertIsNotNone(result, "Expected MeetingMeta from fixture PDF")
        assert result is not None  # for type narrowing
        self.assertIn(
            "Reinsurance",
            result.group_name,
            f"group_name should contain 'Reinsurance', got: {result.group_name!r}",
        )
        self.assertEqual(
            result.date_iso,
            "2026-03-02",
            f"date_iso should be 2026-03-02, got: {result.date_iso!r}",
        )


class TestValidateMeetingMeta(unittest.TestCase):

    def test_valid_meta_passes(self):
        meta = MeetingMeta(
            group_name="Reinsurance (E) Task Force",
            date_iso="2026-03-02",
            start_time_local="14:00",
            end_time_local="15:00",
            timezone="ET",
        )
        result = validate_meeting_meta(meta)
        self.assertTrue(result["valid"])
        self.assertEqual(result["reasons"], [])

    def test_date_too_old_rejected(self):
        meta = MeetingMeta(
            group_name="Committee",
            date_iso="1989-06-03",
            start_time_local=None,
            end_time_local=None,
            timezone=None,
        )
        result = validate_meeting_meta(meta)
        self.assertFalse(result["valid"])
        self.assertTrue(any("1989" in r for r in result["reasons"]))

    def test_date_far_future_rejected(self):
        meta = MeetingMeta(
            group_name="Committee",
            date_iso="2099-01-01",
            start_time_local=None,
            end_time_local=None,
            timezone=None,
        )
        result = validate_meeting_meta(meta)
        self.assertFalse(result["valid"])
        self.assertTrue(any("2099" in r for r in result["reasons"]))

    def test_group_name_too_long_rejected(self):
        meta = MeetingMeta(
            group_name="A" * 81,
            date_iso="2026-03-02",
            start_time_local=None,
            end_time_local=None,
            timezone=None,
        )
        result = validate_meeting_meta(meta)
        self.assertFalse(result["valid"])
        self.assertTrue(any("too long" in r for r in result["reasons"]))

    def test_prose_group_name_rejected(self):
        meta = MeetingMeta(
            group_name="This is a full sentence. And this is another sentence that follows it.",
            date_iso="2026-03-02",
            start_time_local=None,
            end_time_local=None,
            timezone=None,
        )
        result = validate_meeting_meta(meta)
        self.assertFalse(result["valid"])
        self.assertTrue(any("prose" in r for r in result["reasons"]))

    def test_short_group_name_passes(self):
        meta = MeetingMeta(
            group_name="NAIC",
            date_iso="2026-03-02",
            start_time_local=None,
            end_time_local=None,
            timezone=None,
        )
        result = validate_meeting_meta(meta)
        self.assertTrue(result["valid"])

    def test_empty_group_name_passes(self):
        meta = MeetingMeta(
            group_name="",
            date_iso="2026-03-02",
            start_time_local=None,
            end_time_local=None,
            timezone=None,
        )
        result = validate_meeting_meta(meta)
        self.assertTrue(result["valid"])

    def test_2018_boundary_passes(self):
        meta = MeetingMeta(
            group_name="Committee",
            date_iso="2018-01-01",
            start_time_local=None,
            end_time_local=None,
            timezone=None,
        )
        result = validate_meeting_meta(meta)
        self.assertTrue(result["valid"])


if __name__ == "__main__":
    unittest.main()
