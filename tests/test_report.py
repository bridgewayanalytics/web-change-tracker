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
    def _section_from_line(line: str) -> str | None:
        if re.match(r"^\s{2}(?:New documents|Removed documents):", line):
            return "Docs"
        if re.match(r"^\s{2}(?:New/updated meeting links|Removed meeting links):", line):
            return "Meeting Links"
        return None
    # Match item line "    + Title — https://..." or "    - Title — https://..."
    url_re = re.compile(r"\s{4}[+-]\s+.+?\s+—\s+(https?://\S+)")

    for line in report.splitlines():
        sec = _section_from_line(line)
        if sec:
            current_section = sec
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


# Forbidden patterns: old "Section: +X / -Y" style formatting (regression guards)
_FORBIDDEN_PATTERNS = [
    re.compile(r"Docs:\s*\+"),
    re.compile(r"Meeting Links:\s*\+"),
    re.compile(r"Events:\s*\+"),
    re.compile(r":\s*\+[^/]*/\s*-"),  # ": +" followed by "/ -"
]


def test_no_colon_plus_formatting() -> None:
    """Ensure report contains no old ': +' style formatting (Docs/Meeting Links/Events or ': +' / -)."""
    events = _mock_change_events()
    report = render_report(events, verbose=True)
    for pat in _FORBIDDEN_PATTERNS:
        m = pat.search(report)
        assert m is None, f"Report must not match {pat.pattern!r}; found: {m.group()!r}"


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
    for pat in _FORBIDDEN_PATTERNS:
        m = pat.search(report)
        assert m is None, f"Report must not match {pat.pattern!r}; found: {m.group()!r}"

    return report


if __name__ == "__main__":
    report = _run_assertions_and_save_report()
    print(report)
    print("\n--- Assertions passed. Report saved to tests/output_sample_report.txt")
