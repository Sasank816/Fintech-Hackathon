"""Render the README art from the UI's own CSS.

    python docs/readme/build.py                     # app screenshot + every asset, light and dark
    python docs/readme/build.py --no-app            # keep the existing app.png
    python docs/readme/build.py --app-python PATH   # interpreter that has requirements.txt installed

Every [data-asset] element in assets.html becomes <name>-light.png and <name>-dark.png at 2x
(<name>.png when it is marked data-single). The app screenshot runs voiz fully offline (fake Stripe,
rule-based reader, outbox mail, local memory), drives it through the demo story and captures the board:
nothing leaves the machine.

Needs Playwright: `pip install playwright && playwright install chromium`.
"""
from __future__ import annotations

import argparse
import base64
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

SERVER = """
import logging, sys
logging.getLogger("clause.mail").setLevel(logging.ERROR)  # fake:// invoice PDFs are expected offline
sys.path.insert(0, {root!r})
import uvicorn
from clause.app import create_app
from clause.config import Settings
from clause.context import build_ctx
uvicorn.run(create_app(build_ctx(Settings(), fake=True), verify_keys=False),
            host="127.0.0.1", port={port}, log_level="warning")
"""

VOICE_NOTE = "Just finished at Bright Bakery: four hours labour, two RCDs and the call-out."
ACME_EMAIL = "Hi, please go ahead and invoice the extra 3 hours and the EICR. Thanks"


def default_app_python() -> str:
    for venv in (ROOT / ".venv", ROOT.parent / ".venv"):
        for exe in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
            if exe.is_file():
                return str(exe)
    return sys.executable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def shoot_app(pw, app_python: str) -> bytes:
    """The real UI after a voice note, a customer's email and a 45-day skip, as a 2x PNG."""
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen([app_python, "-c", SERVER.format(root=str(ROOT), port=port)], cwd=ROOT)
    try:
        for _ in range(60):
            try:
                call(base, "GET", "/api/state")
                break
            except OSError:
                if server.poll() is not None:
                    raise SystemExit(f"offline app exited ({server.returncode}); try --app-python")
                time.sleep(0.5)
        else:
            raise SystemExit("offline app did not start")
        call(base, "POST", "/api/sim/reset", {})
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.goto(base)
        page.wait_for_selector(".step")
        page.fill("#input", VOICE_NOTE)
        page.press("#input", "Enter")
        page.wait_for_selector(".tile >> text=£361.00")
        call(base, "POST", "/api/mail/inject", {"from": "acme@example.com", "subject": "Extra work", "body": ACME_EMAIL})
        page.click('[data-advance="45"]')
        page.wait_for_selector(".approvals-strip .tile")
        page.wait_for_timeout(1500)
        page.evaluate("document.querySelectorAll('.toast').forEach((t) => t.remove())")
        png = page.screenshot()
        browser.close()
        return png
    finally:
        server.terminate()
        server.wait(timeout=10)


def render(pw, app_png: bytes | None) -> list[Path]:
    written: list[Path] = []
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1000}, device_scale_factor=2)  # wide: no mobile rules
    for theme in ("light", "dark"):
        page.goto((HERE / "assets.html").as_uri() + f"?theme={theme}")
        page.wait_for_function("window.__ready === true")
        if app_png:
            page.eval_on_selector("#app-shot", "(img, src) => img.src = src",
                                  "data:image/png;base64," + base64.b64encode(app_png).decode())
            page.wait_for_function("document.getElementById('app-shot').complete")
        for el in page.query_selector_all("[data-asset]"):
            name = el.get_attribute("data-asset")
            single = el.get_attribute("data-single") is not None
            if single and theme == "dark":
                continue
            if name == "app" and not app_png:
                continue
            out = HERE / (f"{name}.png" if single else f"{name}-{theme}.png")
            el.screenshot(path=str(out), omit_background=True)
            written.append(out)
    browser.close()
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-app", action="store_true", help="skip the app screenshot and keep app.png")
    ap.add_argument("--app-python", default=default_app_python(), help="Python with requirements.txt installed")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        app_png = None if args.no_app else shoot_app(pw, args.app_python)
        for path in render(pw, app_png):
            print(f"{path.relative_to(ROOT).as_posix():40} {path.stat().st_size / 1024:7.1f} KB")


if __name__ == "__main__":
    main()
