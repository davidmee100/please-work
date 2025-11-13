"""Playwright smoke test for the assessment tool export workflow.

This script launches Chromium against the bundled HTML assessment tool,
performs the minimal steps required to start an assessment, and then
triggers the "Save HTML with updated roles" export.

It intercepts the generated Blob so the exported HTML can be inspected
without writing files to disk.  The script asserts that the export
contains the refreshed ``<script id="site-defaults">`` payload as well as
JS that appears after the injection point, ensuring the export is not
truncated by stray ``</script>`` markers.

Usage
=====

    python tests/export_structure_html.py

The script depends on ``playwright`` being installed and will launch a
headless Chromium browser.  It automatically serves the repository over a
temporary HTTP server so no additional setup is required.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.server
import os
import socket
import threading
from pathlib import Path
from typing import Iterator, Optional

from playwright.async_api import async_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]
HTML_NAME = "Assessment Tool_281025_Latest (2) (6) (1) (2) (1).html"
HTML_PATH = REPO_ROOT / HTML_NAME


class SilentHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """A quiet HTTP handler that serves files from the repository root."""

    def log_message(self, format: str, *args) -> None:  # noqa: A003 - matches base signature
        # Silence default logging to keep the test output clean.
        pass


def _pick_free_port() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


@contextlib.contextmanager
def serve_repo() -> Iterator[str]:
    """Serve the repository over HTTP and yield the base URL."""

    original_cwd = os.getcwd()
    os.chdir(REPO_ROOT)
    sock = _pick_free_port()
    port = sock.getsockname()[1]

    handler = SilentHTTPRequestHandler
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler, False)
    httpd.socket = sock
    httpd.server_bind()
    httpd.server_activate()

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        os.chdir(original_cwd)


async def run_export_check(base_url: str) -> None:
    if not HTML_PATH.exists():
        raise FileNotFoundError(f"HTML asset not found: {HTML_PATH}")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        await page.goto(f"{base_url}/{HTML_NAME}", wait_until="load")
        await page.wait_for_selector("#startUserName")
        await page.fill("#startUserName", "Tester")
        await page.fill("#startPlantName", "Demo Plant")
        await page.click("text=Start")
        await page.wait_for_selector("#siteContent table#dataTable tbody")
        await page.click("#backToStartBtn")
        await page.wait_for_selector("#saveStructureStart")

        await page.evaluate(
            """
(() => {
  window.__exportHtml = null;
  const originalCreate = URL.createObjectURL;
  URL.createObjectURL = function(blob) {
    if (blob && typeof blob.text === 'function') {
      blob.text().then(text => { window.__exportHtml = text; });
    }
    return originalCreate.call(this, blob);
  };
})()
"""
        )

        async def handle_dialog(dialog) -> None:
            await dialog.accept()

        page.once("dialog", lambda dialog: asyncio.create_task(handle_dialog(dialog)))

        await page.click("#saveStructureStart")
        await page.wait_for_function("window.__exportHtml !== null && window.__exportHtml.length > 0")
        export_html = await page.evaluate("window.__exportHtml")

        if page_errors:
            raise AssertionError("Page errors encountered:\n" + "\n".join(page_errors))
        if "id=\"site-defaults\"" not in export_html:
            raise AssertionError("Export missing site-defaults payload")
        if "saveStructureVersion()" not in export_html and "saveStructureVersion();" not in export_html:
            raise AssertionError("Exported script appears truncated")
        if "</script>" not in export_html:
            raise AssertionError("Export missing closing script tags")

        print(f"Export HTML captured ({len(export_html)} bytes)")

        await browser.close()


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Validate the Save HTML export workflow")
    parser.parse_args(argv)

    with serve_repo() as base_url:
        asyncio.run(run_export_check(base_url))


if __name__ == "__main__":
    main()
