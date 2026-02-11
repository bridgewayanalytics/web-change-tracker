"""
Test harness for render_report: asserts report content and cross-section deduping.

Uses a fixed mocked diff payload with docs + meeting_links overlap.
Outputs the generated report to tests/output_sample_report.txt for review.

Run with: pytest tests/test_report.py -v
Or: python tests/test_report.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spike import render_report


def _mock_change_events() -> list[dict]:
    """
    Mock payload: one target with docs and meeting_links.
    - doc_overlap.pdf appears in BOTH docs and event_links (no distinct ML metadata -> keep in docs only)
    - doc_only.pdf appears only in docs
    - link_only.htm appears only in event_links (Agenda label -> distinct metadata -> kept in ML)
    """
    overlap_url = "https://content.naic.org/committees/e/life-rbc-wg/doc_overlap.pdf"
    doc_only_url = "https://content.naic.org/committees/e/life-rbc-wg/doc_only.pdf"
    agenda_url = "https://content.naic.org/committees/e/life-rbc-wg/agenda.htm"

    return [
        {
            "label": "Life Risk-Based Capital Working Group",
            "url": "https://content.naic.org/committees/e/life-risk-based-capital-wg",
            "org_id": "naic",
            "org_path": ["NAIC", "E", "Working Groups"],
            "change": {
                "first_run": False,
                "page_changed": False,
                "by_type": {
                    "docs": {
                        "added": [
                            {"title": "Overlap PDF", "url": overlap_url},
                            {"title": "Doc Only", "url": doc_only_url},
                        ],
                        "removed": [],
                    },
                    "event_links": {
                        "added": [
                            {"title": "Overlap PDF", "url": overlap_url},  # same URL - no distinct metadata
                            {"title": "Meeting Agenda", "url": agenda_url},  # "Agenda" -> distinct, kept in ML
                        ],
                        "removed": [],
                    },
                    "events": {"added": [], "removed": []},
                    "meetings": {"added": [], "removed": []},
                },
            },
        },
    ]


def _extract_urls_by_section(report: str) -> tuple[set[str], set[str]]:
    """
    Parse report and extract URLs from Docs section vs Meeting Links section.
    Returns (docs_urls, meeting_links_urls).
    """
    docs_urls: set[str] = set()
    meeting_links_urls: set[str] = set()
    current_section: str | None = None
    # Match "  Docs: +n / -n" or "  Meeting Links: +n / -n"
    section_re = re.compile(r"^\s{2}(Docs|Meeting Links):\s+\+\d+")
    # Match item line "    + Title — https://..." or "    - Title — https://..."
    url_re = re.compile(r"\s{4}[+-]\s+.+?\s+—\s+(https?://\S+)")

    for line in report.splitlines():
        m = section_re.match(line)
        if m:
            current_section = m.group(1)
            continue
        u = url_re.search(line)
        if u and current_section:
            url = u.group(1).rstrip()
            if current_section == "Docs":
                docs_urls.add(url)
            elif current_section == "Meeting Links":
                meeting_links_urls.add(url)
        # Reset section when we hit a new target (Hierarchy: or empty-ish)
        if line.startswith("Hierarchy:") or (line.strip() and not line.startswith(" ") and not line.startswith("  ")):
            current_section = None

    return docs_urls, meeting_links_urls


def test_report_content_and_dedup() -> None:
    """Generate report from mocked payload and assert required content and no duplicate URLs."""
    _run_assertions_and_save_report()


def test_no_docs_plus_style_malformed() -> None:
    """Ensure report does not contain malformed 'Docs: +' without a number."""
    events = _mock_change_events()
    report = render_report(events, verbose=True)
    # "Docs: +" followed by non-digit would be malformed; our format is "Docs: +1 / -0"
    assert "Docs: +\n" not in report
    assert re.search(r"Docs:\s+\+\s*[^\d\s/]", report) is None, "Malformed 'Docs: +' style line found"


def _run_assertions_and_save_report() -> str:
    """Generate report, run assertions, save to output_sample_report.txt. Returns report."""
    events = _mock_change_events()
    report = render_report(events, verbose=True)
    output_file = Path(__file__).parent / "output_sample_report.txt"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(report, encoding="utf-8")

    assert "Identified website updates" in report
    assert "New documents:" in report
    assert "New/updated meeting links:" in report
    docs_urls, meeting_links_urls = _extract_urls_by_section(report)
    overlap = docs_urls & meeting_links_urls
    assert not overlap, f"URLs appear in both Docs and Meeting Links: {overlap}"

    return report


if __name__ == "__main__":
    report = _run_assertions_and_save_report()
    print(report)
    print("\n--- Assertions passed. Report saved to tests/output_sample_report.txt")
