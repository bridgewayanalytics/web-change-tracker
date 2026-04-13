"""
Strip an HTML page down to its main content for LLM comparison.

Removes nav, header, footer, aside, scripts, styles, and consent UI.
Scopes to main content element when available.
Returns cleaned HTML string (not plain text) to preserve structure.
"""

from bs4 import BeautifulSoup

# Mirrors spike.py allowlists — kept in sync manually
_CONSENT_REMOVE_IDS = {
    "onetrust-banner-sdk",
    "onetrust-consent-sdk",
    "ot-sdk-btn",
    "cybotcookiebotdialog",
}
_CONSENT_REMOVE_CLASSES = {
    "ot-sdk-container",
    "otfloatingroundedcorner",
    "cookie-banner",
    "cookie-consent",
    "consent-banner",
}

# Tags stripped wholesale before scoping
_STRIP_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "aside"]

# Ordered preference for main content container
_CONTENT_SELECTORS = (
    "main",
    "#content",
    '[role="main"]',
    ".region-content",
    "#block-mainpagecontent",
)


def _remove_consent_elements(soup: BeautifulSoup) -> None:
    def _should_remove(tag) -> bool:
        if not tag.name:
            return False
        id_val = (tag.get("id") or "").strip().lower()
        if id_val and id_val in _CONSENT_REMOVE_IDS:
            return True
        cls = tag.get("class") or []
        if isinstance(cls, str):
            cls = cls.split()
        return any(c.strip().lower() in _CONSENT_REMOVE_CLASSES for c in cls)

    for tag in sorted(
        soup.find_all(_should_remove),
        key=lambda t: len(list(t.parents)),
        reverse=True,
    ):
        if tag.parent:
            tag.decompose()


def strip_to_content(html: str) -> str:
    """
    Return cleaned HTML scoped to the main content area.

    Removes: script, style, noscript, nav, header, footer, aside, consent UI,
    and NAIC committee hierarchy nav sidebars (.cmte-list__wrapper).
    Scopes to first match of: main, #content, [role="main"], .region-content,
    #block-mainpagecontent; falls back to <body> or full document.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    _remove_consent_elements(soup)

    # Remove NAIC committee hierarchy nav sidebar (embedded inside #content on
    # committee pages, not in a <nav> tag — must be removed explicitly)
    for el in soup.find_all(class_="cmte-list__wrapper"):
        el.decompose()

    for selector in _CONTENT_SELECTORS:
        root = soup.select_one(selector)
        if root:
            return str(root)

    body = soup.find("body")
    return str(body) if body else str(soup)
