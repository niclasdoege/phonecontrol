#!/usr/bin/env python3
"""
Autonomous GameNative config tester — NO API KEY required.

Uses Playwright browser automation to query ChatGPT (or Claude.ai) via their
web interface, so you just need to be logged in once. The local phonecontrol
server's /dashboard page is screenshotted and uploaded to the AI chatbot.

Usage:
    python browser_tester.py --game "Your Game" [--provider chatgpt|claude]

First run opens a browser so you can log in. Subsequent runs reuse the session.

Requirements:
    pip install playwright requests
    playwright install chromium
    # On Raspberry Pi if bundled chromium fails:
    sudo apt install chromium-browser
    playwright install --skip-validation chromium  # or set PLAYWRIGHT_CHROMIUM_PATH
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import tempfile
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout

from configs import CONFIGS, STABILITY_WINDOW, OBSERVE_INTERVAL, MAX_ACTIONS_PER_CONFIG

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"browser_tester_{datetime.now():%Y%m%d_%H%M%S}.log"),
    ],
)
log = logging.getLogger(__name__)

RESULTS_FILE = Path("results.json")
BROWSER_DATA = Path("browser_data")  # persists login sessions

PROVIDERS = {
    "chatgpt": {
        "url": "https://chatgpt.com",
        "name": "ChatGPT",
    },
    "claude": {
        "url": "https://claude.ai",
        "name": "Claude.ai",
    },
    "gemini": {
        "url": "https://gemini.google.com",
        "name": "Gemini",
    },
}


# ── Phone server helper ────────────────────────────────────────────────────────

class Phone:
    def __init__(self, server: str):
        self.base = server.rstrip("/")
        self._s = requests.Session()

    def _post(self, path, body, silent=False):
        try:
            r = self._s.post(f"{self.base}{path}", json=body, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if not silent: log.warning(f"POST {path}: {e}")
            return {}

    def is_alive(self):
        try:
            r = self._s.get(f"{self.base}/api/status", timeout=5)
            return r.ok and r.json().get("connected", False)
        except Exception:
            return False

    def tap(self, x, y):
        log.info(f"  tap({x},{y})")
        self._post("/api/tap", {"x": x, "y": y})
        time.sleep(0.6)

    def swipe(self, x1, y1, x2, y2, ms=300):
        log.info(f"  swipe({x1},{y1}→{x2},{y2})")
        self._post("/api/swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": ms})
        time.sleep(0.6)

    def long_press(self, x, y):
        log.info(f"  long_press({x},{y})")
        self._post("/api/long_press", {"x": x, "y": y})
        time.sleep(1.0)

    def key(self, k):
        log.info(f"  key({k})")
        self._post("/api/key", {"key": k})
        time.sleep(0.5)

    def type_text(self, text):
        log.info(f"  type({text!r})")
        self._post("/api/type", {"text": text})
        time.sleep(0.5)

    def dashboard_url(self, config="", history="", question=""):
        import urllib.parse
        params = urllib.parse.urlencode({"config": config, "history": history, "question": question})
        return f"{self.base}/dashboard?{params}"


# ── AI provider backends ───────────────────────────────────────────────────────

async def ask_chatgpt(page: Page, dashboard_url: str) -> str:
    """Open ChatGPT, start fresh conversation, upload dashboard screenshot, return response."""
    # Navigate to a new chat
    try:
        await page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
    except PWTimeout:
        log.warning("ChatGPT navigation timeout, continuing…")

    # Take a screenshot of the dashboard to upload
    dash_page = await page.context.new_page()
    try:
        await dash_page.goto(dashboard_url, wait_until="networkidle", timeout=15000)
        await asyncio.sleep(1)  # let screenshot load
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
        await dash_page.screenshot(path=tmp_path, full_page=True)
    finally:
        await dash_page.close()

    try:
        # Find file upload input — ChatGPT has a hidden input[type=file]
        # Try clicking the attachment button first
        for attach_sel in [
            'button[aria-label*="ttach"]',
            'button[aria-label*="ile"]',
            'button[data-testid="attachment"]',
            'button[title*="ttach"]',
            '[data-testid="attach-file"]',
        ]:
            try:
                btn = page.locator(attach_sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

        # Set file on input[type=file] — works whether button was clicked or not
        file_input = page.locator('input[type="file"]').first
        await file_input.set_input_files(tmp_path, timeout=5000)
        await asyncio.sleep(1)
        log.info("  Uploaded dashboard screenshot to ChatGPT")
    except Exception as e:
        log.warning(f"  File upload failed: {e} — sending text-only prompt")
    finally:
        os.unlink(tmp_path)

    # Type the prompt — just reference the uploaded image
    prompt = (
        "Look at the dashboard screenshot I uploaded. "
        "The left side shows the current phone screen running GameNative. "
        "The right side shows the context, test history, and what you need to decide. "
        "Respond ONLY with the JSON object as specified in the 'Required Response Format' section. "
        "No explanation, no markdown, just the raw JSON."
    )

    # Find and fill the text input
    for input_sel in [
        'div#prompt-textarea[contenteditable]',
        '#prompt-textarea',
        'textarea[placeholder]',
        'div[contenteditable="true"]',
    ]:
        try:
            inp = page.locator(input_sel).first
            if await inp.is_visible(timeout=3000):
                await inp.click()
                await inp.fill(prompt)
                break
        except Exception:
            pass

    # Submit
    for send_sel in [
        'button[data-testid="send-button"]',
        'button[aria-label*="end"]',
        'button[type="submit"]',
    ]:
        try:
            btn = page.locator(send_sel).first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                break
        except Exception:
            pass

    # Wait for streaming to complete
    log.info("  Waiting for ChatGPT response…")
    response_text = await _wait_for_response_chatgpt(page)
    return response_text


async def _wait_for_response_chatgpt(page: Page, timeout: int = 90) -> str:
    """Poll until the response stops growing."""
    deadline = time.time() + timeout
    last_text = ""
    stable_count = 0

    while time.time() < deadline:
        await asyncio.sleep(2)
        # Get the last assistant message
        for sel in [
            '[data-message-author-role="assistant"] .markdown',
            '[data-message-author-role="assistant"]',
            '.agent-turn .markdown',
            '.agent-turn',
        ]:
            try:
                els = page.locator(sel)
                count = await els.count()
                if count > 0:
                    text = await els.last.inner_text()
                    if text == last_text:
                        stable_count += 1
                        if stable_count >= 2:
                            log.info(f"  Response stable ({len(text)} chars)")
                            return text
                    else:
                        last_text = text
                        stable_count = 0
                    break
            except Exception:
                pass

    log.warning("Response timeout — returning what we have")
    return last_text


async def ask_claude_ai(page: Page, dashboard_url: str) -> str:
    """Open Claude.ai, upload dashboard screenshot, return response."""
    try:
        await page.goto("https://claude.ai/new", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
    except PWTimeout:
        pass

    # Take dashboard screenshot
    dash_page = await page.context.new_page()
    try:
        await dash_page.goto(dashboard_url, wait_until="networkidle", timeout=15000)
        await asyncio.sleep(1)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
        await dash_page.screenshot(path=tmp_path, full_page=True)
    finally:
        await dash_page.close()

    try:
        file_input = page.locator('input[type="file"]').first
        await file_input.set_input_files(tmp_path, timeout=5000)
        await asyncio.sleep(1)
        log.info("  Uploaded dashboard screenshot to Claude.ai")
    except Exception as e:
        log.warning(f"  File upload failed: {e}")
    finally:
        os.unlink(tmp_path)

    prompt = (
        "Look at the dashboard screenshot. Left = phone screen (GameNative). "
        "Right = context and what to decide. "
        "Respond with ONLY the raw JSON as specified. No other text."
    )

    for sel in ['div[contenteditable="true"]', 'div.ProseMirror', 'textarea']:
        try:
            inp = page.locator(sel).first
            if await inp.is_visible(timeout=3000):
                await inp.click()
                await inp.fill(prompt)
                break
        except Exception:
            pass

    for sel in ['button[aria-label="Send message"]', 'button[type="submit"]']:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                break
        except Exception:
            pass

    log.info("  Waiting for Claude.ai response…")
    return await _wait_for_response_claude(page)


async def _wait_for_response_claude(page: Page, timeout: int = 90) -> str:
    deadline = time.time() + timeout
    last_text = ""
    stable_count = 0
    while time.time() < deadline:
        await asyncio.sleep(2)
        for sel in [
            '[data-is-streaming="false"] .prose',
            '.prose',
            '[data-message-author-role="assistant"]',
        ]:
            try:
                els = page.locator(sel)
                count = await els.count()
                if count > 0:
                    text = await els.last.inner_text()
                    if text == last_text:
                        stable_count += 1
                        if stable_count >= 2:
                            return text
                    else:
                        last_text = text
                        stable_count = 0
                    break
            except Exception:
                pass
    return last_text


# ── JSON extraction ────────────────────────────────────────────────────────────

def extract_json(text: str) -> dict | None:
    if not text:
        return None
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find first {...} block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    log.warning(f"Could not parse JSON from: {text[:200]}")
    return None


# ── Action executor ────────────────────────────────────────────────────────────

def execute_actions(phone: Phone, actions: list[dict]):
    for a in actions:
        do = a.get("do", "")
        why = a.get("why", "")
        log.info(f"    → {do}: {why}")
        if do == "tap":
            phone.tap(int(a["x"]), int(a["y"]))
        elif do == "swipe":
            phone.swipe(int(a["x1"]), int(a["y1"]), int(a["x2"]), int(a["y2"]),
                        int(a.get("ms", 300)))
        elif do == "long_press":
            phone.long_press(int(a["x"]), int(a["y"]))
        elif do == "key":
            phone.key(a["key"])
        elif do == "type":
            phone.type_text(a["text"])
        elif do == "wait":
            secs = float(a.get("seconds", 1))
            log.info(f"  sleeping {secs}s")
            time.sleep(secs)


# ── Results ────────────────────────────────────────────────────────────────────

def load_results() -> list[dict]:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return []

def save_result(config, outcome, notes=""):
    results = load_results()
    results.append({"config": config, "outcome": outcome, "notes": notes,
                    "timestamp": datetime.now().isoformat()})
    RESULTS_FILE.write_text(json.dumps(results, indent=2))


# ── Main config test loop ──────────────────────────────────────────────────────

async def test_config(phone: Phone, ai_page: Page, provider: str,
                      config: dict, game: str) -> str:
    config_str = json.dumps(config)
    results = load_results()
    history_lines = [f"{r['config']} → {r['outcome']}" for r in results[-10:]]
    history_str = "\n".join(history_lines) or "none"

    action_count = 0
    observe_start = None
    phase = "apply"

    while action_count < MAX_ACTIONS_PER_CONFIG:
        if phase == "apply":
            question = (
                f'Target game: "{game}". '
                f"Apply this config in GameNative settings then launch the game: {config_str}. "
                "Set done_with_config=true once the game is launched. "
                "Set config_failed=true if the game crashes immediately or you cannot proceed."
            )
        else:
            elapsed = time.time() - observe_start
            remaining = max(0, STABILITY_WINDOW - elapsed)
            question = (
                f'Config under test: {config_str}. '
                f"OBSERVATION: {elapsed:.0f}s elapsed, {remaining:.0f}s remaining. "
                f"Is the game running stably? Set stable=true if running without crash for "
                f"{STABILITY_WINDOW}s. Set config_failed=true if crashed."
            )

        dash_url = phone.dashboard_url(config=config_str, history=history_str, question=question)

        log.info(f"  Querying {provider}…")
        if provider == "claude":
            raw = await ask_claude_ai(ai_page, dash_url)
        else:
            raw = await ask_chatgpt(ai_page, dash_url)

        analysis = extract_json(raw)
        if not analysis:
            log.warning("  No valid JSON in response — retrying with a wait")
            await asyncio.sleep(5)
            action_count += 1
            continue

        log.info(f"  state={analysis.get('state')} game={analysis.get('game_running')} "
                 f"stable={analysis.get('stable')} fps={analysis.get('fps')}")
        if analysis.get("description"):
            log.info(f"  AI: {analysis['description']}")

        actions = analysis.get("actions", [])
        execute_actions(phone, actions)
        action_count += len(actions) or 1  # always increment to avoid infinite loops

        if analysis.get("config_failed"):
            log.info("Config failed")
            return "crashed"

        if phase == "apply" and analysis.get("done_with_config"):
            log.info("Game launched — entering observation phase")
            phase = "observe"
            observe_start = time.time()
            await asyncio.sleep(OBSERVE_INTERVAL)
            continue

        if phase == "observe":
            elapsed = time.time() - observe_start
            if analysis.get("stable") and elapsed >= STABILITY_WINDOW:
                log.info(f"STABLE! {elapsed:.0f}s, fps={analysis.get('fps')}")
                return "stable"
            if elapsed >= STABILITY_WINDOW:
                return "stable" if analysis.get("game_running") else "crashed"
            await asyncio.sleep(OBSERVE_INTERVAL)
            continue

        if not actions:
            await asyncio.sleep(3)

    log.info("Max actions reached")
    return "timeout"


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--game",     default="",                 help="Game name in GameNative")
    parser.add_argument("--server",   default="http://localhost:8080")
    parser.add_argument("--provider", default="chatgpt",          choices=list(PROVIDERS))
    parser.add_argument("--headless", action="store_true",        help="Run browser headless")
    parser.add_argument("--skip-done", action="store_true")
    args = parser.parse_args()

    phone = Phone(args.server)
    if not phone.is_alive():
        print(f"ERROR: phonecontrol server not reachable at {args.server}")
        print("Start it: python -m uvicorn app:app --host 0.0.0.0 --port 8080")
        sys.exit(1)
    log.info(f"Phone server OK: {args.server}")

    provider_info = PROVIDERS[args.provider]
    BROWSER_DATA.mkdir(exist_ok=True)

    # Try system chromium on ARM (Raspberry Pi) if bundled fails
    chromium_path = None
    for candidate in ["/usr/bin/chromium-browser", "/usr/bin/chromium"]:
        if os.path.exists(candidate):
            chromium_path = candidate
            break

    async with async_playwright() as pw:
        ctx: BrowserContext = await pw.chromium.launch_persistent_context(
            str(BROWSER_DATA),
            headless=args.headless,
            executable_path=chromium_path,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        # First visit — let user log in if needed
        log.info(f"Opening {provider_info['name']}…")
        await page.goto(provider_info["url"], wait_until="domcontentloaded", timeout=30000)
        log.info(f"Browser open. If not logged in to {provider_info['name']}, log in now.")
        log.info("Press ENTER to start the config test loop once you're ready…")
        await asyncio.get_event_loop().run_in_executor(None, input)

        done = {json.dumps(r["config"], sort_keys=True) for r in load_results()} if args.skip_done else set()
        winner = None

        for config in CONFIGS:
            key = json.dumps(config, sort_keys=True)
            if key in done:
                log.info(f"Skipping: {config}")
                continue

            outcome = await test_config(phone, page, args.provider, config, args.game)
            save_result(config, outcome)

            if outcome == "stable":
                log.info(f"\n{'='*60}\nWINNER: {json.dumps(config, indent=2)}\n{'='*60}")
                winner = config
                break

            # Go back to main screen before next config
            for _ in range(3):
                phone.key("back")
            time.sleep(1)

        await ctx.close()

    if winner:
        print(f"\n✓ Working config:\n{json.dumps(winner, indent=2)}")
    else:
        print(f"\n✗ No stable config found. Results in results.json")


if __name__ == "__main__":
    asyncio.run(main())
