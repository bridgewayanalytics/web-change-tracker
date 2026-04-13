"""
Convert a stripped HTML page into section-based chunks for vectorization.

Each chunk is a dict with:
  - text:     Markdown-formatted section content (links preserved as [text](url))
  - metadata: Bubble-aligned fields identifying the source and section

Output is a list of chunks, written to JSONL by storage/chunk_s3.py.
One chunk per logical section of the page.
"""

from __future__ import annotations

import logging
from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

# Ordered section definitions for NAIC committee pages.
# Each entry: (css_selector, human_label)
# Tried in order; first match wins per section.
_NAIC_SECTIONS = [
    (".committee_page__body",           "About"),
    (".next-call",                       "Upcoming Meeting"),
    ("#content_1",                       "Meeting Materials"),
    (".webpost_tabs__content--selected", "Meeting Materials"),  # fallback tab selector
    ("#content_2",                       "Exposure Drafts"),
    (".exposure_drafts_content",         "Exposure Drafts"),
    ("#content_3",                       "Documents"),
    (".committee_page__related-documents", "Documents"),
    (".committee__education",            "Education & Training"),
    (".committee__related",              "Related Publications"),
    (".committee__contacts",             "Contacts"),
]

# For non-committee pages, fall back to splitting by heading tags
_HEADING_TAGS = ("h1", "h2", "h3", "h4")


# ---------------------------------------------------------------------------
# Markdown conversion
# ---------------------------------------------------------------------------

def _node_to_md(tag: Tag) -> str:
    """Recursively convert a BeautifulSoup tag to Markdown text."""
    if isinstance(tag, str):
        return tag.strip()

    name = getattr(tag, "name", None)
    if not name:
        return tag.get_text(strip=True)

    if name in ("script", "style", "noscript", "form", "input", "select", "button"):
        return ""

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        text = tag.get_text(strip=True)
        return f"\n{'#' * level} {text}\n" if text else ""

    if name == "a":
        href = (tag.get("href") or "").strip()
        text = tag.get_text(strip=True)
        if not text:
            return ""
        if href and not href.startswith("#"):
            # Make relative URLs absolute for NAIC
            if href.startswith("/"):
                href = f"https://content.naic.org{href}"
            return f"[{text}]({href})"
        return text

    if name in ("strong", "b"):
        text = tag.get_text(strip=True)
        return f"**{text}**" if text else ""

    if name in ("em", "i"):
        text = tag.get_text(strip=True)
        return f"*{text}*" if text else ""

    if name == "li":
        parts = [_node_to_md(c) for c in tag.children]
        text = " ".join(p for p in parts if p).strip()
        return f"- {text}" if text else ""

    if name in ("ul", "ol"):
        items = [_node_to_md(c) for c in tag.children if getattr(c, "name", None) == "li"]
        return "\n".join(i for i in items if i)

    if name == "tr":
        cells = []
        for td in tag.find_all(["td", "th"]):
            parts = [_node_to_md(c) for c in td.children]
            cell_text = " ".join(p for p in parts if p).strip()
            cells.append(cell_text or "")
        return "| " + " | ".join(cells) + " |" if cells else ""

    if name == "table":
        rows = []
        for tr in tag.find_all("tr"):
            row = _node_to_md(tr)
            if row:
                rows.append(row)
        if not rows:
            return ""
        # Insert markdown table header separator after first row
        if len(rows) >= 1:
            col_count = rows[0].count("|") - 1
            separator = "| " + " | ".join(["---"] * col_count) + " |"
            rows.insert(1, separator)
        return "\n".join(rows)

    if name == "p":
        parts = [_node_to_md(c) for c in tag.children]
        text = " ".join(p for p in parts if p).strip()
        return f"\n{text}\n" if text else ""

    if name == "br":
        return "\n"

    if name in ("div", "section", "article", "main", "span",
                "td", "th", "aside", "header", "footer"):
        parts = [_node_to_md(c) for c in tag.children]
        return " ".join(p for p in parts if p)

    # Default: extract text
    return tag.get_text(separator=" ", strip=True)


def _clean_md(text: str) -> str:
    """Remove known artifacts from converted Markdown."""
    import re
    # Remove /.classname artifacts from BeautifulSoup class attributes leaking in
    text = re.sub(r"\s*/\.\S+", "", text)
    # Collapse 3+ consecutive newlines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _section_to_markdown(section_tag: Tag, section_label: str) -> str:
    """Convert a section tag to clean Markdown, headed by the section label."""
    parts = []
    for child in section_tag.children:
        md = _node_to_md(child)
        if md and md.strip():
            parts.append(md.strip())
    content = _clean_md("\n\n".join(p for p in parts if p.strip()))
    return f"## {section_label}\n\n{content}" if content else ""


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------

def _extract_naic_sections(soup: BeautifulSoup) -> list[tuple[str, Tag]]:
    """
    Extract named sections from a NAIC committee page using known CSS selectors.
    Returns list of (section_label, tag) in page order.
    Deduplicates by label (first match wins).
    """
    seen_labels: set[str] = set()
    results: list[tuple[str, Tag]] = []

    for selector, label in _NAIC_SECTIONS:
        if label in seen_labels:
            continue
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            results.append((label, el))
            seen_labels.add(label)

    return results


def _extract_heading_sections(soup: BeautifulSoup) -> list[tuple[str, Tag]]:
    """
    Fallback: split page by heading tags for non-committee pages.
    Groups content between headings into synthetic section tags.
    """
    from bs4 import NavigableString

    body = soup.find("body") or soup
    sections: list[tuple[str, list]] = []
    current_label = "Overview"
    current_nodes: list = []

    for child in body.children:
        if isinstance(child, NavigableString):
            current_nodes.append(child)
            continue
        if not hasattr(child, "name"):
            continue
        if child.name in _HEADING_TAGS:
            heading_text = child.get_text(strip=True)
            if heading_text:
                if current_nodes:
                    sections.append((current_label, list(current_nodes)))
                current_label = heading_text
                current_nodes = []
        else:
            current_nodes.append(child)

    if current_nodes:
        sections.append((current_label, list(current_nodes)))

    # Wrap node lists into synthetic divs
    results = []
    for label, nodes in sections:
        wrapper = BeautifulSoup("<div></div>", "html.parser").find("div")
        for node in nodes:
            wrapper.append(node.__copy__() if hasattr(node, "__copy__") else node)
        results.append((label, wrapper))

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chunk_page(
    stripped_html: str,
    target_context: dict,
    run_timestamp: int | None = None,
) -> list[dict]:
    """
    Convert stripped HTML into section-based chunks ready for vectorization.

    Args:
        stripped_html:   Output of strip_to_content() — nav/scripts already removed.
        target_context:  Dict with keys: target_id, label, url, group, org_path.
        run_timestamp:   Unix timestamp of this run (for date_extracted metadata).

    Returns:
        List of chunk dicts, each with 'text' (Markdown str) and 'metadata' (dict).
    """
    soup = BeautifulSoup(stripped_html, "html.parser")

    # Detect page type: try NAIC committee sections first
    sections = _extract_naic_sections(soup)
    if not sections:
        log.debug("chunk_page: no NAIC sections found, falling back to heading-based split")
        sections = _extract_heading_sections(soup)

    if not sections:
        log.warning("chunk_page: no sections found for %s", target_context.get("target_id"))
        return []

    # Build metadata base
    from datetime import datetime, timezone
    date_extracted = (
        datetime.fromtimestamp(run_timestamp, tz=timezone.utc).isoformat()
        if run_timestamp
        else datetime.now(timezone.utc).isoformat()
    )

    base_metadata = {
        "target_id":      target_context.get("target_id", ""),
        "label":          target_context.get("label", ""),
        "url":            target_context.get("url", ""),
        "group":          target_context.get("group", ""),
        "org_path":       target_context.get("org_path", []),
        "content_type":   "Web Repository",
        "date_extracted": date_extracted,
    }

    chunks: list[dict] = []
    for idx, (section_label, section_tag) in enumerate(sections):
        md = _section_to_markdown(section_tag, section_label)
        if not md.strip():
            continue
        chunks.append({
            "text": md,
            "metadata": {
                **base_metadata,
                "section":     section_label,
                "chunk_index": idx,
            },
        })
        log.debug(
            "chunk_page: [%s] section=%r chars=%d",
            target_context.get("target_id"), section_label, len(md),
        )

    log.info(
        "chunk_page: %s → %d chunks from %d sections",
        target_context.get("target_id"), len(chunks), len(sections),
    )
    return chunks
