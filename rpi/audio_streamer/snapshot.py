"""Periodically capture the local web UI to a JPEG and POST it to the server.

Uses headless Chromium via selenium. Runs as a background async task in the
RPi audio streamer.
"""

import asyncio
import logging
import os

import aiohttp

log = logging.getLogger("hal.rpi.snapshot")


def _capture_screenshot_sync(width: int = 1024, height: int = 600) -> bytes | None:
    """Capture a screenshot of http://localhost:8080 using headless Chromium.

    Returns JPEG bytes, or None on failure. This is a blocking call —
    invoke from an executor.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except Exception as e:
        log.error(f"Selenium not available: {e}")
        return None

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--window-size={width},{height}")
    options.add_argument("--hide-scrollbars")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")

    driver = None
    try:
        # On Debian/Ubuntu chromium-driver is at /usr/bin/chromedriver
        service = Service(executable_path="/usr/bin/chromedriver")
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(15)
        driver.get("http://localhost:8080")
        # Give the WebSocket a moment to connect and the eye to render
        driver.implicitly_wait(2)
        # PNG → re-encode? Selenium returns PNG bytes from get_screenshot_as_png
        png = driver.get_screenshot_as_png()
        return png
    except Exception as e:
        log.warning(f"Screenshot failed: {e}")
        return None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def _png_to_jpeg(png_bytes: bytes, quality: int = 70) -> bytes | None:
    """Convert PNG bytes to JPEG bytes for smaller MQTT payload."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(png_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception as e:
        log.debug(f"PNG→JPEG conversion failed (Pillow missing?): {e}")
        return None


async def snapshot_loop(server_host: str, interval_sec: float = 5.0):
    """Background task: capture web UI and POST to server periodically."""
    if not server_host:
        log.info("Snapshot loop disabled (no server host)")
        return

    url = f"http://{server_host}:8765/api/snapshot"
    log.info(f"Snapshot loop starting; will POST to {url} every {interval_sec}s")

    loop = asyncio.get_event_loop()
    # Stagger startup so the page has time to load
    await asyncio.sleep(8)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                png = await loop.run_in_executor(None, _capture_screenshot_sync)
                if png:
                    jpeg = await loop.run_in_executor(None, _png_to_jpeg, png) or png
                    try:
                        async with session.post(
                            url,
                            data=jpeg,
                            headers={"Content-Type": "image/jpeg"},
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status >= 400:
                                log.warning(f"Snapshot POST returned {resp.status}")
                            else:
                                log.debug(f"Snapshot posted ({len(jpeg)} bytes)")
                    except Exception as e:
                        log.debug(f"Snapshot POST failed: {e}")
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning(f"Snapshot loop error: {e}")

            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                return
