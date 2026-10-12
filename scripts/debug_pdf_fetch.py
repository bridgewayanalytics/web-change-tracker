"""
Concrete diagnostic for NAIC PDF download failures.
Runs both requests and Playwright paths and logs exactly what happens at each step.
Run locally: python3 scripts/debug_pdf_fetch.py
Run on ECS:  python3 scripts/debug_pdf_fetch.py  (via ECS task override)
"""

import sys
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

TEST_URLS = [
    "https://content.naic.org/sites/default/files/inline-files/fall-nm-comments_1.pdf",
    "https://content.naic.org/sites/default/files/inline-files/fall-nm-comments.pdf",
    "https://content.naic.org/sites/default/files/call_materials/10-07-26-E-SAPWG-agenda.pdf",
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def test_requests(url: str):
    import requests
    log.info("=== requests.get ===")
    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": UA,
            "Referer": "https://content.naic.org/",
            "Accept": "application/pdf,*/*",
        })
        log.info("  status: %s", resp.status_code)
        log.info("  content-type: %s", resp.headers.get("content-type", ""))
        log.info("  content-length: %s", resp.headers.get("content-length", ""))
        log.info("  body[:200]: %s", repr(resp.content[:200]))
        return resp.status_code == 200 and len(resp.content) > 1000
    except Exception as e:
        log.error("  requests failed: %s", e)
        return False


def test_playwright(url: str):
    log.info("=== Playwright ===")
    try:
        from playwright.sync_api import sync_playwright
        captured = []

        def on_response(response):
            log.info("  response: status=%s url=%s content-type=%s",
                     response.status, response.url[:100],
                     response.headers.get("content-type", ""))
            if url in response.url:
                log.info("  ** URL MATCH — trying to read body")
                try:
                    body = response.body()
                    log.info("  ** body length: %d", len(body))
                    captured.append(body)
                except Exception as e:
                    log.error("  ** response.body() failed: %s", e)

        def on_request(request):
            log.info("  request: %s %s", request.method, request.url[:100])

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=UA,
                extra_http_headers={
                    "Accept": "application/pdf,*/*",
                    "Referer": "https://content.naic.org/",
                    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
                accept_downloads=True,
            )
            page = ctx.new_page()
            page.on("response", on_response)
            page.on("request", on_request)

            log.info("  navigating to %s", url)
            try:
                page.goto(url, timeout=30000, wait_until="networkidle")
            except Exception as e:
                log.info("  goto raised: %s", e)
                # Wait a moment for any in-flight response.body() calls
                page.wait_for_timeout(3000)

            log.info("  final URL after navigation: %s", page.url)
            log.info("  page title: %s", page.title())
            log.info("  page content[:300]: %s", repr(page.content()[:300]))
            browser.close()

        if captured:
            log.info("  SUCCESS — captured %d bytes", len(captured[0]))
            # Try to extract text
            try:
                import io
                import pypdf
                reader = pypdf.PdfReader(io.BytesIO(captured[0]))
                text = " ".join(p.extract_text() or "" for p in reader.pages[:2])
                log.info("  PDF text sample: %s", text[:200])
            except Exception as e:
                log.info("  PDF text extraction: %s", e)
            return True
        else:
            log.warning("  FAILED — no bytes captured")
            return False

    except Exception as e:
        log.error("  Playwright failed: %s", e)
        return False


if __name__ == "__main__":
    import socket
    log.info("Running from host: %s", socket.gethostname())
    import urllib.request
    try:
        external_ip = urllib.request.urlopen("https://api.ipify.org", timeout=5).read().decode()
        log.info("External IP: %s", external_ip)
    except Exception as e:
        log.info("Could not get external IP: %s", e)

    urls = sys.argv[1:] if len(sys.argv) > 1 else TEST_URLS

    for url in urls:
        log.info("")
        log.info("=" * 60)
        log.info("URL: %s", url)
        log.info("=" * 60)
        r_ok = test_requests(url)
        log.info("requests result: %s", "OK" if r_ok else "FAILED")
        if not r_ok:
            p_ok = test_playwright(url)
            log.info("playwright result: %s", "OK" if p_ok else "FAILED")
