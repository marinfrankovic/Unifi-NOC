"""Check the generated static product page without starting a server."""
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tools/_shots"
OUTPUT.mkdir(exist_ok=True)
errors = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel=os.environ.get("NOC_BROWSER_CHANNEL", "msedge"), headless=True)
    try:
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        for width, height in ((1440, 1000), (1920, 900), (390, 844), (320, 568)):
            page.set_viewport_size({"width": width, "height": height})
            page.goto((ROOT / "site/dist/index.html").as_uri())
            page.locator("img").evaluate_all("images => images.forEach(image => image.loading = 'eager')")
            page.wait_for_function("[...document.images].every(image => image.complete && image.naturalWidth > 0)")
            expect(page.get_by_role("heading", name="Unifi-NOC", exact=True)).to_be_visible()
            expect(page.get_by_role("link", name="Deploy With Docker", exact=True)).to_have_attribute("href", "https://github.com/marinfrankovic/Unifi-NOC/blob/main/docs/deployment.md")
            if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
                raise AssertionError(f"Horizontal overflow at {width}px")
            brand = page.locator(".masthead .identity").bounding_box()
            navigation = page.locator(".masthead nav").bounding_box()
            if brand["x"] + brand["width"] > navigation["x"]:
                raise AssertionError(f"Navigation overlaps brand at {width}px")
            hero = page.locator(".hero").bounding_box()
            if hero["y"] + hero["height"] >= height:
                raise AssertionError(f"No following-section hint at {width}px")
            if "%%" in page.content():
                raise AssertionError("Unresolved release metadata")
            screenshot = page.screenshot(path=str(OUTPUT / f"site-{width}.png"), full_page=True, animations="disabled")
            if len(screenshot) < 25000:
                raise AssertionError("Unexpectedly empty product-page screenshot")
        if errors:
            raise AssertionError(errors)
        print("Product page PASS: 1440/1920 desktop, 390/320 mobile; images, links, metadata and layout")
    finally:
        browser.close()