"""
Test whether different User-Agent / header configurations affect Cloudflare challenge outcome.
Each config launches a fresh Playwright browser, fetches one NAIC URL, and reports
whether we got the challenge page or real content.
"""
import time
from playwright.sync_api import sync_playwright

TEST_URL = "https://content.naic.org/committees/e/statutory-accounting-principles-wg"

CF_MARKERS = (
    "cf-turnstile-response",
    "challenges.cloudflare.com",
    "Performing security verification",
    "_cf_chl_opt",
    "Just a moment",
)

def is_challenge(html: str) -> bool:
    return any(m in html for m in CF_MARKERS)

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = {runtime: {}};
"""

CONFIGS = [
    {
        "name": "Baseline (current prod)",
        "ua": None,  # Playwright default
        "extra_headers": {},
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    },
    {
        "name": "Chrome 131 + full sec-ch-ua headers",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "extra_headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
        },
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    },
    {
        "name": "Chrome 131 Mac + full sec-ch-ua headers",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "extra_headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
        },
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    },
    {
        "name": "Firefox 120 UA",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
        "extra_headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Upgrade-Insecure-Requests": "1",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
        },
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    },
    {
        "name": "iPhone Safari UA (mobile emulation)",
        "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "extra_headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
        },
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    },
    {
        "name": "No stealth flags at all (bare Playwright defaults)",
        "ua": None,
        "extra_headers": {},
        "args": ["--no-sandbox"],  # No AutomationControlled removal
    },
]

def test_config(cfg: dict, pw) -> dict:
    ctx_kwargs = dict(
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
    )
    if cfg["ua"]:
        ctx_kwargs["user_agent"] = cfg["ua"]
    if cfg["extra_headers"]:
        ctx_kwargs["extra_http_headers"] = cfg["extra_headers"]

    browser = pw.chromium.launch(headless=True, args=cfg["args"])
    ctx = browser.new_context(**ctx_kwargs)
    ctx.add_init_script(STEALTH_SCRIPT)
    page = ctx.new_page()

    try:
        page.goto(TEST_URL, wait_until="domcontentloaded", timeout=30000)
        title = page.title()
        if "Just a moment" in title:
            try:
                page.wait_for_function("document.title !== 'Just a moment...'", timeout=20000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
        html = page.content()
        challenged = is_challenge(html)
        final_title = page.title()
        size = len(html)
        return {
            "challenged": challenged,
            "title": final_title,
            "size": size,
            "body_preview": html[:200] if not challenged else "(challenge page)",
        }
    finally:
        browser.close()


def main():
    print(f"Testing URL: {TEST_URL}")
    print("=" * 70)

    with sync_playwright() as pw:
        for i, cfg in enumerate(CONFIGS):
            print(f"\n[{i+1}/{len(CONFIGS)}] {cfg['name']}")
            print(f"  UA: {cfg['ua'] or '(Playwright default)'}")
            t0 = time.time()
            result = test_config(cfg, pw)
            elapsed = time.time() - t0
            status = "❌ CHALLENGED" if result["challenged"] else "✅ REAL CONTENT"
            print(f"  Result: {status}  |  title={result['title']!r}  |  size={result['size']}  |  {elapsed:.1f}s")
            if not result["challenged"]:
                print(f"  Preview: {result['body_preview'][:150]}")
            # Brief pause between tests
            if i < len(CONFIGS) - 1:
                time.sleep(5)

    print("\n" + "=" * 70)
    print("Done.")


if __name__ == "__main__":
    main()
